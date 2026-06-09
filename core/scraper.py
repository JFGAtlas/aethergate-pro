import os
import csv
import json
import time
import base64
import re
import asyncio
import urllib.request
import urllib.parse
import tempfile
from pathlib import Path

class CandidateScraper:
    def __init__(self, data_dir="/opt/vpngate-pro/vpngate_data", api_url="https://www.vpngate.net/api/iphone/"):
        self.data_dir = Path(data_dir)
        self.api_url = api_url
        self.cache_file = self.data_dir / "ip_cache.json"
        self.nodes_file = self.data_dir / "nodes.json"
        
        self.cache_lock = asyncio.Lock()
        self.nodes_lock = asyncio.Lock()
        self.ensure_dirs()

    def ensure_dirs(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load_cache(self) -> dict:
        """Load IP Cache file synchronously (will be run in thread)."""
        if self.cache_file.exists():
            try:
                return json.loads(self.cache_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def save_cache(self, cache: dict):
        """Save IP Cache file synchronously."""
        self.write_json_atomic(self.cache_file, cache)

    def load_nodes_sync(self) -> list:
        """Load nodes.json synchronously."""
        if self.nodes_file.exists():
            try:
                return json.loads(self.nodes_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def save_nodes_sync(self, nodes: list):
        """Save nodes.json synchronously."""
        self.write_json_atomic(self.nodes_file, nodes)

    def write_json_atomic(self, path: Path, payload):
        """Write JSON via atomic replace so interrupted writes do not corrupt state."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = Path(tmp.name)
            tmp_path.replace(path)
        except Exception as e:
            print(f"[Scraper] Failed to save {path.name}: {e}", flush=True)

    def _http_request_blocking(self, url, data=None, headers=None, method=None, timeout=10) -> bytes:
        """Synchronous HTTP Request to be run in asyncio thread pool."""
        if headers is None:
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()

    async def fetch_bytes(self, url, data=None, headers=None, method=None, timeout=10) -> bytes:
        """Wrapper around blocking HTTP request to run it in a thread."""
        return await asyncio.to_thread(self._http_request_blocking, url, data, headers, method, timeout)

    async def scrape_candidates(self) -> list:
        """Fetch candidates from VPNGate and parse the CSV."""
        print("[Scraper] Fetching fresh VPNGate nodes list...", flush=True)
        try:
            raw_data = await self.fetch_bytes(self.api_url, timeout=15)
            text = raw_data.decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[Scraper] Failed to fetch VPNGate nodes: {e}", flush=True)
            return []

        lines = text.strip().splitlines()
        if len(lines) < 3:
            return []

        # Find header line starting with #HostName or similar
        header_idx = -1
        for idx, line in enumerate(lines):
            if line.startswith("#HostName") or line.startswith("#"):
                header_idx = idx
                break
        
        if header_idx == -1:
            print("[Scraper] CSV Header not found in VPNGate API response", flush=True)
            return []

        headers = [h.strip("#").strip() for h in lines[header_idx].split(",")]
        
        candidates = []
        reader = csv.reader(lines[header_idx + 1:])
        for row in reader:
            if not row or len(row) < len(headers) or row[0].startswith("*"):
                continue
                
            node = dict(zip(headers, row))
            
            try:
                config_b64 = node.get("OpenVPN_ConfigData_Base64", "")
                config_text = base64.b64decode(config_b64).decode("utf-8", errors="replace")
            except Exception:
                continue

            # Extract actual remote port and protocol from OpenVPN config
            port = node.get("Port")
            if not port:
                m_remote = re.search(r"remote\s+\S+\s+(\d+)", config_text)
                if m_remote:
                    port = int(m_remote.group(1))
                else:
                    port = 1194
            else:
                try:
                    port = int(port)
                except ValueError:
                    port = 1194

            # Parse node fields
            node_id = f"{node.get('CountryShort')}_{node.get('IP')}_{port}_{node.get('LogTime', int(time.time()))}"

            proto = "udp"
            m = re.search(r"proto\s+(\w+)", config_text)
            if m:
                proto = m.group(1).lower()

            candidates.append({
                "id": node_id,
                "ip": node.get("IP"),
                "remote_host": (node.get("HostName") or "node") + ".opengw.net",
                "remote_port": port,
                "proto": proto,
                "country": node.get("CountryShort"),
                "ping": node.get("Ping"),
                "score": node.get("Score"),
                "config_text": config_text,
                "probe_status": "not_checked",
                "probe_message": "未检测",
                "probed_at": 0,
                "latency_ms": 999999,
                "owner": "",
                "asn": "",
                "as_name": "",
                "location": "",
                "ip_type": "untested",
                "quality": "normal",
                "scamalytics_score": None
            })
            
        print(f"[Scraper] Successfully parsed {len(candidates)} candidates from VPNGate.", flush=True)
        return candidates

    async def get_scamalytics_score(self, ip: str) -> int:
        """Fetch Scamalytics Fraud Score for an IP."""
        url = f"https://scamalytics.com/ip/{ip}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive"
        }
        try:
            raw_html = await self.fetch_bytes(url, headers=headers, timeout=10)
            html = raw_html.decode("utf-8", errors="replace")
            m = re.search(r"\"score\"\s*:\s*\"(\d+)\"", html)
            if m:
                return int(m.group(1))
            m = re.search(r"Fraud Score:\s*(\d+)", html)
            if m:
                return int(m.group(1))
        except Exception as e:
            print(f"[Scamalytics] Error checking {ip}: {e}", flush=True)
        return -1

    async def enrich_batch_ip_api(self, ips: list) -> dict:
        """Geolocate up to 100 IPs in batch via ip-api.com."""
        if not ips:
            return {}
            
        url = "http://ip-api.com/batch?lang=zh-CN&fields=status,message,query,country,regionName,city,isp,org,as,asname,proxy,hosting,mobile"
        payload = json.dumps(ips).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
        
        try:
            raw_res = await self.fetch_bytes(url, data=payload, headers=headers, method="POST", timeout=15)
            data = json.loads(raw_res.decode("utf-8", errors="replace"))
            results = {}
            for item in data:
                if item.get("status") != "success":
                    continue
                ip = item.get("query")
                if not ip:
                    continue
                
                # Determine IP type
                ip_type = "residential"
                if item.get("mobile"):
                    ip_type = "mobile"
                elif item.get("proxy"):
                    ip_type = "proxy"
                elif item.get("hosting"):
                    ip_type = "hosting"

                quality = "normal"
                if item.get("proxy"):
                    quality = "proxy"
                elif item.get("hosting"):
                    quality = "datacenter"
                elif item.get("mobile"):
                    quality = "mobile"

                loc = " ".join(part for part in [item.get("country"), item.get("regionName"), item.get("city")] if part)
                results[ip] = {
                    "owner": item.get("org") or item.get("isp") or "",
                    "asn": item.get("as") or "",
                    "as_name": item.get("asname") or "",
                    "location": loc,
                    "ip_type": ip_type,
                    "quality": quality,
                    "cached_at": time.time()
                }
            return results
        except Exception as e:
            print(f"[Scraper] Geolocation batch query failed: {e}", flush=True)
            return {}

    async def enrich_nodes_info(self, nodes: list, fetch_scamalytics: bool = True):
        """Enrich a list of nodes with IP type and Scamalytics score from cache or external APIs."""
        async with self.cache_lock:
            cache = await asyncio.to_thread(self.load_cache)
            
        ips_to_query = []
        now = time.time()
        
        for n in nodes:
            ip = n.get("ip")
            if not ip:
                continue
            
            # Check local cache first (cache TTL: 7 days)
            if ip in cache and now - cache[ip].get("cached_at", 0) < 7 * 24 * 3600:
                cached = cache[ip]
                n["owner"] = cached.get("owner", "")
                n["asn"] = cached.get("asn", "")
                n["as_name"] = cached.get("as_name", "")
                n["location"] = cached.get("location", "")
                n["ip_type"] = cached.get("ip_type", "untested")
                n["quality"] = cached.get("quality", "normal")
                
                cached_score = cached.get("scamalytics_score")
                if cached_score is not None or not fetch_scamalytics:
                    n["scamalytics_score"] = cached_score
                else:
                    if ip not in ips_to_query:
                        ips_to_query.append(ip)
            else:
                if ip not in ips_to_query:
                    ips_to_query.append(ip)

        if not ips_to_query:
            return

        # Query geolocation in batches of 100
        new_entries = {}
        for i in range(0, len(ips_to_query), 100):
            chunk = ips_to_query[i:i+100]
            chunk_results = await self.enrich_batch_ip_api(chunk)
            new_entries.update(chunk_results)

        # For newly queried IPs, also fetch Scamalytics score asynchronously if requested
        if fetch_scamalytics:
            for ip, entry in new_entries.items():
                score = await self.get_scamalytics_score(ip)
                entry["scamalytics_score"] = score
                await asyncio.sleep(1.0) # Rate limit
        else:
            for ip, entry in new_entries.items():
                entry["scamalytics_score"] = None

        if new_entries:
            async with self.cache_lock:
                current_cache = await asyncio.to_thread(self.load_cache)
                current_cache.update(new_entries)
                await asyncio.to_thread(self.save_cache, current_cache)

            # Copy newly fetched values to nodes list
            for n in nodes:
                ip = n.get("ip")
                if ip in new_entries:
                    cached = new_entries[ip]
                    n["owner"] = cached.get("owner", "")
                    n["asn"] = cached.get("asn", "")
                    n["as_name"] = cached.get("as_name", "")
                    n["location"] = cached.get("location", "")
                    n["ip_type"] = cached.get("ip_type", "untested")
                    n["quality"] = cached.get("quality", "normal")
                    n["scamalytics_score"] = cached.get("scamalytics_score")
