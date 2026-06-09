import os
import asyncio
import sys
import tempfile
from pathlib import Path
import subprocess

# Sentinel keywords from OpenVPN log that indicate progress milestones
_MILESTONES = {
    "Attempting to establish TCP connection":  "正在建立 TCP 连接...",
    "TCP connection established":              "TCP 连接已建立",
    "TLS handshake":                           "TLS 握手中...",
    "Control Channel Authentication":         "认证控制通道...",
    "Control Channel Opened":                 "控制通道已开启",
    "PUSH_REPLY":                              "接收服务器路由推送...",
    "add_route":                               "正在写入路由表...",
    "Initialization Sequence Completed":       "✅ 隧道初始化完成",
}

_FAIL_KEYWORDS = {
    "AUTH_FAILED":       "认证失败（用户名/密码错误）",
    "TLS Error":         "TLS 握手失败（服务器证书问题）",
    "TLS_ERROR":         "TLS 握手失败（服务器证书问题）",
    "Connection refused":"服务器拒绝连接",
    "Connection timed out": "连接超时",
    "ECONNREFUSED":      "服务器拒绝连接",
    "RESOLVE":           "域名解析失败",
    "Permission denied": "脚本权限不足（dns_up.sh 需要 chmod +x）",
    "Options error":     "OpenVPN 配置项错误",
    "Cannot open":       "无法打开配置文件或命名空间",
    "failed to start":   "OpenVPN 启动失败",
}


