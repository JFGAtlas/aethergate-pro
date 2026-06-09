"""
core/engine.py — AetherGate Pro · Async AutoPilot Engine
=========================================================

Architecture overview
---------------------
┌─────────────────────────────────────────────────────┐
│  AutoPilotWatchdog  (60s health cycle)              │
│   └─ HealthProbe  → latency + Scamalytics check     │
│   └─ CircuitBreaker (3 failures → iptables reset)   │
│   └─ AtomicSwitcher  → smooth tun0 NAT replace      │
│       (proxy socket handle stays alive on port 7928) │
└─────────────────────────────────────────────────────┘
          │  push real-time events
          ▼
  WSBroadcaster  ──► all connected WebSocket clients
          ▲
  FastAPI /ws endpoint (managed by web.py)

Key design decisions
--------------------
* uvloop is installed as the event loop policy at entry-point (main.py).
* All outbound HTTP uses httpx.AsyncClient with a shared connection pool
  (connect=5s, read=5s, write=5s, pool=5s).
* Config writes are always atomic: write to NamedTemporaryFile → fsync → replace.
* Circuit-breaker trips at 3 consecutive probe failures, runs iptables cleanup,
  and resets the VPN subprocess — then half-opens after a configurable cool-down.
* Atomic NAT switch: new iptables MASQUERADE rule is added BEFORE the old one is
  deleted, so there is zero gap in NAT coverage during a node transition.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Coroutine, Optional, Set

# --------------------------------------------------------------------------- #
#  Optional uvloop — gracefully falls back on standard asyncio on macOS.       #
# --------------------------------------------------------------------------- #
try:
    import uvloop  # type: ignore
    HAS_UVLOOP = True
except ImportError:
    HAS_UVLOOP = False


def install_uvloop_policy() -> None:
    """Install uvloop as the global event-loop policy (call once at startup)."""
    if HAS_UVLOOP:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        print("[Engine] uvloop event-loop policy installed.", flush=True)
    else:
        print("[Engine] uvloop not available — using default asyncio event loop.", flush=True)


# --------------------------------------------------------------------------- #
#  httpx shared client factory                                                 #
# --------------------------------------------------------------------------- #
try:
    import httpx  # type: ignore

    _HTTPX_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
    _HTTPX_LIMITS  = httpx.Limits(max_keepalive_connections=10, max_connections=20)

    def build_httpx_client(**kwargs) -> "httpx.AsyncClient":
        """Return a pre-configured httpx.AsyncClient with strict timeouts."""
        return httpx.AsyncClient(
            timeout=_HTTPX_TIMEOUT,
            limits=_HTTPX_LIMITS,
            follow_redirects=True,
            **kwargs,
        )

    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False
    httpx = None  # type: ignore

    def build_httpx_client(**kwargs):  # type: ignore[return]
        raise RuntimeError("httpx is not installed. Run: pip install httpx")


# --------------------------------------------------------------------------- #
#  WebSocket Event Bus (WSBroadcaster)                                         #
# --------------------------------------------------------------------------- #

class WSBroadcaster:
    """
    Maintains a set of active WebSocket send-queues.
    Any coroutine can call `broadcast(event)` to push a JSON event to every
    currently connected client without blocking.

    Protocol:  see docs/ws_protocol.md (generated in Step 2).
    """

    def __init__(self) -> None:
        self._queues: Set[asyncio.Queue] = set()
        self._lock   = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber and return its dedicated queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        async with self._lock:
            self._queues.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber queue (called on WebSocket disconnect)."""
        async with self._lock:
            self._queues.discard(q)

    async def broadcast(self, event: dict) -> None:
        """
        Non-blocking broadcast.  Slow / full queues are silently skipped to
        avoid head-of-line blocking across all subscribers.
        """
        msg = json.dumps(event, ensure_ascii=False)
        dead: list[asyncio.Queue] = []
        async with self._lock:
            snapshot = list(self._queues)
        for q in snapshot:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        if dead:
            async with self._lock:
                for dq in dead:
                    self._queues.discard(dq)

    def subscriber_count(self) -> int:
        return len(self._queues)


# Singleton shared across the whole process.
ws_broadcaster = WSBroadcaster()


# --------------------------------------------------------------------------- #
#  Circuit Breaker                                                              #
# --------------------------------------------------------------------------- #

