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
import logging
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..audit import Auditor, audited, load_auditor
from ..grant_tools import (
    build_request_path_access_handlers,
    request_path_access_tool_descriptor,
)
from ..grants import GrantStore
from ..path_safety import resolve_deny_lists
from .context import OpContext
from .dispatch import dispatch_tool_call
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

    # Resolve the in-ceiling blacklist (HARES_SANDBOX_EXCLUDE /
    # HARES_SANDBOX_PROTECT) ONCE, now that the ceiling is final — fs
    # handlers receive it explicitly instead of re-reading the env per
    # call. (Standalone fs mode never refines the ceiling at runtime;
    # see the use_roots note in _serve_async.)
    deny = resolve_deny_lists(ceiling)

    # In-memory store for runtime request_path_access grants. Per-
    # process, never persisted — see hares.grants.
    grant_store = GrantStore()

    # Build the per-instance tool descriptor list plus a tagged handler
    # registry: name → (kind, handler). "fs" handlers take
    # (args, ctx=OpContext(...)); "restrict" handlers are bound
    # closures that take only the args dict. Same convention as the
    # combined server.
    tool_descriptors: list[Tool] = []
    handlers: dict[str, tuple] = {}

    for base_name, spec in READ_OPS.items():
        full_name = _prefixed(base_name, scope_id)
        tool_descriptors.append(Tool(
            name=full_name,
            description=spec["description"],
            inputSchema=spec["inputSchema"],
        ))
        handlers[full_name] = ("fs", spec["handler"])

    if not read_only:
        for base_name, spec in WRITE_OPS.items():
            full_name = _prefixed(base_name, scope_id)
            tool_descriptors.append(Tool(
                name=full_name,
                description=spec["description"],
                inputSchema=spec["inputSchema"],
            ))
            handlers[full_name] = ("fs", spec["handler"])

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
        grant_store=grant_store,
    )
    tool_descriptors.extend(restrict_descriptors)
    for k, v in restrict_handlers.items():
        handlers[k] = ("restrict", v)

    # request_path_access — the runtime, human-in-the-loop-gated
    # escape hatch for widening access OUTSIDE the ceiling. Always
    # registered (like restrict tools) — it fails closed on its own
    # for any non-interactive client, so no operator off-switch is
    # needed. --read-only is enforced INSIDE the handler (only
    # mode="ro" requests can be granted).
    tool_descriptors.append(request_path_access_tool_descriptor(scope_id))
    grant_handlers = build_request_path_access_handlers(
        server=server,
        scope_id=scope_id,
        ceiling=ceiling,
        deny=deny,
        grant_store=grant_store,
        read_only=read_only,
    )
    for k, v in grant_handlers.items():
        handlers[k] = ("grant", v)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return tool_descriptors

    @server.call_tool()
    @audited(auditor, scope_id=scope_id)
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        ctx = OpContext(
            ceiling=ceiling, scope=scope_state.current(),
            deny=deny, grants=grant_store,
        )
        return await dispatch_tool_call(
            handlers, name, arguments, ctx,
            non_fs_kinds=("restrict", "grant"),
        )

    return server


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
