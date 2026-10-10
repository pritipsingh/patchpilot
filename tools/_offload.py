"""
Off-loop tool execution
=======================

Agno calls a sync tool directly on the event loop, even inside an async run. Every
tool in this service does blocking work -- a git clone, `pnpm install`, a test
suite, a GitHub API call -- so for the whole of that call the server answers
nothing. Slack gives an endpoint three seconds before it retries, and a button
click that lands during a 40-second install simply never gets its 200.

`offloaded` builds an async twin of a sync tool that runs the body in a worker
thread. It keeps the original signature, annotations and docstring, so Agno
generates the same schema for the twin and still recognises `run_context` as a
framework-injected parameter. Register the twin with the toolkit's `async_tools`
under the same name, and Agno picks it in async runs.

`RunContext` is imported here on purpose: the annotations are strings, and Agno
resolves them against the wrapper's module globals.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any

from agno.run import RunContext  # noqa: F401  (resolves the twin's annotations)


def offloaded(method: Callable[..., Any]) -> Callable[..., Any]:
    """An async twin of `method` that runs it in a worker thread."""

    @functools.wraps(method)
    async def twin(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)

    return twin


def async_twins(owner: Any, names: list[str]) -> list[tuple[Callable[..., Any], str]]:
    """`async_tools` entries for every named sync tool on `owner`."""
    return [(offloaded(getattr(owner, name)), name) for name in names]
