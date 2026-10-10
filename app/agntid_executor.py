"""Run the callback MCP endpoint in the AgentOS process, on a separate listener."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import uvicorn

from app.agntid_bridge import AgntidBridge


class CallbackServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The main AgentOS server owns process signals.
        yield


@asynccontextmanager
async def serve_executor(bridge: AgntidBridge) -> AsyncIterator[None]:
    settings = bridge.settings
    mcp = bridge.executor()
    config = uvicorn.Config(
        mcp.streamable_http_app(),
        host=settings.executor_host,
        port=settings.executor_port,
        log_level="warning",
        access_log=False,
    )
    server = CallbackServer(config)
    # Bind synchronously so an occupied port fails startup instead of silently
    # leaving the helper connected to a different executor.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    task = None
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((settings.executor_host, settings.executor_port))
        sock.listen(128)
        sock.setblocking(False)
        task = asyncio.create_task(server.serve(sockets=[sock]))
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("AgntID executor stopped before startup")
                await asyncio.sleep(0.01)
        yield
    finally:
        server.should_exit = True
        if task is not None:
            try:
                async with asyncio.timeout(10):
                    await asyncio.shield(task)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        sock.close()