class CBState(Enum):
    CLOSED    = auto()   # healthy — requests flow normally
    OPEN      = auto()   # tripped — requests blocked / remediation running
    HALF_OPEN = auto()   # cool-down done — next probe determines fate


@dataclass
class CircuitBreaker:
    """
    Three-state circuit-breaker for VPN tunnel health.

    * CLOSED   → normal operation.
    * 3 consecutive failures → OPEN → iptables cleanup + VPN process reset.
    * After `reset_timeout` seconds → HALF_OPEN → next probe re-evaluates.
    """
    failure_threshold: int   = 3
    reset_timeout:     float = 120.0   # seconds before half-open probe

    _failures:   int   = field(default=0,        init=False, repr=False)
    _state:      CBState = field(default=CBState.CLOSED, init=False, repr=False)
    _opened_at:  float = field(default=0.0,      init=False, repr=False)

    # Injected callbacks (set after construction).
    on_trip:     Optional[Callable[[], Coroutine]] = field(default=None, repr=False)

    @property
    def state(self) -> CBState:
        if self._state == CBState.OPEN:
            if time.monotonic() - self._opened_at >= self.reset_timeout:
                self._state = CBState.HALF_OPEN
                print("[CircuitBreaker] Half-open: attempting recovery probe.", flush=True)
        return self._state

    def is_open(self) -> bool:
        return self.state == CBState.OPEN

    async def record_failure(self) -> None:
        self._failures += 1
        print(f"[CircuitBreaker] Failure recorded ({self._failures}/{self.failure_threshold}).", flush=True)
        if self._failures >= self.failure_threshold and self._state == CBState.CLOSED:
            await self._trip()

    async def record_success(self) -> None:
        if self._state == CBState.HALF_OPEN:
            print("[CircuitBreaker] Half-open probe succeeded — closing breaker.", flush=True)
        self._failures = 0
        self._state    = CBState.CLOSED

    async def _trip(self) -> None:
        self._state     = CBState.OPEN
        self._opened_at = time.monotonic()
        print(
            f"[CircuitBreaker] ⚡ TRIPPED after {self._failures} consecutive failures. "
            "Triggering iptables cleanup + VPN reset.",
            flush=True,
        )
        await ws_broadcaster.broadcast({
            "event": "circuit_breaker_tripped",
            "data":  {"failures": self._failures, "reset_in": self.reset_timeout},
            "ts":    time.time(),
        })
        if self.on_trip:
            try:
                await self.on_trip()
            except Exception as exc:
                print(f"[CircuitBreaker] on_trip callback raised: {exc}", flush=True)


# --------------------------------------------------------------------------- #
#  Atomic NAT Switcher                                                         #
# --------------------------------------------------------------------------- #

class AtomicNATSwitcher:
    """
    Manages iptables MASQUERADE rules for the VPN namespace subnet.

    Switching strategy (zero NAT gap):
    1. ADD   new MASQUERADE rule for new egress interface.
    2. SLEEP  a short grace window so in-flight packets are handled.
    3. DELETE old MASQUERADE rule.

    The proxy process on port 7928 is NOT touched — its socket handle remains
    open in the network namespace throughout, so existing client connections
    survive the switch.
    """

    def __init__(self, subnet: str = "10.200.0", ns_name: str = "vpn_ns") -> None:
        self.subnet   = subnet
        self.ns_name  = ns_name
        self._current_interface: Optional[str] = None

    def _detect_default_interface(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True, text=True, timeout=3,
            )
            for part, nxt in zip(result.stdout.split(), result.stdout.split()[1:]):
                if part == "dev":
                    return nxt
        except Exception:
            pass
        return None

    def _iptables(self, action: str, interface: Optional[str]) -> bool:
        """Run a single iptables nat POSTROUTING rule."""
        cmd = ["iptables", "-t", "nat", action, "POSTROUTING",
               "-s", f"{self.subnet}.0/24"]
        if interface:
            cmd += ["-o", interface]
        cmd += ["-j", "MASQUERADE"]
        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=5)
            return True
        except Exception as exc:
            print(f"[AtomicNAT] iptables {action} failed: {exc}", flush=True)
            return False

    async def smooth_switch(self) -> None:
        """
        Atomically replace the active NAT rule.
        Safe to call while the proxy socket on 7928 is alive.
        """
        import sys
        if not sys.platform.startswith("linux"):
            print("[AtomicNAT] Non-Linux — skipping iptables switch.", flush=True)
            return

        new_iface = self._detect_default_interface()
        old_iface = self._current_interface

        print(f"[AtomicNAT] Switching NAT: {old_iface!r} → {new_iface!r}", flush=True)

        # Step 1 — add new rule first (zero-gap)
        self._iptables("-A", new_iface)

        # Step 2 — brief grace window
        await asyncio.sleep(0.25)

        # Step 3 — remove old rule
        if old_iface is not None or True:   # always attempt cleanup
            self._iptables("-D", old_iface)

        self._current_interface = new_iface
        print("[AtomicNAT] NAT rule smoothly replaced. Proxy socket unaffected.", flush=True)

    def emergency_flush(self) -> None:
        """
        Flush all POSTROUTING rules for our subnet.
        Called by the circuit-breaker on_trip handler.
        """
        import sys
        if not sys.platform.startswith("linux"):
            return
        try:
            result = subprocess.run(
                ["iptables", "-t", "nat", "-S", "POSTROUTING"],
                capture_output=True, text=True, timeout=5,
            )
            for rule in result.stdout.splitlines():
                if f"{self.subnet}.0/24" in rule:
                    del_rule = rule.replace("-A ", "-D ", 1).split()
                    subprocess.run(
                        ["iptables", "-t", "nat"] + del_rule,
                        capture_output=True, timeout=5,
                    )
            self._current_interface = None
            print("[AtomicNAT] Emergency iptables flush completed.", flush=True)
        except Exception as exc:
            print(f"[AtomicNAT] Emergency flush error: {exc}", flush=True)


