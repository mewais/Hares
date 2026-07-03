"""Hares shell MCP server — exposes ``execute_command``.

Thin wrapper around :class:`hares.runner.Runner` that exposes a
single tool — ``execute_command`` — over the MCP stdio transport.

Per-instance configuration (scope-id prefix, optional active-scope
narrowing for bwrap mounts, read-only mode) is supplied by the CLI
entry point (:mod:`hares.cli`); :func:`serve` is the dispatch target.

For 0.1-compat bare invocation (``hares-mcp`` with no flags), all
optional parameters default to None / False and the registered tool
name is the unprefixed ``execute_command``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..audit import Auditor, audited, load_auditor
from ..config import load_config
from ..coordination import CrossProcessCoordinator, install_atexit_cleanup
from ..exec_tools import (
    SHELL_ELICIT_DECLINE_TEMPLATE,
    build_exec_tool_handlers,
    make_roots_refiner,
    shell_exec_tool_descriptors,
)
from ..grant_tools import (
    build_request_path_access_handlers,
    request_path_access_tool_descriptor,
)
from ..grants import GrantStore
from ..path_safety import resolve_deny_lists
from ..policy import PolicyEngine
from ..runner import Runner

logger = logging.getLogger(__name__)


def _build_server(
    runner: Runner,
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
    read_only: bool = False,
    state_file: Optional[Path] = None,
    auditor: Optional["Auditor"] = None,
    use_roots: bool = False,
    policy: Optional[PolicyEngine] = None,
    mem_limit_mb: int = 7168,
    mem_limit_max_mb: int = 0,
    grant_store: Optional[GrantStore] = None,
) -> Server:
    """Build the MCP Server with ``execute_command`` plus the shared
    restrict tools wired to the supplied runner.

    Args:
      runner: The subprocess executor.
      scope_id: Optional tool-name prefix (per-instance disambiguation).
      ceiling: Optional outer bound for active scope (only meaningful
        for runtime-narrowing tool calls).
      read_only: When True, bwrap mounts the active scope as RO so
        subprocess writes are kernel-rejected.
      state_file: Optional path where the active scope is persisted.

    The restrict tools (``restrict_paths``, ``get_active_paths``)
    are registered when ``ceiling`` is
    set — they let an architect-class caller narrow the active scope
    at runtime within the ceiling. When ``ceiling`` is None (bare
    0.1-compat invocation), restrict tools are not exposed.
    """
    server: Server = Server("hares-shell")

    # In-memory store for runtime request_path_access grants. Always
    # constructed (even in bare 0.1-compat mode with no ceiling) —
    # the tool itself works without a ceiling, falling back to the
    # server's cwd as the base for relative path args.
    if grant_store is None:
        grant_store = GrantStore()

    # Resolve the in-ceiling blacklist (HARES_SANDBOX_EXCLUDE /
    # HARES_SANDBOX_PROTECT) once, mirroring hares.fs.server — used
    # only by request_path_access's grant-approval-time deny check
    # (validate_grant_target). None when there's no ceiling to
    # resolve against (bare shell mode); the handler treats that as
    # "no in-ceiling blacklist applies."
    deny = resolve_deny_lists(ceiling) if ceiling is not None else None

    # Lazy import to keep the shared-tools module a soft dep on the
    # shell-only path (avoids circular shape during refactor). The
    # restrict tools live under hares.fs.tools because they're shared
    # between fs and shell, and conceptually narrow filesystem access.
    if ceiling is not None:
        from ..fs.tools import (
            build_restrict_tool_handlers,
            restrict_tool_descriptors,
        )
        scope_state, restrict_handlers = build_restrict_tool_handlers(
            scope_id=scope_id,
            ceiling=ceiling,
            state_file=state_file,
            on_change=lambda: runner.set_active_scope(
                scope_state.current().paths,
                read_only=read_only,
            ),
            grant_store=grant_store,
        )
        # Apply the loaded scope to the runner immediately (state file
        # may have been populated by a prior process).
        runner.set_active_scope(
            scope_state.current().paths,
            read_only=read_only,
        )
        restrict_descriptors = restrict_tool_descriptors(scope_id)
    else:
        restrict_handlers = {}
        restrict_descriptors = []
        scope_state = None

    # Exec tools — descriptors + handlers shared with the combined
    # server via hares.exec_tools (policy gating, memory elicitation,
    # OOM-hint appending all live there).
    exec_descriptors = shell_exec_tool_descriptors(
        scope_id,
        mem_limit_mb=mem_limit_mb,
        mem_limit_max_mb=mem_limit_max_mb,
    )
    handlers = build_exec_tool_handlers(
        server=server,
        runner=runner,
        scope_id=scope_id,
        policy=policy,
        mem_limit_mb=mem_limit_mb,
        mem_limit_max_mb=mem_limit_max_mb,
        elicit_decline_template=SHELL_ELICIT_DECLINE_TEMPLATE,
    )
    handlers.update(restrict_handlers)

    # request_path_access — the runtime, human-in-the-loop-gated
    # escape hatch for widening bwrap mount access OUTSIDE the
    # ceiling. Always registered, like the restrict tools (no CLI
    # flag gate): it fails closed on its own for any non-interactive
    # client. --read-only is enforced INSIDE the handler (only
    # mode="ro" requests can be granted).
    grant_descriptor = request_path_access_tool_descriptor(scope_id)
    handlers.update(build_request_path_access_handlers(
        server=server,
        scope_id=scope_id,
        ceiling=ceiling,
        deny=deny,
        grant_store=grant_store,
        read_only=read_only,
    ))

    # One-shot: fetch roots from the MCP client on the first
    # list_tools() call (always fired before any tool call) and use
    # them to refine the ceiling when no explicit ceiling was configured.
    _maybe_refine_from_roots = make_roots_refiner(runner, use_roots)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        await _maybe_refine_from_roots()
        tools: list[Tool] = list(exec_descriptors)
        tools.extend(restrict_descriptors)
        tools.append(grant_descriptor)
        return tools

    @server.call_tool()
    @audited(auditor, scope_id=scope_id)
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")
        result = await handler(arguments)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


async def _serve_async(
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
    read_only: bool = False,
    state_file: Optional[Path] = None,
    use_roots: bool = False,
    policy: Optional[PolicyEngine] = None,
) -> None:
    """Async entry point — sets up the runner, builds the server, and
    drives the stdio transport until shutdown."""
    cfg = load_config(default_cwd=os.getcwd())
    logger.info(
        "Hares shell starting: scope_id=%r ceiling=%r read_only=%s "
        "max_concurrent=%d mem=%dMB cpu=%ds sandbox=%s",
        scope_id, str(ceiling) if ceiling else None, read_only,
        cfg.max_concurrent, cfg.mem_limit_mb, cfg.cpu_limit_sec,
        "bwrap" if cfg.sandbox.enabled else "off",
    )
    if use_roots:
        logger.info(
            "Ceiling will be refined from MCP roots at session init "
            "(current guess: %s)",
            ceiling,
        )
    if cfg.sandbox.enabled:
        logger.info(
            "Sandbox: rw_binds=%s ro_binds=%s network=%s",
            list(cfg.sandbox.rw_binds), list(cfg.sandbox.ro_binds),
            "on" if cfg.sandbox.allow_network else "off",
        )
    coord = CrossProcessCoordinator(
        coord_dir=cfg.coordination_dir,
        max_concurrent=cfg.max_concurrent,
    )
    install_atexit_cleanup(coord)
    # Shared with _build_server's request_path_access registration so
    # grants recorded via the tool are immediately visible to the
    # Runner's bwrap mount composition (same object, not a copy).
    grant_store = GrantStore()
    runner = Runner(
        max_concurrent=cfg.max_concurrent,
        mem_limit_mb=cfg.mem_limit_mb,
        cpu_limit_sec=cfg.cpu_limit_sec,
        rss_poll_interval=cfg.rss_poll_interval,
        rss_overshoot_ratio=cfg.rss_overshoot_ratio,
        sandbox=cfg.sandbox,
        coordinator=coord,
        ceiling=ceiling,
        network_policy=cfg.network_policy,
        mem_limit_max_mb=cfg.mem_limit_max_mb,
        grant_store=grant_store,
    )
    auditor = load_auditor()
    if auditor is not None:
        logger.info("Audit log enabled: dest=%r", auditor.dest)
    server = _build_server(
        runner,
        scope_id=scope_id,
        ceiling=ceiling,
        read_only=read_only,
        state_file=state_file,
        auditor=auditor,
        use_roots=use_roots,
        policy=policy,
        mem_limit_mb=cfg.mem_limit_mb,
        mem_limit_max_mb=cfg.mem_limit_max_mb,
        grant_store=grant_store,
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def serve(
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
    read_only: bool = False,
    state_file: Optional[Path] = None,
    use_roots: bool = False,
    policy: Optional[PolicyEngine] = None,
) -> None:
    """Synchronous entry — wraps :func:`_serve_async` in ``asyncio.run``.

    Called by the CLI dispatcher (:func:`hares.cli.main`) after argument
    parsing. Tests can call this directly with explicit kwargs.
    """
    try:
        asyncio.run(_serve_async(
            scope_id=scope_id,
            ceiling=ceiling,
            read_only=read_only,
            state_file=state_file,
            use_roots=use_roots,
            policy=policy,
        ))
    except KeyboardInterrupt:
        logger.info("Hares shutting down (KeyboardInterrupt)")


def main() -> None:
    """0.1-compat entry: ``hares-mcp`` with no flags. Preserved as a
    courtesy for callers that haven't migrated to the new CLI surface;
    new code should go through :mod:`hares.cli`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s hares: %(message)s",
        datefmt="%H:%M:%S",
    )
    serve()


if __name__ == "__main__":
    main()
