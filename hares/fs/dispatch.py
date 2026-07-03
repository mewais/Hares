"""Shared tagged-dispatch helper for MCP ``call_tool`` handlers.

Both :mod:`hares.fs.server` and :mod:`hares.combined.server` register
tools into a ``name -> (kind, handler)`` map and, on every
``call_tool``, look the name up, dispatch by ``kind`` (fs handlers
need the per-call :class:`~hares.fs.context.OpContext`; every other
registered kind is a bound closure that takes only the arguments
dict), and wrap the JSON-able result dict in the MCP
``TextContent`` envelope. This module factors that lookup-dispatch-
wrap sequence out so both servers share one implementation; the two
servers differ only in which non-"fs" kinds they register (e.g.
combined also has "exec"), so the allowed set is passed in.
"""

from __future__ import annotations

import json
from typing import Container

from mcp.types import TextContent

from .context import OpContext


async def dispatch_tool_call(
    handlers: dict,
    name: str,
    arguments: dict,
    ctx: OpContext,
    *,
    non_fs_kinds: Container[str],
) -> list[TextContent]:
    """Look up ``name`` in ``handlers``, dispatch by kind, and wrap the
    result. ``handlers`` maps tool name to ``(kind, handler)``: "fs"
    handlers are called as ``handler(arguments, ctx=ctx)``; any kind
    in ``non_fs_kinds`` is called as ``handler(arguments)``.

    Raises:
      ValueError: ``name`` is not registered, or its kind is neither
        "fs" nor in ``non_fs_kinds``.
    """
    entry = handlers.get(name)
    if entry is None:
        raise ValueError(f"Unknown tool: {name}")
    kind, handler = entry
    if kind == "fs":
        result = await handler(arguments, ctx=ctx)
    elif kind in non_fs_kinds:
        result = await handler(arguments)
    else:
        raise ValueError(f"Unknown handler kind: {kind!r}")
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