# --------------------------------------------------------------------------- #
#  Health Probe                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class ProbeResult:
    ok:          bool
    latency_ms:  int   = 999999
    scam_score:  int   = -1
    message:     str   = ""


class HealthProbe:
    """
    Async health probe: checks exit latency via TCP ping and
    Scamalytics fraud score for the active tunnel exit IP.

    Uses httpx with the shared connection pool.
    Falls back gracefully to urllib if httpx is absent.
    """

    LATENCY_LIMIT_MS  = 300
    SCAM_SCORE_LIMIT  = 10   # overridden per-node from config

    def __init__(self, proxy_port: int = 7928) -> None:
        self.proxy_port = proxy_port

    async def probe_exit_ip(self, exit_ip: str, remote_port: int = 443,
                            scam_limit: int = 10) -> ProbeResult:
        """Return a ProbeResult for `exit_ip`."""
        # Try testing through the proxy first (real end-to-end connectivity & latency)
        latency = await self._tcp_ping_via_proxy(timeout=3.0)
        if latency == 999999:
            # Fallback to direct check if proxy check fails
            latency = await self._tcp_ping(exit_ip, remote_port)
            
        scam    = await self._scamalytics(exit_ip)

        ok = (latency < self.LATENCY_LIMIT_MS) and (scam < scam_limit or scam == -1)
        msg = (
            f"Latency {latency}ms · Scamalytics {scam}"
            if ok else
            f"⚠ Latency {latency}ms · Scamalytics {scam} → switch needed"
        )
        return ProbeResult(ok=ok, latency_ms=latency, scam_score=scam, message=msg)

    async def _tcp_ping_via_proxy(self, target_host: str = "www.msftconnecttest.com", target_port: int = 80, timeout: float = 3.0) -> int:
        """
        Measure latency by establishing a TCP connection to target_host:target_port
        through the local HTTP proxy on 127.0.0.1:proxy_port using HTTP CONNECT.
        """
        t0 = time.monotonic()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.proxy_port),
                timeout=timeout
            )
            req = f"CONNECT {target_host}:{target_port} HTTP/1.1\r\nHost: {target_host}:{target_port}\r\n\r\n"
            writer.write(req.encode('utf-8'))
            await writer.drain()

            resp_line = await reader.readline()
            writer.close()
            await writer.wait_closed()

            if b"200" in resp_line:
                return int((time.monotonic() - t0) * 1000)
        except Exception:
            pass
        return 999999

    async def probe_proxy_connectivity(self) -> bool:
        """Quick connectivity check through the local proxy tunnel."""
        cmd = [
            "curl", "-s", "-x", f"http://127.0.0.1:{self.proxy_port}",
            "-m", "5", "http://www.msftconnecttest.com/connecttest.txt",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=6.0)
            return "Microsoft Connect Test" in stdout.decode(errors="replace")
        except Exception:
            return False

    async def _tcp_ping(self, ip: str, port: int, timeout: float = 3.0) -> int:
        t0 = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return int((time.monotonic() - t0) * 1000)
        except Exception:
            return 999999

    async def _scamalytics(self, ip: str) -> int:
        """Fetch Scamalytics fraud score; returns -1 on error."""
        import re
        url = f"https://scamalytics.com/ip/{ip}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        try:
            if HAS_HTTPX:
                async with build_httpx_client(headers=headers) as client:
                    resp = await client.get(url)
                    html = resp.text
            else:
                # Fallback: urllib in thread pool
                import urllib.request
                def _get():
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.read().decode("utf-8", errors="replace")
                html = await asyncio.to_thread(_get)

            for pat in (r'"score"\s*:\s*"(\d+)"', r"Fraud Score:\s*(\d+)"):
                m = re.search(pat, html)
                if m:
                    return int(m.group(1))
        except Exception as exc:
            print(f"[HealthProbe] Scamalytics check failed for {ip}: {exc}", flush=True)
        return -1


