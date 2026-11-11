"""Local control channel: state over SSE (GET /events) and commands over POST /cmd. No dependencies.

Every request must carry the per-start secret in the `X-Voz-Token` header and a
localhost Host header. The plugin creates the secret at each start and passes it
to the daemon on standard input, so no other local program or web page can drive
the daemon.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("voz.ui")
TOKEN_HEADER = "x-voz-token"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 64 * 1024


def host_is_local(host: str, port: int) -> bool:
    """True when the Host header names this machine on this port (blocks DNS rebinding)."""
    name, sep, given = host.strip().lower().rpartition(":")
    return bool(sep) and name in LOCAL_HOSTS and given == str(port)


class UI:
    def __init__(self, port: int, on_cmd: Callable[[dict], Awaitable[Any]], token: str):
        if not token:
            raise ValueError("the control channel needs a non-empty token")
        self.port = port
        self.on_cmd = on_cmd
        self._token = token.encode()
        self._clients: set[asyncio.StreamWriter] = set()
        self._server: asyncio.AbstractServer | None = None
        self._history: list[dict] = []  # recent events, for clients that connect later

    async def start(self) -> None:
        # On a mod reload the previous daemon may still be releasing the port: wait up to 15 s.
        for attempt in range(30):
            try:
                self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
                break
            except OSError as e:
                if attempt == 29:
                    raise RuntimeError(f"port {self.port} is in use ({e.strerror}); close the other voz-daemon") from e
                if attempt == 0:
                    log.info("port %d in use; waiting for the previous daemon to exit...", self.port)
                await asyncio.sleep(0.5)
        log.info("control channel on 127.0.0.1:%d", self.port)

    async def close(self) -> None:
        for w in list(self._clients):
            w.close()
        if self._server:
            self._server.close()

    def emit(self, type_: str, **data: Any) -> None:
        ev = {"type": type_, **data}
        if type_ != "level":
            self._history.append(ev)
            self._history = self._history[-300:]
        line = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()
        for w in list(self._clients):
            try:
                w.write(line)
            except Exception:  # noqa: BLE001
                self._clients.discard(w)

    def authorized(self, headers: dict[str, str]) -> bool:
        """The request names localhost and carries the per-start secret."""
        if not host_is_local(headers.get("host", ""), self.port):
            return False
        return hmac.compare_digest(headers.get(TOKEN_HEADER, "").encode(), self._token)

    # ---------- Minimal HTTP ----------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except Exception:  # noqa: BLE001
            writer.close(); return
        lines = head.decode(errors="replace").split("\r\n")
        try:
            method, path, _ = lines[0].split(" ", 2)
        except ValueError:
            writer.close(); return
        headers = {k.lower().strip(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if ":" in l)}
        if not self.authorized(headers):
            await self._finish(writer, 403, "text/plain", b"forbidden")
            return
        try:
            n = int(headers.get("content-length", "0") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            await self._finish(writer, 413, "text/plain", b"bad body length")
            return
        body = await reader.readexactly(n) if n else b""

        if method == "GET" and path == "/events":
            await self._stream(writer)
        elif method == "POST" and path == "/cmd":
            await self._command(writer, body)
        else:
            await self._finish(writer, 404, "text/plain", b"nothing here")

    async def _command(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        try:
            cmd = json.loads(body or b"{}")
            if not isinstance(cmd, dict):
                raise ValueError("the command must be a JSON object")
            result = await self.on_cmd(cmd)
            payload = result if isinstance(result, dict) else {"ok": True}
            await self._finish(writer, 200, "application/json", json.dumps(payload, ensure_ascii=False).encode())
        except Exception as e:  # noqa: BLE001
            await self._finish(writer, 400, "application/json", json.dumps({"error": str(e)}).encode())

    async def _stream(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
        for ev in self._history[-80:]:
            writer.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
        await writer.drain()
        self._clients.add(writer)
        try:
            while True:
                await asyncio.sleep(15)
                writer.write(b": ping\n\n"); await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._clients.discard(writer); writer.close()

    @staticmethod
    async def _finish(writer: asyncio.StreamWriter, status: int, ctype: str, body: bytes) -> None:
        writer.write(f"HTTP/1.1 {status} OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        finally:
            writer.close()
