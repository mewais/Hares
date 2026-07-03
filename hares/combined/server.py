"""Combined fs+shell MCP server — one process, both tool families,
one shared scope.

Builds a single MCP server that registers:

* All fs read tools (under ``--ceiling``, plus active-scope narrowing for writes)
* All fs write tools (suppressed by ``--read-only``)
* The shell ``execute_command`` (consuming the same active scope for bwrap mounts)
* The shared ``restrict_paths`` and ``get_active_paths`` tools

ONE ``ScopeStateStore`` is constructed and shared with both the fs
operations (which read it for write-target validation) AND the
``Runner.set_active_scope()`` callback (which the shell side uses to
re-spawn bwrap with new mounts).

A caller invokes ``<scope>_restrict_paths(['lib/foo'])`` once and
BOTH layers narrow together — that's the value proposition of the
combined mode.
"""

from __future__ import annotations

import asyncio
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
    COMBINED_ELICIT_DECLINE_TEMPLATE,
    build_exec_tool_handlers,
    combined_exec_tool_descriptors,
    make_roots_refiner,
    prefixed as _prefixed,
)
from ..fs.context import OpContext
from ..fs.dispatch import dispatch_tool_call
from ..fs.operations import READ_OPS, WRITE_OPS
from ..grant_tools import (
    build_request_path_access_handlers,
    request_path_access_tool_descriptor,
)
from ..grants import GrantStore
from ..path_safety import resolve_deny_lists
from ..policy import PolicyEngine
from ..fs.state import ScopeStateStore
from ..fs.tools import (
    build_restrict_tool_handlers,
    restrict_tool_descriptors,
)
from ..runner import Runner

logger = logging.getLogger(__name__)


