import os
import sys
import json
import time
import argparse
import asyncio
import urllib.request
import signal
import secrets
import tempfile
from pathlib import Path

# ── AutoPilot async engine ──────────────────────────────────────────────
from core.engine import (
    install_uvloop_policy,
    AutoPilotWatchdog,
    AtomicNATSwitcher,
    ws_broadcaster,
    write_json_atomic,
)

# Install uvloop as the event-loop policy before anything else starts.
install_uvloop_policy()

import socket

# Set global socket default timeout to prevent blocking thread pool indefinitely
socket.setdefaulttimeout(15.0)

# Add project root to path for local imports
sys.path.append(str(Path(__file__).parent))

from core.netns import NetNSManager
from core.openvpn import OpenVPNRunner
from core.proxy import MixedProxyServer
from core.scraper import CandidateScraper
from core.web import AsyncWebServer

# Global configurations
DEFAULT_CONFIG = {
    "username": "admin",
    "password": secrets.token_urlsafe(18),
    "secret_path": secrets.token_urlsafe(9),
    "ui_host": "0.0.0.0",
    "ui_port": 8787,
    "proxy_host": "127.0.0.1",
    "proxy_port": 7928,
    "routing_mode": "auto",
    "force_country": "",
    "connection_enabled": False,
    "fixed_node_id": "",
    "scamalytics_threshold": 10,
    "api_url": "https://www.vpngate.net/api/iphone/"
}

ALLOWED_ROUTING_MODES = {"auto", "fixed_ip", "fixed_region"}
CONFIG_TYPES = {
    "username": str,
    "password": str,
    "secret_path": str,
    "ui_host": str,
    "ui_port": int,
    "proxy_host": str,
    "proxy_port": int,
    "routing_mode": str,
    "force_country": str,
    "connection_enabled": bool,
    "fixed_node_id": str,
    "scamalytics_threshold": int,
    "api_url": str
}

def get_data_dir() -> Path:
    """Choose data dir: /opt/vpngate-pro/vpngate_data or fallback to local directory."""
    opt_path = Path("/opt/vpngate-pro/vpngate_data")
    try:
        opt_path.mkdir(parents=True, exist_ok=True)
        return opt_path
    except Exception:
        local_path = Path(__file__).parent / "vpngate_data"
        local_path.mkdir(parents=True, exist_ok=True)
        return local_path

