"""The api stops promptly with a page's live stream still open: the stream is
closed for it, and its own clean-up then runs."""

import asyncio
import os
import signal
import socket
import sys
import time

from httpx import AsyncClient, TransportError

from opennotebook.shutdown import CONNECTIONS_SECONDS, DRAIN_SECONDS


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_the_api_stops_with_a_live_stream_open(client: AsyncClient) -> None:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    port = _free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from opennotebook.cli import main; main()",
        "serve",
        "--port",
        str(port),
        env=os.environ.copy(),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as web:
            for _ in range(100):
                try:
                    if (await web.get("/api/health")).status_code == 200:
                        break
                except TransportError:
                    pass
                await asyncio.sleep(0.2)
        # A page's stream, held open the way a browser holds it: it never
        # hangs up by itself.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"GET /api/collections/{cid}/events HTTP/1.1\r\nHost: studio\r\n"
            "Accept: text/event-stream\r\n\r\n".encode()
        )
        await writer.drain()
        assert (await reader.readline()).startswith(b"HTTP/1.1 200")
        await reader.readuntil(b"event: collection")
        proc.send_signal(signal.SIGTERM)
        started = time.monotonic()
        await asyncio.wait_for(proc.wait(), timeout=CONNECTIONS_SECONDS + DRAIN_SECONDS)
        assert time.monotonic() - started < CONNECTIONS_SECONDS + 5
        # Uvicorn raises the signal again once it has stopped cleanly.
        assert proc.returncode in (0, -signal.SIGTERM)
        writer.close()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
