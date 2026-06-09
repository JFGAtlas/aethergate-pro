import asyncio
import socket
import urllib.parse

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 15
MAX_HTTP_LINE_BYTES = 8192
MAX_HTTP_HEADER_BYTES = 64 * 1024

class MixedProxyServer:
    def __init__(self, host="0.0.0.0", port=7928):
        self.host = host
        self.port = port
        self.server = None

    async def start(self):
        """Start the async mixed proxy server."""
        self.server = await asyncio.start_server(
            self.handle_connection, self.host, self.port, reuse_port=True
        )
        print(f"[Proxy] Mixed SOCKS5/HTTP Proxy listening on {self.host}:{self.port}", flush=True)

    async def stop(self):
        """Stop the proxy server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            print("[Proxy] Proxy server stopped.", flush=True)

    async def handle_connection(self, reader, writer):
        """Sniff the first byte and dispatch to SOCKS5 or HTTP handler."""
        try:
            # Read first byte to determine protocol
            first_byte = await asyncio.wait_for(reader.readexactly(1), timeout=READ_TIMEOUT)
            if not first_byte:
                writer.close()
                return

            if first_byte == b'\x05':
                await self.handle_socks5(reader, writer)
            else:
                await self.handle_http(first_byte, reader, writer)
        except Exception as e:
            # Silently close on connection errors
            try:
                writer.close()
            except Exception:
                pass

    async def handle_socks5(self, reader, writer):
        """Handle SOCKS5 handshake and connection tunneling."""
        try:
            # 1. Read greeting methods
            header = await asyncio.wait_for(reader.readexactly(1), timeout=READ_TIMEOUT)
            nmethods = header[0]
            await asyncio.wait_for(reader.readexactly(nmethods), timeout=READ_TIMEOUT)
            
            # Respond: SOCKS5, No Authentication
            writer.write(b'\x05\x00')
            await writer.drain()

            # 2. Read request: VER, CMD, RSV, ATYP
            req_header = await asyncio.wait_for(reader.readexactly(4), timeout=READ_TIMEOUT)
            cmd = req_header[1]
            atyp = req_header[3]

            if cmd != 0x01: # Only support CONNECT command
                writer.write(b'\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00') # Command not supported
                await writer.drain()
                writer.close()
                return

            # Read target address
            if atyp == 0x01: # IPv4
                addr_bytes = await asyncio.wait_for(reader.readexactly(4), timeout=READ_TIMEOUT)
                target_host = socket.inet_ntoa(addr_bytes)
            elif atyp == 0x03: # Domain name
                len_byte = await asyncio.wait_for(reader.readexactly(1), timeout=READ_TIMEOUT)
                domain_len = len_byte[0]
                domain_bytes = await asyncio.wait_for(reader.readexactly(domain_len), timeout=READ_TIMEOUT)
                target_host = domain_bytes.decode('utf-8')
            elif atyp == 0x04: # IPv6
                addr_bytes = await asyncio.wait_for(reader.readexactly(16), timeout=READ_TIMEOUT)
                target_host = socket.inet_ntop(socket.AF_INET6, addr_bytes)
            else:
                writer.write(b'\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00') # Address type not supported
                await writer.drain()
                writer.close()
                return

            # Read target port
            port_bytes = await asyncio.wait_for(reader.readexactly(2), timeout=READ_TIMEOUT)
            target_port = int.from_bytes(port_bytes, 'big')
            if not self.is_valid_port(target_port):
                writer.write(b'\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00')
                await writer.drain()
                writer.close()
                return

            try:
                target_reader, target_writer = await asyncio.wait_for(
                    asyncio.open_connection(target_host, target_port),
                    timeout=CONNECT_TIMEOUT
                )
            except Exception as e:
                print(f"[Proxy Debug] SOCKS5 Connect to {target_host}:{target_port} failed: {e}", flush=True)
                writer.write(b'\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00') # Host unreachable
                await writer.drain()
                writer.close()
                return

            # Connection success response
            writer.write(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
            await writer.drain()

            # 4. Bridge connection
            await self.bridge_connections(reader, writer, target_reader, target_writer)

        except Exception:
            writer.close()

    async def handle_http(self, first_byte, reader, writer):
        """Handle HTTP proxy connection (both CONNECT tunneling and GET/POST relaying)."""
        try:
            # Read remainder of the first request line
            request_line = first_byte
            while True:
                char = await asyncio.wait_for(reader.readexactly(1), timeout=READ_TIMEOUT)
                request_line += char
                if len(request_line) > MAX_HTTP_LINE_BYTES:
                    writer.write(b"HTTP/1.1 414 URI Too Long\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return
                if request_line.endswith(b'\r\n'):
                    break

            req_str = request_line.decode('utf-8', errors='replace').strip()
            parts = req_str.split()
            if len(parts) < 2:
                writer.close()
                return

            method, target = parts[0], parts[1]

            if method.upper() == 'CONNECT':
                # CONNECT host:port HTTP/1.1
                target_host, target_port = self.parse_connect_target(target)
                if not target_host or not self.is_valid_port(target_port):
                    writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return
                
                # Consume headers
                header_bytes = 0
                while True:
                    line = await self.read_line(reader)
                    header_bytes += len(line)
                    if header_bytes > MAX_HTTP_HEADER_BYTES:
                        writer.write(b"HTTP/1.1 431 Request Header Fields Too Large\r\nContent-Length: 0\r\n\r\n")
                        await writer.drain()
                        writer.close()
                        return
                    if not line or line in (b'\r\n', b'\n'):
                        break

                try:
                    target_reader, target_writer = await asyncio.wait_for(
                        asyncio.open_connection(target_host, target_port),
                        timeout=CONNECT_TIMEOUT
                    )
                except Exception as e:
                    print(f"[Proxy Debug] HTTP CONNECT to {target_host}:{target_port} failed: {e}", flush=True)
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return

                # Send established response
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

                # Bridge
                await self.bridge_connections(reader, writer, target_reader, target_writer)
            else:
                # Regular GET/POST proxy request: GET http://host/path HTTP/1.1
                parsed = urllib.parse.urlsplit(target)
                target_host = parsed.hostname
                target_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                if parsed.scheme not in ("http", "https") or not target_host or not self.is_valid_port(target_port):
                    writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return
                
                # Reconstruct path and request headers
                path = parsed.path
                if parsed.query:
                    path += "?" + parsed.query
                if not path:
                    path = "/"

                rebuilt_req = f"{method} {path} HTTP/1.1\r\n"
                
                # Consume and rebuild remaining headers
                header_bytes = 0
                while True:
                    line = await self.read_line(reader)
                    if not line:
                        break
                    header_bytes += len(line)
                    if header_bytes > MAX_HTTP_HEADER_BYTES:
                        writer.write(b"HTTP/1.1 431 Request Header Fields Too Large\r\nContent-Length: 0\r\n\r\n")
                        await writer.drain()
                        writer.close()
                        return
                    line_str = line.decode('utf-8', errors='replace')
                    rebuilt_req += line_str
                    if line in (b'\r\n', b'\n'):
                        break

                try:
                    target_reader, target_writer = await asyncio.wait_for(
                        asyncio.open_connection(target_host, target_port),
                        timeout=CONNECT_TIMEOUT
                    )
                except Exception as e:
                    print(f"[Proxy Debug] HTTP Relaying to {target_host}:{target_port} failed: {e}", flush=True)
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return

                # Write request to target
                target_writer.write(rebuilt_req.encode('utf-8'))
                await target_writer.drain()

                # Bridge
                await self.bridge_connections(reader, writer, target_reader, target_writer)

        except Exception:
            writer.close()

    async def read_line(self, reader):
        """Helper to read an HTTP header line."""
        line = b''
        try:
            while True:
                char = await asyncio.wait_for(reader.readexactly(1), timeout=READ_TIMEOUT)
                line += char
                if len(line) > MAX_HTTP_LINE_BYTES:
                    return b''
                if line.endswith(b'\n'):
                    break
            return line
        except Exception:
            return b''

    def parse_connect_target(self, target):
        if target.startswith("["):
            end = target.find("]")
            if end == -1:
                return "", 0
            host = target[1:end]
            port_text = target[end + 2:] if target[end + 1:end + 2] == ":" else "443"
        else:
            host, _, port_text = target.rpartition(":")
            if not host:
                host = target
                port_text = "443"
        try:
            return host, int(port_text)
        except ValueError:
            return "", 0

    def is_valid_port(self, port):
        return isinstance(port, int) and 0 < port <= 65535

    async def bridge_connections(self, r1, w1, r2, w2):
        """Asynchronously pipe data bidirectionally between client and target."""
        async def pipe(reader, writer):
            try:
                while True:
                    data = await reader.read(8192)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        # Run both directions in parallel
        await asyncio.gather(pipe(r1, w2), pipe(r2, w1), return_exceptions=True)