class VPNGateProManager:
    def __init__(self):
        self.data_dir = get_data_dir()
        self.config_file = self.data_dir / "config.json"
        self.nodes_file = self.data_dir / "nodes.json"
        self.created_config = False
        
        self.config = self.load_config()
        
        # Core components
        self.netns_mgr = NetNSManager(
            ns_name="vpn_ns", 
            host_port=self.config.get("proxy_port", 7928),
            ns_port=self.config.get("proxy_port", 7928)
        )
        self.vpn_runner = OpenVPNRunner(self.netns_mgr, data_dir=self.data_dir)
        # Inject WS broadcaster so OpenVPN log milestones stream to the UI in real-time
        self.vpn_runner.set_ws_broadcaster(ws_broadcaster.broadcast)
        self.scraper = CandidateScraper(
            data_dir=self.data_dir,
            api_url=self.config.get("api_url", "https://www.vpngate.net/api/iphone/")
        )
        
        # State indicators
        self.state_lock = asyncio.Lock()
        self.is_connecting = False
        self.last_check_message = "系统已启动，正在初始化..."
        self.connected_at = 0
        self.proxy_ok = False
        self.failed_node_ids = set()
        self.ns_ok = True
        
        # Background loops
        self.loops_running = False
        self.proxy_process = None

        # ── AutoPilot engine components ──────────────────────────────────
        self.nat_switcher  = AtomicNATSwitcher(
            subnet=self.netns_mgr.subnet,
            ns_name=self.netns_mgr.ns_name,
        )
        self.autopilot = AutoPilotWatchdog(self, self.nat_switcher)

    def load_config(self) -> dict:
        """Load config file, or write default config if missing."""
        cfg = DEFAULT_CONFIG.copy()
        loaded = {}
        if self.config_file.exists():
            try:
                loaded = json.loads(self.config_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg.update(loaded)
                else:
                    loaded = {}
            except Exception as e:
                print(f"[Config] Failed to read config, using defaults: {e}", flush=True)
                loaded = {}
        else:
            self.created_config = True

        normalized = self.normalize_config(cfg)
        if self.created_config or any(loaded.get(k) != normalized[k] for k in CONFIG_TYPES):
            self.save_config_sync(normalized)
            if self.created_config:
                try:
                    os.chmod(self.config_file, 0o600)
                except Exception:
                    pass
        return normalized

    def normalize_config(self, cfg: dict) -> dict:
        """Keep persisted settings within the supported schema and ranges."""
        normalized = DEFAULT_CONFIG.copy()
        for key, expected_type in CONFIG_TYPES.items():
            if key not in cfg:
                continue
            value = cfg[key]
            if expected_type is bool:
                if isinstance(value, bool):
                    normalized[key] = value
                elif isinstance(value, str):
                    normalized[key] = value.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    normalized[key] = bool(value)
            elif expected_type is int:
                try:
                    normalized[key] = int(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(value, expected_type):
                normalized[key] = value

        normalized["routing_mode"] = (
            normalized["routing_mode"]
            if normalized["routing_mode"] in ALLOWED_ROUTING_MODES
            else "auto"
        )
        normalized["ui_port"] = min(max(normalized["ui_port"], 1), 65535)
        normalized["proxy_port"] = min(max(normalized["proxy_port"], 1), 65535)
        normalized["scamalytics_threshold"] = min(max(normalized["scamalytics_threshold"], 0), 100)
        normalized["force_country"] = normalized["force_country"].strip().upper()[:2]
        normalized["username"] = normalized["username"].strip() or DEFAULT_CONFIG["username"]
        normalized["secret_path"] = normalized["secret_path"].strip().strip("/") or DEFAULT_CONFIG["secret_path"]
        return normalized

    def save_config_sync(self, cfg):
        cfg = self.normalize_config(cfg)
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(cfg, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.config_file.parent,
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.config_file)

    async def save_config(self, cfg):
        self.config = self.normalize_config(cfg)
        await asyncio.to_thread(self.save_config_sync, cfg)

    def get_state_data(self) -> dict:
        """Return UI-compatible state representation."""
        return {
            "routing_mode": self.config.get("routing_mode"),
            "force_country": self.config.get("force_country"),
            "connection_enabled": self.config.get("connection_enabled"),
            "fixed_node_id": self.config.get("fixed_node_id"),
            "scamalytics_threshold": self.config.get("scamalytics_threshold"),
            "secret_path": self.config.get("secret_path"),
            "username": self.config.get("username", "admin"),
            
            "is_connecting": self.is_connecting,
            "last_check_message": self.last_check_message,
            "active_openvpn_node_id": self.vpn_runner.current_node_id if self.vpn_runner.is_running() else "",
            "connected_at": self.connected_at,
            "proxy_ok": self.proxy_ok
        }

    async def get_filtered_nodes(self) -> list:
        """Return nodes that match our scamalytics threshold."""
        async with self.scraper.nodes_lock:
            nodes = await asyncio.to_thread(self.scraper.load_nodes_sync)
            
        threshold = self.config.get("scamalytics_threshold", 10)
        filtered = []
        for n in nodes:
            # Active node is always shown
            if n.get("id") == self.vpn_runner.current_node_id:
                filtered.append(n)
                continue
            
            score = n.get("scamalytics_score")
            # If scamalytics score is known and exceeds threshold, hide it
            if score is not None and score >= threshold:
                continue
            filtered.append(n)
            
        return self.sort_nodes(filtered)

    def sort_nodes(self, nodes: list) -> list:
        """Sort nodes: available first (residential/mobile priority, then latency), then untested, then unavailable."""
        available = sorted(
            [n for n in nodes if n.get("probe_status") == "available"],
            key=lambda n: (
                0 if n.get("ip_type") in ("residential", "mobile") else 1,
                n.get("latency_ms", 999999),
                -int(n.get("score", 0))
            )
        )
        untested = sorted(
            [n for n in nodes if n.get("probe_status") == "not_checked"],
            key=lambda n: (-int(n.get("score", 0)))
        )
        unavailable = sorted(
            [n for n in nodes if n.get("probe_status") == "unavailable"],
            key=lambda n: (-int(n.get("score", 0)))
        )
        return available + untested + unavailable

    async def update_settings(self, payload):
        """Update and persist settings."""
        cfg = self.config.copy()
        cfg.update({k: v for k, v in payload.items() if k in CONFIG_TYPES})
        await self.save_config(cfg)
        print(f"[Settings] Configurations updated: {payload}", flush=True)

    async def connect_node(self, node_id):
        """Manually trigger connection to a node."""
        if not self.ns_ok:
            async with self.state_lock:
                self.last_check_message = "连接失败：隔离网络空间 (NetNS) 未就绪，无法建立连接"
            return

        async with self.state_lock:
            if self.is_connecting:
                print("[Manager] Already connecting, ignoring connect_node request.", flush=True)
                return
            self.is_connecting = True
            self.last_check_message = "正在准备 OpenVPN 连接配置..."
            
        try:
            async with self.scraper.nodes_lock:
                nodes = await asyncio.to_thread(self.scraper.load_nodes_sync)
                
            node = next((n for n in nodes if n["id"] == node_id), None)
            if not node:
                async with self.state_lock:
                    self.last_check_message = "连接失败：找不到节点"
                return
                
            # Write connection enabled flag in config
            cfg = self.config.copy()
            cfg["connection_enabled"] = True
            
            # If in fixed IP mode, lock this node
            if cfg["routing_mode"] == "fixed_ip":
                cfg["fixed_node_id"] = node_id
            await self.save_config(cfg)

            success = await self.vpn_runner.start(node_id, node["config_text"])
            
            async with self.state_lock:
                if success:
                    self.connected_at = time.time()
                    self.last_check_message = "连接成功，正在进行连通性诊断..."
                    # Run health check
                    asyncio.create_task(self.diagnose_connection())
                else:
                    self.last_check_message = f"连接失败：{self.vpn_runner.conn_message}"
                    await self.vpn_runner.stop()
        finally:
            async with self.state_lock:
                self.is_connecting = False

    async def disconnect_node(self):
        """Manually trigger disconnect."""
        async with self.state_lock:
            self.is_connecting = False
            self.last_check_message = "正在断开连接..."
            self.proxy_ok = False
            self.connected_at = 0
            
        # Disable auto-connection
        cfg = self.config.copy()
        cfg["connection_enabled"] = False
        await self.save_config(cfg)

        await self.vpn_runner.stop()
        
        async with self.state_lock:
            self.last_check_message = "已断开连接"

    async def test_node_latency(self, node_id):
        """Asynchronously ping a node to update its latency."""
        async with self.scraper.nodes_lock:
            nodes = await asyncio.to_thread(self.scraper.load_nodes_sync)
            
        node = next((n for n in nodes if n["id"] == node_id), None)
        if not node:
            return
            
        ip = node.get("ip")
        
        # Safely parse port
        port_val = node.get("remote_port")
        if port_val is None:
            port = 443
        else:
            try:
                port = int(port_val)
            except (ValueError, TypeError):
                port = 443
        
        latency = await self._tcp_ping(ip, port)
        
        async with self.scraper.nodes_lock:
            current_nodes = await asyncio.to_thread(self.scraper.load_nodes_sync)
            cn = next((n for n in current_nodes if n["id"] == node_id), None)
            if cn:
                cn["latency_ms"] = latency
                cn["probe_status"] = "available" if latency < 99999 else "unavailable"
                cn["probed_at"] = time.time()
                await asyncio.to_thread(self.scraper.save_nodes_sync, current_nodes)

    async def update_active_node_latency(self, node_id, latency):
        """Update active node's latency and probe status directly in the database."""
        async with self.scraper.nodes_lock:
            current_nodes = await asyncio.to_thread(self.scraper.load_nodes_sync)
            cn = next((n for n in current_nodes if n["id"] == node_id), None)
            if cn:
                cn["latency_ms"] = latency
                cn["probe_status"] = "available" if latency < 99999 else "unavailable"
                cn["probed_at"] = time.time()
                await asyncio.to_thread(self.scraper.save_nodes_sync, current_nodes)

    async def _tcp_ping(self, ip, port, timeout=3.0) -> int:
        """Measure TCP handshake latency."""
        t0 = time.time()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port),
                timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return int((time.time() - t0) * 1000)
        except Exception:
            return 999999

    async def diagnose_connection(self) -> bool:
        """Test proxy tunnel connectivity."""
        print("[Watchdog] Running proxy connectivity diagnosis...", flush=True)
        proxy_port = self.config.get("proxy_port", 7928)
        
        t0 = time.time()
        # Build curl command to test proxy asynchronously with timeout
        cmd = [
            "curl", "-s", "-x", f"http://127.0.0.1:{proxy_port}",
            "-m", "5", "http://www.msftconnecttest.com/connecttest.txt"
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=6.0)
            ok = "Microsoft Connect Test" in stdout.decode(errors="replace")
            latency = int((time.time() - t0) * 1000)
        except Exception:
            ok = False
            latency = 999999

        self.proxy_ok = ok
        
        async with self.state_lock:
            if ok:
                self.last_check_message = f"连接健康，代理通道畅通 (延迟 {latency}ms)"
                # Update current active node details in database with the measured proxy latency
                if self.vpn_runner.current_node_id:
                    asyncio.create_task(self.update_active_node_latency(self.vpn_runner.current_node_id, latency))
            else:
                self.last_check_message = "网络就绪，但本地代理出口测试失败"
                
        return ok

    async def start_background_loops(self):
        """Start daemon loops."""
        self.loops_running = True
        asyncio.create_task(self.collector_loop())
        # asyncio.create_task(self.watchdog_loop())
        asyncio.create_task(self.scamalytics_backpopulation_loop())
        # Start AutoPilot watchdog (60s health cycle + circuit breaker)
        await self.autopilot.start()

    async def collector_loop(self):
        """Scrapes VPNGate and tests candidate nodes periodically (every 10 minutes)."""
        while self.loops_running:
            try:
                candidates = await self.scraper.scrape_candidates()
                if candidates:
                    self.failed_node_ids.clear()
                    print(f"[Collector] Enriching {len(candidates)} candidates geolocations...", flush=True)
                    await self.scraper.enrich_nodes_info(candidates, fetch_scamalytics=False)
                    async with self.scraper.nodes_lock:
                        nodes = await asyncio.to_thread(self.scraper.load_nodes_sync)
                        
                        # Merge lists (keep active node and existing checked nodes)
                        active_id = self.vpn_runner.current_node_id
                        active_node = next((n for n in nodes if n["id"] == active_id), None)
                        
                        merged = []
                        seen_ids = set()
                        if active_node:
                            merged.append(active_node)
                            seen_ids.add(active_id)
                            
                        # Preserve existing nodes that have geolocation and scamalytics score
                        nodes_map = {n["id"]: n for n in nodes if n.get("scamalytics_score") is not None}
                        
                        for cand in candidates:
                            if cand["id"] in seen_ids:
                                continue
                            # If we have it in memory with scamalytics score, recover it
                            if cand["id"] in nodes_map:
                                merged.append(nodes_map[cand["id"]])
                            else:
                                merged.append(cand)
                            seen_ids.add(cand["id"])
                            
                        if len(merged) > 1000:
                            merged = merged[:1000]
                            
                        await asyncio.to_thread(self.scraper.save_nodes_sync, merged)

                # Select the best candidates to test
                async with self.scraper.nodes_lock:
                    current_nodes = await asyncio.to_thread(self.scraper.load_nodes_sync)
                    
                # Load IP cache to skip known high fraud nodes
                async with self.scraper.cache_lock:
                    cache = await asyncio.to_thread(self.scraper.load_cache)
                    
                def is_test_candidate(n):
                    if n.get("id") == self.vpn_runner.current_node_id:
                        return False
                    # Check Scamalytics score in node or cache
                    score = n.get("scamalytics_score")
                    if score is None:
                        ip = n.get("ip")
                        if ip in cache:
                            score = cache[ip].get("scamalytics_score")
                    
                    if score is not None and score >= self.config.get("scamalytics_threshold", 10):
                        return False
                    
                    # Respect region lock filter
                    if self.config.get("routing_mode") == "fixed_region":
                        country = self.config.get("force_country", "")
                        if country and n.get("country") != country:
                            return False
                            
                    return n.get("probe_status") == "not_checked"

                to_test = [n for n in current_nodes if is_test_candidate(n)][:10]
                if to_test:
                    print(f"[Collector] Testing {len(to_test)} unscored candidates concurrently...", flush=True)
                    # Run TCP ping tests concurrently
                    tasks = [self.test_node_latency(n["id"]) for n in to_test]
                    await asyncio.gather(*tasks)
                    
                    # After ping success, enrich geolocation and scamalytics scores for the tested nodes
                    async with self.scraper.nodes_lock:
                        nodes = await asyncio.to_thread(self.scraper.load_nodes_sync)
                    # Enrich the top available ones
                    available_unscored = [n for n in nodes if n.get("probe_status") == "available" and n.get("scamalytics_score") is None][:5]
                    if available_unscored:
                        print(f"[Collector] Geolocating and scoring {len(available_unscored)} newly available nodes...", flush=True)
                        await self.scraper.enrich_nodes_info(available_unscored)
                        async with self.scraper.nodes_lock:
                            # Save back to database
                            nodes_db = await asyncio.to_thread(self.scraper.load_nodes_sync)
                            for ndb in nodes_db:
                                match = next((x for x in available_unscored if x["id"] == ndb["id"]), None)
                                if match:
                                    ndb.update(match)
                            await asyncio.to_thread(self.scraper.save_nodes_sync, nodes_db)

            except Exception as e:
                print(f"[Collector] Error in collector loop: {e}", flush=True)
            
            # Run every 10 minutes
            await asyncio.sleep(600)

    async def watchdog_loop(self):
        """Monitor connection status and handle auto-connection / failovers."""
        await asyncio.sleep(10) # Let system startup
        while self.loops_running:
            try:
                conn_enabled = self.config.get("connection_enabled", False)
                is_running = self.vpn_runner.is_running()
                
                if conn_enabled:
                    if not is_running:
                        print("[Watchdog] Tunnel down but connection enabled. Initiating auto-reconnect...", flush=True)
                        await self.auto_reconnect()
                    else:
                        # Tunnel is running, perform active diagnostic check
                        healthy = await self.diagnose_connection()
                        if not healthy:
                            print("[Watchdog] Proxy diagnostic failed. Restarting tunnel...", flush=True)
                            if self.vpn_runner.current_node_id:
                                self.failed_node_ids.add(self.vpn_runner.current_node_id)
                            await self.auto_reconnect()
                else:
                    if is_running:
                        print("[Watchdog] Connection disabled but tunnel running. Terminating...", flush=True)
                        await self.vpn_runner.stop()
            except Exception as e:
                print(f"[Watchdog] Error in watchdog loop: {e}", flush=True)
                
            await asyncio.sleep(10)   # halved: faster failover detection

    async def auto_reconnect(self):
        """Select node based on routing mode and connect."""
        if not self.ns_ok:
            return

        async with self.state_lock:
            if self.is_connecting:
                print("[Manager] Already connecting, ignoring auto_reconnect request.", flush=True)
                return
            self.is_connecting = True
            self.last_check_message = "正在重新拉起隧道..."
            
        try:
            routing_mode = self.config.get("routing_mode", "auto")
            target_country = self.config.get("force_country", "")
            fixed_node_id = self.config.get("fixed_node_id", "")
            
            async with self.scraper.nodes_lock:
                nodes = await asyncio.to_thread(self.scraper.load_nodes_sync)
                
            target_node = None
            if routing_mode == "fixed_ip" and fixed_node_id:
                target_node = next((n for n in nodes if n["id"] == fixed_node_id), None)
                if not target_node:
                    print(f"[Watchdog] Locked node {fixed_node_id} not found in database. Falling back to auto.", flush=True)
                    routing_mode = "auto"
                    
            if routing_mode != "fixed_ip" or not target_node:
                # Filter available nodes and skip blacklisted failed nodes
                available = [n for n in nodes if n.get("probe_status") == "available" and n["id"] not in self.failed_node_ids]
                
                # Respect region filter
                if routing_mode == "fixed_region" and target_country:
                    available = [n for n in available if n.get("country") == target_country]
                    
                # Filter scamalytics threshold
                threshold = self.config.get("scamalytics_threshold", 10)
                available = [n for n in available if n.get("scamalytics_score") is None or n.get("scamalytics_score") < threshold]
                
                sorted_avail = self.sort_nodes(available)
                if sorted_avail:
                    target_node = sorted_avail[0]
                    
            if target_node:
                print(f"[Watchdog] Selected reconnect target: {target_node['id']} ({target_node['ip']})", flush=True)
                
                success = await self.vpn_runner.start(target_node["id"], target_node["config_text"])
                
                async with self.state_lock:
                    if success:
                        self.connected_at = time.time()
                        self.last_check_message = "已自动重连，通道正常"
                        asyncio.create_task(self.diagnose_connection())
                    else:
                        self.last_check_message = f"自动重连失败：{self.vpn_runner.conn_message}"
                        self.failed_node_ids.add(target_node["id"])
                        await self.vpn_runner.stop()
            else:
                print("[Watchdog] No suitable reconnect candidates found.", flush=True)
                async with self.state_lock:
                    self.last_check_message = "没有可用的备用节点，等待下一次节点更新..."
        finally:
            async with self.state_lock:
                self.is_connecting = False

    async def scamalytics_backpopulation_loop(self):
        """Background daemon that slowly queries scamalytics scores for all database nodes."""
        await asyncio.sleep(15)
        while self.loops_running:
            try:
                async with self.scraper.nodes_lock:
                    nodes = await asyncio.to_thread(self.scraper.load_nodes_sync)
                    
                if not nodes:
                    await asyncio.sleep(10)
                    continue
                
                # Filter nodes that do not have scamalytics_score (excluding sentinel -1)
                unscored = [n for n in nodes if n.get("scamalytics_score") is None]
                if not unscored:
                    await asyncio.sleep(15)
                    continue
                
                # Prioritize: active/available first, then untested, then unavailable
                def get_priority(node):
                    status = node.get("probe_status", "not_checked")
                    if status == "available" or node.get("id") == self.vpn_runner.current_node_id:
                        return 0
                    elif status == "not_checked":
                        return 1
                    else:
                        return 2
                
                unscored.sort(key=get_priority)
                target = unscored[0]
                ip = target.get("ip")
                if not ip:
                    # Set sentinel
                    self.update_node_score_sync(target["id"], -1)
                    continue
                    
                # Check cache first
                async with self.scraper.cache_lock:
                    cache = await asyncio.to_thread(self.scraper.load_cache)
                    
                score = None
                if ip in cache and cache[ip].get("scamalytics_score") is not None:
                    score = cache[ip]["scamalytics_score"]
                else:
                    print(f"[Scamalytics] Cache miss/None for {ip}. Querying Scamalytics API...", flush=True)
                    score = await self.scraper.get_scamalytics_score(ip)
                    # Add to cache
                    if ip not in cache:
                        cache[ip] = {
                            "owner": target.get("owner", ""),
                            "asn": target.get("asn", ""),
                            "as_name": target.get("as_name", ""),
                            "location": target.get("location", ""),
                            "ip_type": target.get("ip_type", "datacenter"),
                            "quality": target.get("quality", "normal"),
                            "cached_at": time.time()
                        }
                    cache[ip]["scamalytics_score"] = score
                    async with self.scraper.cache_lock:
                        await asyncio.to_thread(self.scraper.save_cache, cache)
                    await asyncio.sleep(1.5) # Rate limit delay
                
                # Save score back to database
                print(f"[Scamalytics] Backpopulating score {score} for node {target['id']} ({ip})", flush=True)
                await self.update_node_score(target["id"], score)
                
            except Exception as e:
                print(f"[Scamalytics] Backpopulation error: {e}", flush=True)
                await asyncio.sleep(5)
                
            await asyncio.sleep(0.5)

    async def update_node_score(self, node_id, score):
        async with self.scraper.nodes_lock:
            await asyncio.to_thread(self.update_node_score_sync, node_id, score)

    def update_node_score_sync(self, node_id, score):
        nodes = self.scraper.load_nodes_sync()
        for n in nodes:
            if n["id"] == node_id:
                n["scamalytics_score"] = score
                break
        self.scraper.save_nodes_sync(nodes)

    async def start_proxy_subprocess(self):
        """Launch the proxy server inside the network namespace (Linux only)."""
        if not self.netns_mgr.is_linux:
            # On macOS, we run the proxy server in a local asyncio task instead of subprocess namespace
            print("[Proxy] Mock namespace: Starting mixed proxy locally.", flush=True)
            self.local_proxy = MixedProxyServer(host="127.0.0.1", port=self.config.get("proxy_port", 7928))
            await self.local_proxy.start()
            return

        print("[Proxy] Linux host: Spawning proxy process inside network namespace...", flush=True)
        # Execute itself with --proxy-only in netns
        prefix = self.netns_mgr.get_ns_prefix()
        cmd = prefix + [
            "python3",
            __file__,
            "--proxy-only",
            "--port", str(self.config.get("proxy_port", 7928))
        ]
        
        try:
            self.proxy_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            # Log reader
            async def log_reader():
                try:
                    while True:
                        line = await self.proxy_process.stdout.readline()
                        if not line:
                            break
                        print(f"[Proxy Core Log] {line.decode().strip()}", flush=True)
                except Exception:
                    pass
            asyncio.create_task(log_reader())
            
            # Start port forwarding
            self.netns_mgr.start_port_forward(listen_host=self.config.get("proxy_host", "127.0.0.1"))
        except Exception as e:
            print(f"[Proxy] Failed to spawn proxy subprocess: {e}", flush=True)

    async def shutdown(self):
        """Clean teardown."""
        print("[System] Shutting down service...", flush=True)
        self.loops_running = False

        # Stop AutoPilot watchdog first
        await self.autopilot.stop()
        
        # Stop servers
        if hasattr(self, "local_proxy"):
            await self.local_proxy.stop()
            
        if self.proxy_process:
            try:
                self.proxy_process.terminate()
                await self.proxy_process.wait()
            except Exception:
                pass
            self.proxy_process = None

        await self.vpn_runner.stop()
        self.netns_mgr.cleanup()
        print("[System] Service terminated successfully.", flush=True)

async def main():
    # Root check for Linux
    if sys.platform.startswith("linux") and os.geteuid() != 0:
        print("[System] Error: AetherGate Pro must be run as root (sudo python3 main.py) to manage network namespaces and iptables.", file=sys.stderr, flush=True)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="AetherGate Pro Gateway Manager")
    parser.add_argument("--proxy-only", action="store_true", help="Run proxy server inside network namespace")
    parser.add_argument("--port", type=int, default=7928, help="Proxy server listen port")
    args = parser.parse_args()

    # Register signal handlers for graceful shutdown on Linux/Unix
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    
    def handle_stop():
        stop_event.set()
        
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_stop)
        except NotImplementedError:
            pass

    if args.proxy_only:
        # Spawn SOCKS5/HTTP Proxy only (running inside netns)
        proxy = MixedProxyServer(host="0.0.0.0", port=args.port)
        await proxy.start()
        # Keep running until signaled
        try:
            await stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await proxy.stop()
        return

    # Master manager process
    manager = VPNGateProManager()
    
    # 1. Setup namespace
    ns_ok = manager.netns_mgr.setup()
    manager.ns_ok = ns_ok
    if not ns_ok:
        manager.last_check_message = "❌ 隔离网络空间 (NetNS vpn_ns) 创建失败！请确认以 root 权限运行此服务，或者您的 VPS 虚拟化环境支持网络命名空间 (LXC容器需开启 nesting / 挂载权限)。"
        print(f"[System] {manager.last_check_message}", flush=True)
    
    # 2. Spawn namespace proxy
    if ns_ok:
        await manager.start_proxy_subprocess()
    
    # 3. Start web API dashboard server
    web_server = AsyncWebServer(
        manager,
        host=manager.config.get("ui_host", "0.0.0.0"),
        port=manager.config.get("ui_port", 8787)
    )
    await web_server.start()
    
    # 4. Start background update/watchdog tasks
    if ns_ok:
        await manager.start_background_loops()

    # 5. Keep running and listen for termination
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await web_server.stop()
        await manager.shutdown()

if __name__ == "__main__":
    try:
        # uvloop policy already installed at import time via install_uvloop_policy().
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