def _build_server(
    *,
    scope_id: Optional[str],
    ceiling: Path,
    read_only: bool,
    state_file: Optional[Path],
    runner: Runner,
    auditor: Optional[Auditor] = None,
    use_roots: bool = False,
    policy: Optional[PolicyEngine] = None,
    mem_limit_mb: int = 7168,
    mem_limit_max_mb: int = 0,
    grant_store: Optional[GrantStore] = None,
) -> Server:
    server: Server = Server("hares-combined")

    # ONE GrantStore, shared between the fs operation handlers and the
    # shell Runner — exactly like scope_state below. Caller (serve())
    # normally constructs this ahead of time so it can also hand it to
    # the Runner's constructor; build a fresh one only for callers
    # (tests) that don't pass one.
    if grant_store is None:
        grant_store = GrantStore()

    # ONE scope state, shared between fs handlers and shell runner.
    scope_state = ScopeStateStore(
        scope_id=scope_id, ceiling=ceiling, state_file=state_file,
    )

    # On every restrict call, push the new scope into the runner so
    # the next execute_command rebuilds bwrap with the new mounts.
    def _on_change() -> None:
        runner.set_active_scope(
            scope_state.current().paths,
            read_only=read_only,
        )

    # Apply the loaded scope (state file may be pre-populated) immediately.
    runner.set_active_scope(scope_state.current().paths, read_only=read_only)

    # Resolve the in-ceiling blacklist (HARES_SANDBOX_EXCLUDE /
    # HARES_SANDBOX_PROTECT) ONCE, now that the fs ceiling is final —
    # fs handlers receive it explicitly instead of re-reading the env
    # per call. (Roots refinement below only updates the RUNNER's
    # ceiling for bwrap mounts; the fs-op ceiling is fixed at build
    # time, and the Runner resolves its own blacklist per call in
    # ``Runner._resolve_blacklist`` precisely because its ceiling can
    # change.)
    deny = resolve_deny_lists(ceiling)

    # Build descriptors + handlers for both tool families plus restrict.
    tool_descriptors: list[Tool] = []
    handlers: dict = {}

    # FS reads.
    for base_name, spec in READ_OPS.items():
        full_name = _prefixed(base_name, scope_id)
        tool_descriptors.append(Tool(
            name=full_name,
            description=spec["description"],
            inputSchema=spec["inputSchema"],
        ))
        handlers[full_name] = ("fs", spec["handler"])

    # FS writes (suppressed when read_only).
    if not read_only:
        for base_name, spec in WRITE_OPS.items():
            full_name = _prefixed(base_name, scope_id)
            tool_descriptors.append(Tool(
                name=full_name,
                description=spec["description"],
                inputSchema=spec["inputSchema"],
            ))
            handlers[full_name] = ("fs", spec["handler"])

    # Exec tools — descriptors + handlers shared with the shell server
    # via hares.exec_tools (policy gating, memory elicitation, OOM-hint
    # appending all live there).
    tool_descriptors.extend(combined_exec_tool_descriptors(
        scope_id,
        mem_limit_mb=mem_limit_mb,
        mem_limit_max_mb=mem_limit_max_mb,
    ))
    exec_handlers = build_exec_tool_handlers(
        server=server,
        runner=runner,
        scope_id=scope_id,
        policy=policy,
        mem_limit_mb=mem_limit_mb,
        mem_limit_max_mb=mem_limit_max_mb,
        elicit_decline_template=COMBINED_ELICIT_DECLINE_TEMPLATE,
    )
    for k, v in exec_handlers.items():
        handlers[k] = ("exec", v)

    # Restrict tools — one shared instance for both fs and shell.
    restrict_descriptors = restrict_tool_descriptors(scope_id)
    _, restrict_handlers = build_restrict_tool_handlers(
        scope_id=scope_id,
        ceiling=ceiling,
        state_file=state_file,
        scope_state=scope_state,
        on_change=_on_change,
        grant_store=grant_store,
    )
    tool_descriptors.extend(restrict_descriptors)
    for k, v in restrict_handlers.items():
        handlers[k] = ("restrict", v)

    # request_path_access — the runtime, human-in-the-loop-gated
    # escape hatch, sharing the SAME grant_store as the fs dispatch
    # below and the Runner's bwrap mount composition — a grant
    # recorded here is immediately visible to both. Always
    # registered (no CLI flag gate): it fails closed on its own for
    # any non-interactive client. --read-only is enforced INSIDE the
    # handler (only mode="ro" requests can be granted).
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

    _maybe_refine_from_roots = make_roots_refiner(runner, use_roots)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        await _maybe_refine_from_roots()
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
            non_fs_kinds=("exec", "restrict", "grant"),
        )

    return server


async def _serve_async(
    *,
    scope_id: Optional[str],
    ceiling: Path,
    read_only: bool,
    state_file: Optional[Path],
    use_roots: bool = False,
    policy: Optional[PolicyEngine] = None,
) -> None:
    cfg = load_config(default_cwd=os.getcwd())
    coord = CrossProcessCoordinator(
        coord_dir=cfg.coordination_dir,
        max_concurrent=cfg.max_concurrent,
    )
    install_atexit_cleanup(coord)
    # ONE GrantStore for the whole process — shared between the fs
    # operation handlers and the shell Runner's bwrap mount
    # composition, exactly like the ScopeStateStore built inside
    # _build_server.
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
    logger.info(
        "Hares combined starting: scope_id=%r ceiling=%s read_only=%s "
        "state_file=%s coord_dir=%s sandbox=%s",
        scope_id, ceiling, read_only, state_file,
        cfg.coordination_dir,
        "bwrap" if cfg.sandbox.enabled else "off",
    )
    auditor = load_auditor()
    if auditor is not None:
        logger.info("Audit log enabled: dest=%r", auditor.dest)
    server = _build_server(
        scope_id=scope_id,
        ceiling=ceiling,
        read_only=read_only,
        state_file=state_file,
        runner=runner,
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
    ceiling: Path,
    read_only: bool = False,
    state_file: Optional[Path] = None,
    use_roots: bool = False,
    policy: Optional[PolicyEngine] = None,
) -> None:
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
        logger.info("Hares combined shutting down (KeyboardInterrupt)")
