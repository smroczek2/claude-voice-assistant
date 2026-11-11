import asyncio
import io
import json

import pytest

from voz.daemon import read_token
from voz.ui import UI, host_is_local

TOKEN = "t" * 40


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:8766", True),
    ("localhost:8766", True),
    ("[::1]:8766", True),
    ("LOCALHOST:8766", True),
    ("127.0.0.1:9999", False),
    ("127.0.0.1", False),
    ("evil.example:8766", False),
    ("127.0.0.1.evil.example:8766", False),
    ("", False),
])
def test_host_is_local(host, ok):
    assert host_is_local(host, 8766) is ok


def test_ui_needs_a_token():
    with pytest.raises(ValueError):
        UI(8766, lambda cmd: None, "")


def test_read_token_rejects_short_or_missing():
    with pytest.raises(SystemExit):
        read_token(io.StringIO(""))
    with pytest.raises(SystemExit):
        read_token(io.StringIO("short\n"))
    assert read_token(io.StringIO(TOKEN + "\n")) == TOKEN


async def _request(port, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    return data


def _post(port, headers: dict, body: bytes = b'{"type":"stop"}') -> bytes:
    lines = ["POST /cmd HTTP/1.1", f"Content-Length: {len(body)}"] + [f"{k}: {v}" for k, v in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def test_channel_rejects_missing_or_wrong_secret_and_bad_host(unused_port):
    got = []

    async def on_cmd(cmd):
        got.append(cmd)
        return {"ok": True}

    async def run():
        ui = UI(unused_port, on_cmd, TOKEN)
        await ui.start()
        try:
            host = f"127.0.0.1:{unused_port}"
            no_token = await _request(unused_port, _post(unused_port, {"Host": host}))
            wrong = await _request(unused_port, _post(unused_port, {"Host": host, "X-Voz-Token": "x" * 40}))
            rebind = await _request(unused_port, _post(unused_port, {"Host": f"evil.example:{unused_port}", "X-Voz-Token": TOKEN}))
            page = await _request(unused_port, f"GET / HTTP/1.1\r\nHost: {host}\r\nX-Voz-Token: {TOKEN}\r\n\r\n".encode())
            ok = await _request(unused_port, _post(unused_port, {"Host": host, "X-Voz-Token": TOKEN}))
            return no_token, wrong, rebind, page, ok
        finally:
            await ui.close()

    no_token, wrong, rebind, page, ok = asyncio.run(run())
    for refused in (no_token, wrong, rebind):
        assert refused.startswith(b"HTTP/1.1 403")
    assert page.startswith(b"HTTP/1.1 404")
    assert ok.startswith(b"HTTP/1.1 200")
    assert b"Access-Control-Allow-Origin" not in ok
    assert got == [{"type": "stop"}]
    assert json.loads(ok.split(b"\r\n\r\n", 1)[1]) == {"ok": True}


@pytest.fixture
def unused_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