class OpenVPNRunner:
    def __init__(self, netns_mgr, data_dir="/opt/vpngate-pro/vpngate_data"):
        self.netns_mgr = netns_mgr
        self.data_dir = Path(data_dir)
        self.config_file = self.data_dir / "current.ovpn"
        self.proc = None
        self.connected = False
        self.conn_message = "未连接"
        self.current_node_id = ""

        # Event-driven connection signalling — replaces sleep-poll loop
        self._connected_event: asyncio.Event = asyncio.Event()
        self._failed_event:    asyncio.Event = asyncio.Event()

        # Optional WebSocket broadcaster (injected by main.py after import)
        self._ws_broadcast = None   # callable: async (dict) -> None

    def set_ws_broadcaster(self, fn):
        """Inject WebSocket broadcast callable so logs stream to the UI."""
        self._ws_broadcast = fn

    def ensure_dirs(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def _push(self, msg: str):
        """Push a real-time progress message to WebSocket subscribers."""
        self.conn_message = msg
        if self._ws_broadcast:
            try:
                await self._ws_broadcast({
                    "event": "openvpn_progress",
                    "data":  {"message": msg, "node_id": self.current_node_id},
                    "ts":    __import__("time").time(),
                })
            except Exception:
                pass

    async def _download_ca_certs_async(self):
        """
        Download Let's Encrypt Generation Y root/intermediate CAs.
        Uses asyncio.to_thread so it never blocks the event loop.
        """
        gen_y_root_path = self.data_dir / "root-yr.pem"
        gen_y_int_path  = self.data_dir / "int-yr1.pem"

        if gen_y_root_path.exists() and gen_y_int_path.exists():
            return   # Already cached — skip network round-trip entirely

        await self._push("正在下载 Let's Encrypt CA 证书链...")

        async def _fetch(url, dest):
            import urllib.request
            try:
                await asyncio.to_thread(urllib.request.urlretrieve, url, dest)
            except Exception as e:
                print(f"[OpenVPN] Failed to download {url}: {e}", flush=True)

        # Download both concurrently
        await asyncio.gather(
            _fetch("https://letsencrypt.org/certs/gen-y/root-yr.pem",  gen_y_root_path),
            _fetch("https://letsencrypt.org/certs/gen-y/int-yr1.pem",  gen_y_int_path),
        )

    async def _write_config_async(self, config_text: str) -> bool:
        """Atomic config write via tempfile, run in thread pool."""
        try:
            await self._push("正在写入 VPN 配置文件...")

            # Merge CA bundle (async file read in thread)
            ca_text = ""
            ca_bundle_path = Path("/etc/ssl/certs/ca-certificates.crt")
            if ca_bundle_path.exists():
                try:
                    ca_text = await asyncio.to_thread(ca_bundle_path.read_text, "utf-8")
                except Exception:
                    pass

            for p in [self.data_dir / "root-yr.pem", self.data_dir / "int-yr1.pem"]:
                if p.exists():
                    try:
                        ca_text += "\n" + await asyncio.to_thread(p.read_text, "utf-8")
                    except Exception:
                        pass

            if ca_text and "</ca>" in config_text:
                config_text = config_text.replace("</ca>", f"\n{ca_text}\n</ca>")

            # Atomic write: tempfile → fsync → replace
            def _write():
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8",
                    dir=self.config_file.parent,
                    delete=False, suffix=".ovpn.tmp"
                ) as tmp:
                    tmp.write(config_text)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    tmp_path = tmp.name
                os.replace(tmp_path, self.config_file)

            await asyncio.to_thread(_write)
            return True
        except Exception as e:
            self.conn_message = f"配置写入失败: {e}"
            return False

    async def start(self, node_id, config_text):
        """Start OpenVPN inside the network namespace."""
        self.ensure_dirs()
        await self.stop()

        self.current_node_id = node_id
        self.connected = False
        self._connected_event.clear()
        self._failed_event.clear()

        await self._push("正在准备连接...")

        # ① Download CA certs asynchronously (non-blocking, cached after first run)
        await self._download_ca_certs_async()

        # ② Write config atomically in thread pool
        if not await self._write_config_async(config_text):
            return False

        # ③ Launch OpenVPN subprocess
        prefix   = self.netns_mgr.get_ns_prefix()
        core_dir = Path(__file__).resolve().parent
        cmd = prefix + [
            "openvpn",
            "--config",       str(self.config_file),
            "--dev",          "tun0",
            "--data-ciphers", "AES-128-CBC:AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305",
            "--script-security", "2",
            "--tls-verify",   "/bin/true",
            "--up",           str(core_dir / "dns_up.sh"),
            "--down",         str(core_dir / "dns_down.sh"),
            # Faster reconnect tuning
            "--connect-retry",       "2",   # retry after 2s on TCP failure
            "--connect-retry-max",   "3",   # max 3 retries then give up
            "--server-poll-timeout", "8",   # UDP server poll timeout
        ]

        await self._push(f"正在启动 OpenVPN 进程 → 节点 {node_id[:20]}...")
        print(f"[OpenVPN] Launching for node {node_id}: {' '.join(cmd)}", flush=True)

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            self.conn_message = f"启动失败: {e}"
            print(f"[OpenVPN] Failed to start process: {e}", flush=True)
            return False

        # ④ Start real-time log reader (sets events on success/failure)
        asyncio.create_task(self._read_logs(), name=f"ovpn-logs-{node_id[:12]}")

        # ⑤ Wait for connected or failed event (with overall timeout)
        CONNECT_TIMEOUT = 25.0   # seconds — was 30, now 25 with faster signalling
        try:
            done, _ = await asyncio.wait(
                [
                    asyncio.create_task(self._connected_event.wait()),
                    asyncio.create_task(self._failed_event.wait()),
                ],
                timeout=CONNECT_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception:
            done = set()

        if self._connected_event.is_set():
            return True

        # Process already exited or timeout
        if not done:
            self.conn_message = f"连接超时（{int(CONNECT_TIMEOUT)}秒内未完成握手）"
            await self._push(self.conn_message)

        return False

    async def _read_logs(self):
        """
        Asynchronously consume OpenVPN stdout line-by-line.
        Sets asyncio.Events on success/failure — replaces the sleep-poll loop.
        Streams meaningful milestones to the WebSocket broadcaster.
        """
        if not self.proc or not self.proc.stdout:
            self._failed_event.set()
            return

        try:
            while True:
                line_bytes = await self.proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                print(f"[OpenVPN Log] {line}", flush=True)

                # ── Success ──────────────────────────────────────────────
                if "Initialization Sequence Completed" in line:
                    self.connected = True
                    self.conn_message = "连接成功"
                    self._connected_event.set()
                    await self._push("✅ OpenVPN 隧道已建立，代理通道就绪")
                    continue

                # ── Failures ─────────────────────────────────────────────
                matched_fail = False
                for kw, msg in _FAIL_KEYWORDS.items():
                    if kw in line:
                        self.conn_message = msg
                        self._failed_event.set()
                        await self._push(f"❌ {msg}")
                        matched_fail = True
                        break

                if matched_fail:
                    continue

                # ── Progress milestones → WebSocket ──────────────────────
                for kw, msg in _MILESTONES.items():
                    if kw in line:
                        await self._push(msg)
                        break

                # Generic exit signals
                if any(k in line for k in ("SIGTERM", "SIGINT", "exiting", "process exiting")):
                    self.connected = False
                    self._failed_event.set()
                    break

        except Exception as e:
            print(f"[OpenVPN] Error reading logs: {e}", flush=True)
        finally:
            self.connected = False
            if self.conn_message in ("连接成功", "正在连接...", "正在准备连接..."):
                self.conn_message = "连接已终止"
            # Ensure waiters are unblocked even on unexpected EOF
            self._failed_event.set()

    async def stop(self):
        """Terminate the OpenVPN process cleanly."""
        self.connected = False
        self.current_node_id = ""
        self._connected_event.clear()
        self._failed_event.set()   # unblock any pending waiters

        if self.proc:
            print("[OpenVPN] Terminating OpenVPN process...", flush=True)
            try:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    print("[OpenVPN] Force killing OpenVPN process...", flush=True)
                    self.proc.kill()
                    await self.proc.wait()
            except Exception as e:
                print(f"[OpenVPN] Error stopping process: {e}", flush=True)
            finally:
                self.proc = None

        # Clean up temp file
        if self.config_file.exists():
            try:
                self.config_file.unlink()
            except Exception:
                pass

        # Pkill any orphaned openvpn in the namespace
        if self.netns_mgr.is_linux:
            prefix = self.netns_mgr.get_ns_prefix()
            try:
                subprocess.run(prefix + ["pkill", "-f", "openvpn"],
                               capture_output=True, timeout=3)
            except Exception:
                pass

        self.conn_message = "已断开连接"

    def is_running(self):
        """Check if OpenVPN is currently running."""
        return self.proc is not None and self.proc.returncode is None
