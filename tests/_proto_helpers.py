"""Shared helpers for MCP-protocol-level (proto) tests.

These tests spawn ``hares-mcp`` as a subprocess and exchange real
JSON-RPC over stdio via the official ``mcp`` client. We use the
client to avoid hand-rolling Content-Length framing and to mirror
how any MCP host (Claude Code, IDE plugins, framework orchestrators)
talks to the server.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@asynccontextmanager
async def hares_session(
    *,
    enable: str = "shell",
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
    read_only: bool = False,
    state_file: Optional[Path] = None,
    extra_env: Optional[dict] = None,
) -> AsyncIterator[ClientSession]:
    """Spawn ``hares-mcp`` with the given flags and yield a connected
    ClientSession after ``initialize()`` completes.

    Notes:
      - ``HARES_SANDBOX_DISABLED=1`` is exported by default so tests
        don't depend on bwrap being installed in the sandbox they run in.
      - ``ceiling`` is required for any --enable mode (per CLI), so
        callers MUST pass it explicitly.
    """
    args: list[str] = [f"--enable={enable}"]
    if scope_id:
        args.extend(["--scope-id", scope_id])
    if ceiling is not None:
        args.extend(["--ceiling", str(ceiling)])
    if read_only:
        args.append("--read-only")
    if state_file is not None:
        args.extend(["--state-file", str(state_file)])

    env = os.environ.copy()
    env.setdefault("HARES_SANDBOX_DISABLED", "1")
    if extra_env:
        env.update(extra_env)

    params = StdioServerParameters(command="hares-mcp", args=args, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def parse_text_result(result) -> dict:
    """Tools return a list of TextContent. Each handler in this codebase
    JSON-encodes its return value into the first text item — this
    helper undoes that wrapping so tests can assert structurally."""
    assert result.content, f"empty content: {result}"
    first = result.content[0]
    return json.loads(first.text)
