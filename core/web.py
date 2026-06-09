import os
import json
import time
import mimetypes
import asyncio
import posixpath
import secrets
import hashlib
import base64
from pathlib import Path

from core.engine import ws_broadcaster

MAX_HEADER_BYTES = 32 * 1024
MAX_BODY_BYTES = 256 * 1024
HEADER_TIMEOUT = 5
BODY_TIMEOUT = 10
STATUS_REASONS = {
    200: "OK",
    302: "Found",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    413: "Payload Too Large",
    500: "Internal Server Error",
}

class AsyncWebServer:
    def __init__(self, manager, host="0.0.0.0", port=8787):
        self.manager = manager
        self.host = host
        self.port = port
        self.server = None
        self.sessions = {} # token -> expiry_timestamp
        
        self.web_dir = Path(__file__).resolve().parent.parent / "web"

    async def start(self):
        self.server = await asyncio.start_server(
            self.handle_client, self.host, self.port
        )
        print(f"[Web] Web Dashboard Server listening on http://{self.host}:{self.port}/", flush=True)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def handle_client(self, reader, writer):
        try:
            # Read HTTP request header
            header_data = b""
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=HEADER_TIMEOUT)
                if not chunk:
                    break
                header_data += chunk
                if len(header_data) > MAX_HEADER_BYTES:
                    self.send_json(writer, {"error": "Request header too large"}, 413)
                    return
                if b"\r\n\r\n" in header_data:
                    break

            if not header_data:
                await self.close_writer(writer)
                return
            if b"\r\n\r\n" not in header_data:
                self.send_json(writer, {"error": "Malformed request"}, 400)
                return

            header_part, body_part = header_data.split(b"\r\n\r\n", 1)
            lines = header_part.decode("utf-8", errors="replace").split("\r\n")
            request_line = lines[0].split()
            if len(request_line) < 3:
                self.send_json(writer, {"error": "Malformed request"}, 400)
                return

            method, path, _ = request_line
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            # Read remaining body if Content-Length specified
            try:
                content_length = int(headers.get("content-length", 0))
            except ValueError:
                self.send_json(writer, {"error": "Invalid Content-Length"}, 400)
                return
            if content_length < 0 or content_length > MAX_BODY_BYTES:
                self.send_json(writer, {"error": "Request body too large"}, 413)
                return
            body = body_part
            if len(body) < content_length:
                body += await asyncio.wait_for(
                    reader.readexactly(content_length - len(body)),
                    timeout=BODY_TIMEOUT
                )

            # Parse cookies
            cookie_header = headers.get("cookie", "")
            cookies = {}
            if cookie_header:
                for item in cookie_header.split(";"):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        cookies[k.strip()] = v.strip()

            # Handle routing
            await self.route_request(reader, writer, method, path, headers, cookies, body)
        except Exception as e:
            print(f"[Web] Error handling request: {e}", flush=True)
            try:
                await self.close_writer(writer)
            except Exception:
                pass

    async def route_request(self, reader, writer, method, path, headers, cookies, body):
        # 1. Clean Path
        clean_path = path.split("?")[0]
        secret_path = self.manager.config.get("secret_path", "")
        prefix = f"/{secret_path}" if secret_path else ""

        # Redirect root paths to secret path or handle index
        if clean_path in ("/", "/index.html"):
            if prefix:
                self.send_redirect(writer, f"{prefix}/")
                return
            else:
                await self.serve_dashboard_or_login(writer, cookies)
                return
        
        # Match prefixed dashboard paths
        if prefix and clean_path == f"{prefix}/":
            await self.serve_dashboard_or_login(writer, cookies)
            return

        if prefix and clean_path.startswith(f"{prefix}/web/"):
            # Strip prefix and serve static web assets
            relative_path = clean_path.replace(f"{prefix}/web/", "", 1)
            await self.serve_static_file(writer, relative_path)
            return

        # Check APIs
        api_path = clean_path
        if prefix:
            if clean_path.startswith(f"{prefix}/api/"):
                api_path = clean_path.replace(prefix, "", 1)
            else:
                self.send_json(writer, {"error": "Unauthorized"}, 401)
                return

        # API Handlers
        if api_path == "/api/login" and method == "POST":
            await self.handle_login(writer, body)
        elif api_path == "/api/ws" and method == "GET":
            # WebSocket upgrade — auth via cookie or ?token= param
            token_param = ""
            if "?" in path:
                qs = path.split("?", 1)[1]
                for part in qs.split("&"):
                    if part.startswith("token="):
                        token_param = part[6:]
            fake_cookies = dict(cookies)
            if token_param:
                fake_cookies["session"] = token_param
            if not self.is_authorized(fake_cookies):
                self.send_json(writer, {"error": "Unauthorized"}, 401)
            else:
                await self._handle_websocket(reader, writer, headers)
        elif api_path == "/api/nodes" and method == "GET":
            if not self.is_authorized(cookies):
                self.send_json(writer, {"error": "Unauthorized"}, 401)
            else:
                await self.handle_get_nodes(writer)
        elif api_path == "/api/connect" and method == "POST":
            if not self.is_authorized(cookies):
                self.send_json(writer, {"error": "Unauthorized"}, 401)
            else:
                await self.handle_connect(writer, body)
        elif api_path == "/api/disconnect" and method == "POST":
            if not self.is_authorized(cookies):
                self.send_json(writer, {"error": "Unauthorized"}, 401)
            else:
                await self.handle_disconnect(writer)
        elif api_path == "/api/settings" and method == "POST":
            if not self.is_authorized(cookies):
                self.send_json(writer, {"error": "Unauthorized"}, 401)
            else:
                await self.handle_save_settings(writer, body)
        elif api_path == "/api/test_node" and method == "POST":
            if not self.is_authorized(cookies):
                self.send_json(writer, {"error": "Unauthorized"}, 401)
            else:
                await self.handle_test_node(writer, body)
        else:
            self.send_json(writer, {"error": "Not Found"}, 404)

    def is_authorized(self, cookies):
        # If no password set, bypass authentication
        pwd = self.manager.config.get("password")
        if not pwd:
            return True
            
        token = cookies.get("session")
        if not token:
            return False
        matched_token = next((saved for saved in self.sessions if secrets.compare_digest(saved, token)), None)
        if not matched_token:
            return False
        
        # Check expiry
        if self.sessions[matched_token] < time.time():
            del self.sessions[matched_token]
            return False
            
        return True

    async def serve_dashboard_or_login(self, writer, cookies):
        if self.is_authorized(cookies):
            await self.serve_static_file(writer, "index.html")
        else:
            await self.serve_static_file(writer, "login.html")

    async def serve_static_file(self, writer, filename):
        normalized = posixpath.normpath("/" + filename).lstrip("/")
        file_path = self.web_dir / normalized
        # Security check to prevent directory traversal
        try:
            resolved_path = file_path.resolve()
            web_root = self.web_dir.resolve()
            if resolved_path != web_root and web_root not in resolved_path.parents:
                self.send_json(writer, {"error": "Forbidden"}, 403)
                return
        except Exception:
            self.send_json(writer, {"error": "Not Found"}, 404)
            return

        if not resolved_path.exists() or resolved_path.is_dir():
            self.send_json(writer, {"error": "Not Found"}, 404)
            return

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(str(resolved_path))
        if not mime_type:
            mime_type = "application/octet-stream"

        try:
            content = await asyncio.to_thread(resolved_path.read_bytes)
            self.send_bytes(writer, content, mime_type)
        except Exception as e:
            self.send_json(writer, {"error": f"Internal Server Error: {e}"}, 500)

    async def handle_login(self, writer, body):
        try:
            payload = json.loads(body.decode("utf-8"))
            username = payload.get("username")
            password = payload.get("password")
            
            cfg_user = self.manager.config.get("username", "admin")
            cfg_pass = self.manager.config.get("password", "")
            
            if (
                isinstance(username, str)
                and isinstance(password, str)
                and secrets.compare_digest(username, cfg_user)
                and secrets.compare_digest(password, cfg_pass)
            ):
                # Generate session token
                token = secrets.token_urlsafe(32)
                now = time.time()
                self.sessions = {k: v for k, v in self.sessions.items() if v > now}
                self.sessions[token] = time.time() + 24 * 3600 # 24 hours expiry
                
                cookie_header = f"session={token}; Path=/; HttpOnly; SameSite=Lax"
                self.send_json(writer, {"success": True}, headers={"Set-Cookie": cookie_header})
            else:
                self.send_json(writer, {"success": False, "error": "用户名或密码错误"}, 400)
        except Exception as e:
            self.send_json(writer, {"error": str(e)}, 400)

    async def handle_get_nodes(self, writer):
        nodes = await self.manager.get_filtered_nodes()
        state = self.manager.get_state_data()
        self.send_json(writer, {"nodes": nodes, "state": state})

    async def handle_connect(self, writer, body):
        try:
            payload = json.loads(body.decode("utf-8"))
            node_id = payload.get("node_id")
            if not node_id:
                self.send_json(writer, {"error": "Missing node_id"}, 400)
                return
            
            # Trigger connection task in background
            asyncio.create_task(self.manager.connect_node(node_id))
            self.send_json(writer, {"success": True, "message": "正在发起连接"})
        except Exception as e:
            self.send_json(writer, {"error": str(e)}, 400)

    async def handle_disconnect(self, writer):
        asyncio.create_task(self.manager.disconnect_node())
        self.send_json(writer, {"success": True, "message": "正在断开连接"})

    async def handle_save_settings(self, writer, body):
        try:
            payload = json.loads(body.decode("utf-8"))
            
            # Check if username or password is being changed
            credentials_changed = False
            new_username = payload.get("username")
            new_password = payload.get("password")
            
            if new_username is not None or new_password is not None:
                # Validate
                if new_username is not None and not new_username.strip():
                    self.send_json(writer, {"error": "用户名不能为空"}, 400)
                    return
                if new_password is not None and not new_password.strip():
                    self.send_json(writer, {"error": "密码不能为空"}, 400)
                    return
                credentials_changed = True
            
            await self.manager.update_settings(payload)
            
            if credentials_changed:
                # Clear all active sessions to force re-login
                self.sessions.clear()
                # Clear cookie by setting expired session cookie
                cookie_header = "session=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Lax"
                self.send_json(
                    writer, 
                    {"success": True, "message": "凭证已更新，请重新登录", "require_relogin": True}, 
                    headers={"Set-Cookie": cookie_header}
                )
            else:
                self.send_json(writer, {"success": True, "message": "设置已保存"})
        except Exception as e:
            self.send_json(writer, {"error": str(e)}, 400)

    # ------------------------------------------------------------------ #
    #  WebSocket endpoint                                                  #
    # ------------------------------------------------------------------ #

    async def _handle_websocket(self, reader, writer, headers):
        """
        Minimal RFC-6455 WebSocket server implementation.
        - Performs HTTP→WS upgrade handshake.
        - Subscribes to WSBroadcaster for server→client push.
        - Reads client frames for command dispatch.
        """
        ws_key = headers.get("sec-websocket-key", "")
        if not ws_key:
            self.send_json(writer, {"error": "Bad WebSocket handshake"}, 400)
            return

        # Build upgrade response
        accept = base64.b64encode(
            hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        try:
            writer.write(handshake.encode())
            await writer.drain()
        except Exception:
            return

        # Subscribe to broadcaster
        queue = await ws_broadcaster.subscribe()

        # Send full state snapshot immediately
        try:
            await self._ws_send_text(writer, json.dumps({
                "event": "state_snapshot",
                "data":  self.manager.get_state_data(),
                "ts":    time.time(),
            }))
        except Exception:
            pass

        async def sender():
            """Forward broadcast messages to this WebSocket client."""
            try:
                while True:
                    msg = await queue.get()
                    await self._ws_send_text(writer, msg)
            except Exception:
                pass

        async def receiver():
            """Read frames from client and dispatch commands."""
            try:
                while True:
                    frame = await self._ws_read_frame(reader)
                    if frame is None:
                        break   # connection closed
                    try:
                        cmd_obj = json.loads(frame)
                        await self._dispatch_ws_cmd(cmd_obj)
                    except Exception:
                        pass
            except Exception:
                pass

        sender_task   = asyncio.create_task(sender())
        receiver_task = asyncio.create_task(receiver())
        try:
            await asyncio.wait(
                [sender_task, receiver_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            sender_task.cancel()
            receiver_task.cancel()
            await ws_broadcaster.unsubscribe(queue)
            try:
                writer.close()
            except Exception:
                pass

    async def _dispatch_ws_cmd(self, obj: dict):
        cmd = obj.get("cmd", "")
        if cmd == "cmd_connect":
            node_id = obj.get("node_id", "")
            if node_id:
                asyncio.create_task(self.manager.connect_node(node_id))
        elif cmd == "cmd_disconnect":
            asyncio.create_task(self.manager.disconnect_node())
        elif cmd == "cmd_test_node":
            node_id = obj.get("node_id", "")
            if node_id:
                asyncio.create_task(self.manager.test_node_latency(node_id))
        elif cmd == "cmd_request_snapshot":
            pass   # snapshot will be broadcast on next state change

    @staticmethod
    async def _ws_send_text(writer, text: str):
        """Encode and send a WebSocket text frame."""
        payload = text.encode("utf-8")
        length  = len(payload)
        if length < 126:
            header = bytes([0x81, length])
        elif length < 65536:
            header = bytes([0x81, 126]) + length.to_bytes(2, "big")
        else:
            header = bytes([0x81, 127]) + length.to_bytes(8, "big")
        writer.write(header + payload)
        await writer.drain()

    @staticmethod
    async def _ws_read_frame(reader) -> "str | None":
        """Read a single unmasked/masked WebSocket text frame from client."""
        try:
            header = await asyncio.wait_for(reader.readexactly(2), timeout=60)
            opcode = header[0] & 0x0F
            if opcode == 0x8:   # close
                return None
            masked  = bool(header[1] & 0x80)
            length  = header[1] & 0x7F
            if length == 126:
                ext = await asyncio.wait_for(reader.readexactly(2), timeout=10)
                length = int.from_bytes(ext, "big")
            elif length == 127:
                ext = await asyncio.wait_for(reader.readexactly(8), timeout=10)
                length = int.from_bytes(ext, "big")
            if length > 65536:
                return None
            mask_key = b""
            if masked:
                mask_key = await asyncio.wait_for(reader.readexactly(4), timeout=10)
            data = await asyncio.wait_for(reader.readexactly(length), timeout=10)
            if masked:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
            return data.decode("utf-8", errors="replace")
        except Exception:
            return None

    async def handle_test_node(self, writer, body):
        try:
            payload = json.loads(body.decode("utf-8"))
            node_id = payload.get("node_id")
            if not node_id:
                self.send_json(writer, {"error": "Missing node_id"}, 400)
                return
                
            asyncio.create_task(self.manager.test_node_latency(node_id))
            self.send_json(writer, {"success": True, "message": "已开始检测节点延迟"})
        except Exception as e:
            self.send_json(writer, {"error": str(e)}, 400)

    def send_bytes(self, writer, body, content_type, status=200, headers=None):
        reason = STATUS_REASONS.get(status, "OK")
        res = f"HTTP/1.1 {status} {reason}\r\n"
        res += f"Content-Type: {content_type}\r\n"
        res += f"Content-Length: {len(body)}\r\n"
        res += "Cache-Control: no-store, no-cache, must-revalidate\r\n"
        res += "X-Content-Type-Options: nosniff\r\n"
        if headers:
            for k, v in headers.items():
                res += f"{k}: {v}\r\n"
        res += "\r\n"
        
        try:
            writer.write(res.encode("utf-8") + body)
            asyncio.create_task(self.safe_drain_close(writer))
        except Exception:
            pass

    async def safe_drain_close(self, writer):
        try:
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def close_writer(self, writer):
        writer.close()
        await writer.wait_closed()

    def send_json(self, writer, data, status=200, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(writer, body, "application/json; charset=utf-8", status, headers)

    def send_redirect(self, writer, location):
        res = f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n"
        try:
            writer.write(res.encode("utf-8"))
            asyncio.create_task(self.safe_drain_close(writer))
        except Exception:
            pass
