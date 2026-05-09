"""Hares filesystem MCP server.

Registers fs tools (reads always; writes suppressed when
``read_only=True``) plus the shared restrict tools (always
registered — gating is via the agent-side tools allowlist, not
deploy-time configuration).

Tool names are optionally prefixed with ``<scope_id>_`` per the
unified naming convention. The server handles dispatch by stripping
the prefix on incoming tool names and looking up the base name in
the dispatch tables from :mod:`hares.fs.operations`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..audit import Auditor, audited, load_auditor
from .operations import READ_OPS, WRITE_OPS
from .state import ScopeStateStore

logger = logging.getLogger(__name__)


def _prefixed(name: str, scope_id: Optional[str]) -> str:
    return f"{scope_id}_{name}" if scope_id else name


def _build_server(
    *,
    scope_id: Optional[str],
    ceiling: Path,
    read_only: bool,
    state_file: Optional[Path],
    auditor: Optional[Auditor] = None,
    use_roots: bool = False,  # reserved; see note in _serve_async
) -> Server:
    """Build the MCP server. The ``ScopeStateStore`` instance owns the
    active scope; restrict-tool calls mutate it; fs-op handlers read
    from it."""
    server: Server = Server("hares-fs")
    scope_state = ScopeStateStore(
        scope_id=scope_id, ceiling=ceiling, state_file=state_file,
    )

    # Build the per-instance tool descriptor list.
    tool_descriptors: list[Tool] = []
    handlers: dict[str, callable] = {}

    for base_name, spec in READ_OPS.items():
        full_name = _prefixed(base_name, scope_id)
        tool_descriptors.append(Tool(
            name=full_name,
            description=spec["description"],
            inputSchema=spec["inputSchema"],
        ))
        handlers[full_name] = spec["handler"]

    if not read_only:
        for base_name, spec in WRITE_OPS.items():
            full_name = _prefixed(base_name, scope_id)
            tool_descriptors.append(Tool(
                name=full_name,
                description=spec["description"],
                inputSchema=spec["inputSchema"],
            ))
            handlers[full_name] = spec["handler"]

    # Restrict tools — always registered (no flag gate). The agent-side
    # tools allowlist controls who calls them.
    from .tools import restrict_tool_descriptors, build_restrict_tool_handlers
    restrict_descriptors = restrict_tool_descriptors(scope_id)
    _, restrict_handlers = build_restrict_tool_handlers(
        scope_id=scope_id,
        ceiling=ceiling,
        state_file=state_file,
        scope_state=scope_state,
        on_change=None,  # fs ops read scope_state directly each call; no notification needed
    )
    tool_descriptors.extend(restrict_descriptors)
    handlers.update(restrict_handlers)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return tool_descriptors

    @server.call_tool()
    @audited(auditor, scope_id=scope_id)
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")
        # Detect whether this is a restrict-tool (1-arg) vs fs-op (3-arg).
        # Restrict tools take just args; fs ops take args + ceiling + scope.
        # Check by calling convention via inspection (cheap):
        result = await _dispatch(handler, arguments, scope_state, ceiling)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


async def _dispatch(handler, arguments: dict, scope_state: ScopeStateStore,
                    ceiling: Path):
    """Call handler with the right kwargs based on its name.

    Restrict handlers (restrict_paths, get_active_paths)
    are bound closures that already capture scope_state + ceiling, so
    they take only ``args``. FS operations need ``ceiling`` and
    ``scope`` injected as kwargs.
    """
    # Heuristic: bound closures from build_restrict_tool_handlers take
    # exactly one positional arg (args dict). FS op functions take
    # (args, *, ceiling, scope). We try the fs-op signature first;
    # restrict closures will TypeError on unexpected kwargs and we
    # fall back.
    try:
        return await handler(arguments, ceiling=ceiling, scope=scope_state.current())
    except TypeError as exc:
        if "unexpected keyword argument" in str(exc) or "ceiling" in str(exc):
            return await handler(arguments)
        raise


async def _serve_async(
    *,
    scope_id: Optional[str],
    ceiling: Path,
    read_only: bool,
    state_file: Optional[Path],
    use_roots: bool = False,
) -> None:
    logger.info(
        "Hares fs starting: scope_id=%r ceiling=%s read_only=%s state_file=%s",
        scope_id, ceiling, read_only, state_file,
    )
    auditor = load_auditor()
    if auditor is not None:
        logger.info("Audit log enabled: dest=%r", auditor.dest)
    # Note: use_roots is passed through but not yet active for --enable=fs
    # standalone. Updating the ceiling at runtime requires threading a
    # mutable ceiling ref into ScopeStateStore and all fs-op dispatch
    # closures. For now, roots-based ceiling refinement is implemented
    # only in shell and combined modes. Use --enable=fs+shell (combined)
    # which fully supports roots, or set HARES_FS_CEILING explicitly.
    server = _build_server(
        scope_id=scope_id,
        ceiling=ceiling,
        read_only=read_only,
        state_file=state_file,
        auditor=auditor,
        use_roots=use_roots,
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def serve(
    *,
    scope_id: Optional[str] = None,
    ceiling: Path,
    read_only: bool = False,
    state_file: Optional[Path] = None,
    use_roots: bool = False,
) -> None:
    """Synchronous entry — wraps :func:`_serve_async` in ``asyncio.run``.
    Called by the CLI dispatcher."""
    try:
        asyncio.run(_serve_async(
            scope_id=scope_id,
            ceiling=ceiling,
            read_only=read_only,
            state_file=state_file,
            use_roots=use_roots,
        ))
    except KeyboardInterrupt:
        logger.info("Hares fs shutting down (KeyboardInterrupt)")