# --------------------------------------------------------------------------- #
#  AutoPilotWatchdog                                                            #
# --------------------------------------------------------------------------- #

class AutoPilotWatchdog:
    """
    Background guardian that runs a perpetual 60-second health cycle.

    Responsibilities
    ----------------
    1. Probe the active tunnel exit: latency + Scamalytics.
    2. If probe fails → record failure on CircuitBreaker → trigger atomic switch.
    3. If CircuitBreaker trips → iptables flush + VPN process reset.
    4. Broadcast all state-change events over WebSocket in real-time.
    5. All blocking operations are off-loaded via asyncio.to_thread().

    Integration
    -----------
    Instantiate once and call `start()` inside the asyncio event loop.
    Pass the `VPNGateProManager` reference so the watchdog can:
      - read current node IP / config
      - call `auto_reconnect()` on failure
      - call `vpn_runner.stop()` on circuit-breaker trip
    """

    HEALTH_INTERVAL = 60   # seconds between probe cycles

    def __init__(self, manager, nat_switcher: AtomicNATSwitcher) -> None:
        self.manager     = manager
        self.nat_switcher = nat_switcher
        self.probe       = HealthProbe(proxy_port=manager.config.get("proxy_port", 7928))
        self.breaker     = CircuitBreaker(failure_threshold=3, reset_timeout=120.0)
        self.breaker.on_trip = self._on_breaker_trip   # type: ignore[assignment]

        self._running  = False
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task    = asyncio.create_task(self._loop(), name="autopilot-watchdog")
        print("[AutoPilot] Watchdog started.", flush=True)
        await ws_broadcaster.broadcast({
            "event": "autopilot_started",
            "data":  {"interval_s": self.HEALTH_INTERVAL},
            "ts":    time.time(),
        })

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        print("[AutoPilot] Watchdog stopped.", flush=True)

    # ------------------------------------------------------------------ #
    #  Main loop                                                           #
    # ------------------------------------------------------------------ #

    async def _loop(self) -> None:
        await asyncio.sleep(15)   # boot grace period
        while self._running:
            try:
                await self._cycle()
            except Exception as exc:
                print(f"[AutoPilot] Unexpected error in cycle: {exc}", flush=True)
            await asyncio.sleep(self.HEALTH_INTERVAL)

    async def _cycle(self) -> None:
        """One health-check cycle."""
        mgr = self.manager

        if mgr.is_connecting:
            print("[AutoPilot] Connection in progress — skipping health cycle.", flush=True)
            return

        conn_enabled = mgr.config.get("connection_enabled", False)
        is_running   = mgr.vpn_runner.is_running()

        if not conn_enabled:
            if is_running:
                print("[AutoPilot] Connection disabled — stopping tunnel.", flush=True)
                await mgr.vpn_runner.stop()
            return

        # ① Tunnel down → auto-reconnect
        if not is_running:
            print("[AutoPilot] Tunnel down — initiating auto-reconnect.", flush=True)
            await self._broadcast_status("reconnecting", "隧道离线，正在重连...")
            await mgr.auto_reconnect()
            return

        # ② Tunnel up → probe exit quality
        node_id  = mgr.vpn_runner.current_node_id
        exit_ip  = await self._resolve_exit_ip(node_id)
        scam_lim = mgr.config.get("scamalytics_threshold", self.probe.SCAM_SCORE_LIMIT)

        await ws_broadcaster.broadcast({
            "event": "probe_started",
            "data":  {"node_id": node_id, "exit_ip": exit_ip},
            "ts":    time.time(),
        })

        result = await self.probe.probe_exit_ip(
            exit_ip, scam_limit=scam_lim
        )

        await ws_broadcaster.broadcast({
            "event": "probe_result",
            "data":  {
                "node_id":    node_id,
                "exit_ip":    exit_ip,
                "latency_ms": result.latency_ms,
                "scam_score": result.scam_score,
                "ok":         result.ok,
                "message":    result.message,
            },
            "ts": time.time(),
        })

        if result.ok:
            await self.breaker.record_success()
            async with mgr.state_lock:
                mgr.last_check_message = f"✅ {result.message}"
            print(f"[AutoPilot] Probe OK — {result.message}", flush=True)
        else:
            print(f"[AutoPilot] Probe FAIL — {result.message}", flush=True)
            mgr.failed_node_ids.add(node_id)
            await self.breaker.record_failure()

            # Breaker not yet open → attempt smooth NAT switch + reconnect
            if not self.breaker.is_open():
                await self._broadcast_status("switching", f"节点质量不足 → 切换节点: {result.message}")
                # Smooth NAT replace (does not drop proxy socket)
                await self.nat_switcher.smooth_switch()
                await mgr.auto_reconnect()
            # else: on_trip callback handles remediation

    # ------------------------------------------------------------------ #
    #  Circuit Breaker Trip Handler                                        #
    # ------------------------------------------------------------------ #

    async def _on_breaker_trip(self) -> None:
        """
        Called by CircuitBreaker when failure_threshold is reached.
        Flushes iptables and hard-resets the VPN process.
        """
        print("[AutoPilot] ⚡ Circuit breaker tripped — running emergency remediation.", flush=True)
        await self._broadcast_status("circuit_open", "熔断触发：正在执行 iptables 清理与进程重置...")

        # 1. Flush NAT rules
        await asyncio.to_thread(self.nat_switcher.emergency_flush)

        # 2. Stop VPN process
        try:
            await self.manager.vpn_runner.stop()
        except Exception as exc:
            print(f"[AutoPilot] VPN stop error during trip: {exc}", flush=True)

        # 3. Kill any orphan openvpn processes (Linux only)
        import sys
        if sys.platform.startswith("linux"):
            prefix = self.manager.netns_mgr.get_ns_prefix()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *(prefix + ["pkill", "-9", "-f", "openvpn"]),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

        await ws_broadcaster.broadcast({
            "event": "remediation_complete",
            "data":  {"action": "iptables_flush_and_vpn_reset"},
            "ts":    time.time(),
        })
        print("[AutoPilot] Remediation complete. Watchdog will retry after cool-down.", flush=True)

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    async def _resolve_exit_ip(self, node_id: str) -> str:
        """Extract the exit IP from the node database entry."""
        try:
            async with self.manager.scraper.nodes_lock:
                nodes = await asyncio.to_thread(self.manager.scraper.load_nodes_sync)
            node = next((n for n in nodes if n["id"] == node_id), None)
            if node:
                return node.get("ip", "")
        except Exception:
            pass
        return ""

    async def _broadcast_status(self, status: str, message: str) -> None:
        async with self.manager.state_lock:
            self.manager.last_check_message = message
        await ws_broadcaster.broadcast({
            "event": "status_update",
            "data":  {"status": status, "message": message},
            "ts":    time.time(),
        })


# --------------------------------------------------------------------------- #
#  Atomic config write helper (module-level convenience)                       #
# --------------------------------------------------------------------------- #

def write_json_atomic(path: Path, payload) -> None:
    """
    Write JSON via atomic tempfile → fsync → os.replace.
    Prevents partial writes from corrupting state on power-loss.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)


# --------------------------------------------------------------------------- #
#  Module-level exports                                                        #
# --------------------------------------------------------------------------- #

__all__ = [
    "install_uvloop_policy",
    "build_httpx_client",
    "WSBroadcaster",
    "ws_broadcaster",
    "CircuitBreaker",
    "CBState",
    "AtomicNATSwitcher",
    "HealthProbe",
    "ProbeResult",
    "AutoPilotWatchdog",
    "write_json_atomic",
]
