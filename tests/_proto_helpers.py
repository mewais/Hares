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
from mcp.types import ElicitResult, ElicitRequestParams


async def _accept_elicitation_callback(ctx, params: ElicitRequestParams) -> ElicitResult:
    """Elicitation callback that unconditionally accepts every elicitation.

    Used in tests that need to simulate a user who approves a prompt.
    The MCP SDK invokes this callback on the client side whenever the
    server sends an elicitation/create request.
    """
    from mcp.types import ElicitResult
    return ElicitResult(action="accept", content={})


async def _decline_elicitation_callback(ctx, params: ElicitRequestParams) -> ElicitResult:
    """Elicitation callback that unconditionally declines every elicitation.

    Used in tests that explicitly simulate a user who rejects a prompt
    (as opposed to a client that has no elicitation support at all).
    """
    from mcp.types import ElicitResult
    return ElicitResult(action="decline")


async def _cancel_elicitation_callback(ctx, params: ElicitRequestParams) -> ElicitResult:
    """Elicitation callback that unconditionally cancels (dismissed
    without an explicit choice) every elicitation."""
    from mcp.types import ElicitResult
    return ElicitResult(action="cancel")


def _grant_elicitation_callback(grant: str):
    """Build an elicitation callback that accepts with a specific
    ``{"grant": ...}`` form-field value — simulates a well-behaved MCP
    client that honors ``requestedSchema`` and a human who picked
    ``grant`` in the request_path_access dialog (one of "once",
    "session", "deny")."""
    async def _callback(ctx, params: ElicitRequestParams) -> ElicitResult:
        from mcp.types import ElicitResult
        return ElicitResult(action="accept", content={"grant": grant})
    return _callback


@asynccontextmanager
async def hares_session(
    *,
    enable: str = "shell",
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
    read_only: bool = False,
    state_file: Optional[Path] = None,
    extra_env: Optional[dict] = None,
    elicitation_callback=None,
) -> AsyncIterator[ClientSession]:
    """Spawn ``hares-mcp`` with the given flags and yield a connected
    ClientSession after ``initialize()`` completes.

    Notes:
      - ``HARES_SANDBOX_DISABLED=1`` is exported by default so tests
        don't depend on bwrap being installed in the sandbox they run in.
      - ``ceiling`` is required for any --enable mode (per CLI), so
        callers MUST pass it explicitly.
      - Pass ``elicitation_callback=_accept_elicitation_callback`` (or
        ``_decline_elicitation_callback``) to simulate user interaction
        with server-initiated elicitation requests.  Without a callback,
        the default MCP client rejects elicitation with an error — which
        is the correct "no elicitation support" fail-closed behaviour.
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
        async with ClientSession(
            read, write,
            elicitation_callback=elicitation_callback,
        ) as session:
            await session.initialize()
            yield session


def parse_text_result(result) -> dict:
    """Tools return a list of TextContent. Each handler in this codebase
    JSON-encodes its return value into the first text item — this
    helper undoes that wrapping so tests can assert structurally."""
    assert result.content, f"empty content: {result}"
    first = result.content[0]
    return json.loads(first.text)
