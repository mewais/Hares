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
from ..fs.operations import READ_OPS, WRITE_OPS
from ..policy import Decision, PolicyEngine, elicit_approval, elicit_memory_approval
from ..fs.state import ScopeStateStore
from ..fs.tools import (
    build_restrict_tool_handlers,
    restrict_tool_descriptors,
)
from ..runner import Runner

logger = logging.getLogger(__name__)


def _prefixed(name: str, scope_id: Optional[str]) -> str:
    return f"{scope_id}_{name}" if scope_id else name


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
) -> Server:
    server: Server = Server("hares-combined")

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

    # Shell tools — shared inputSchema for both execute_command and
    # execute_command_high_memory.
    _exec_input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "timeout": {"type": "number"},
            "weight": {"type": "integer", "minimum": 1},
            "mem_limit_mb": {
                "type": "integer", "minimum": 1,
                "description": (
                    "Per-call RLIMIT_AS override (MB). Right-size for "
                    "the command — clamped to HARES_MEM_LIMIT_MB."
                ),
            },
            "cpu_limit_sec": {
                "type": "integer", "minimum": 1,
                "description": (
                    "Per-call RLIMIT_CPU override (CPU seconds, not "
                    "wall-clock). Clamped to HARES_CPU_LIMIT_SEC."
                ),
            },
            "stdin": {
                "type": "string",
                "description": (
                    "UTF-8 text written to the child's stdin and then "
                    "closed. Use for commands that read from stdin "
                    "(jq, python -, patch, mail) instead of wrapping "
                    "the call in /bin/sh -c."
                ),
            },
        },
        "required": ["command"],
    }

    shell_name = _prefixed("execute_command", scope_id)
    high_mem_name = _prefixed("execute_command_high_memory", scope_id)

    tool_descriptors.append(Tool(
        name=shell_name,
        description=(
            "Run a shell command under Hares's resource caps + bwrap "
            "sandbox. The bwrap mount list narrows to the active scope "
            "(set via restrict_paths); subprocess writes outside "
            "the scope are kernel-rejected. Per-call mem_limit_mb / "
            "cpu_limit_sec override the operator defaults (clamped down)."
        ),
        inputSchema=_exec_input_schema,
    ))
    handlers[shell_name] = ("shell", None)  # dispatched directly to runner.execute

    tool_descriptors.append(Tool(
        name=high_mem_name,
        description=(
            f"Run a command that needs MORE MEMORY than the normal cap "
            f"(HARES_MEM_LIMIT_MB = {mem_limit_mb} MB) inside the bwrap sandbox. "
            f"REQUIRES USER APPROVAL — a blocking dialog is shown to the user on "
            f"EVERY call; there is no way to skip this. "
            f"The run is cgroup-bounded to a machine-safe maximum "
            f"(HARES_MEM_LIMIT_MAX_MB = {mem_limit_max_mb} MB) so even a "
            f"multi-process memory bomb cannot take down the MCP session. "
            f"Use this for large compiles, simulators, or workloads that "
            f"legitimately exceed the standard cap."
        ),
        inputSchema=_exec_input_schema,
    ))
    handlers[high_mem_name] = ("high_memory", None)

    # Restrict tools — one shared instance for both fs and shell.
    restrict_descriptors = restrict_tool_descriptors(scope_id)
    _, restrict_handlers = build_restrict_tool_handlers(
        scope_id=scope_id,
        ceiling=ceiling,
        state_file=state_file,
        scope_state=scope_state,
        on_change=_on_change,
    )
    tool_descriptors.extend(restrict_descriptors)
    for k, v in restrict_handlers.items():
        handlers[k] = ("restrict", v)

    _roots_applied: list[bool] = [False]

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        if use_roots and not _roots_applied[0]:
            _roots_applied[0] = True
            try:
                import mcp.server as _mcp_server
                ctx = _mcp_server.request_context.get(None)
                if ctx is not None:
                    from ..roots import derive_ceiling_from_roots
                    derived = await derive_ceiling_from_roots(ctx.session)
                    if derived is not None:
                        runner.update_ceiling(derived)
                        logger.info("Ceiling updated from MCP roots: %s", derived)
            except Exception as exc:
                logger.debug("Roots ceiling derivation failed: %s", exc)
        return tool_descriptors

    @server.call_tool()
    @audited(auditor, scope_id=scope_id)
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        entry = handlers.get(name)
        if entry is None:
            raise ValueError(f"Unknown tool: {name}")
        kind, handler = entry
        if kind == "fs":
            result = await handler(
                arguments, ceiling=ceiling, scope=scope_state.current(),
            )
        elif kind == "shell":
            command = arguments["command"]
            if policy is not None and policy.active:
                pr = policy.check(command)
                if pr.decision is Decision.DENY:
                    result = {
                        "exit_code": -1, "stdout": "", "stderr": "",
                        "killed_reason": "rejected_by_policy",
                        "rejected_reason": pr.message,
                        "matched_pattern": pr.matched_pattern,
                    }
                    return [TextContent(type="text", text=json.dumps(result, indent=2))]
                if pr.decision is Decision.ELICIT:
                    if not await elicit_approval(server, command, pr):
                        result = {
                            "exit_code": -1, "stdout": "", "stderr": "",
                            "killed_reason": "rejected_by_policy",
                            "rejected_reason": (
                                f"Command declined by user or elicitation not supported "
                                f"(pattern: {pr.matched_pattern!r})."
                            ),
                            "matched_pattern": pr.matched_pattern,
                        }
                        return [TextContent(type="text", text=json.dumps(result, indent=2))]
            result = await runner.execute(
                command=command,
                cwd=arguments.get("cwd"),
                env=arguments.get("env"),
                timeout=float(arguments.get("timeout", 300.0)),
                weight=int(arguments.get("weight", 1)),
                mem_limit_mb=arguments.get("mem_limit_mb"),
                cpu_limit_sec=arguments.get("cpu_limit_sec"),
                stdin=arguments.get("stdin"),
            )
            # If the command was killed by the cgroup OOM killer, append a
            # hint pointing the caller at the high-memory tool.
            if result.get("killed_reason") == "oom":
                note = result.get("killed_note", "")
                note += (
                    f" Retry via the `{high_mem_name}` tool "
                    f"(it will ask the user to approve a larger allocation)."
                )
                result["killed_note"] = note
        elif kind == "high_memory":
            command = arguments["command"]

            # Policy gate: deny wins unconditionally.
            if policy is not None and policy.active:
                pr = policy.check(command)
                if pr.decision is Decision.DENY:
                    result = {
                        "exit_code": -1, "stdout": "", "stderr": "",
                        "killed_reason": "rejected_by_policy",
                        "rejected_reason": pr.message,
                        "matched_pattern": pr.matched_pattern,
                    }
                    return [TextContent(type="text", text=json.dumps(result, indent=2))]

            # Unconditional memory elicitation — there is NO argument a caller
            # can pass to skip this step.
            requested_mb: int = arguments.get("mem_limit_mb") or mem_limit_max_mb
            approved = await elicit_memory_approval(
                server, command, requested_mb, mem_limit_mb,
            )
            if not approved:
                result = {
                    "exit_code": -1, "stdout": "", "stderr": "",
                    "killed_reason": "rejected_by_policy",
                    "rejected_reason": (
                        "High-memory run declined by user or client does not "
                        "support elicitation."
                    ),
                }
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            result = await runner.execute(
                command=command,
                cwd=arguments.get("cwd"),
                env=arguments.get("env"),
                timeout=float(arguments.get("timeout", 300.0)),
                weight=int(arguments.get("weight", 1)),
                mem_limit_mb=arguments.get("mem_limit_mb"),
                cpu_limit_sec=arguments.get("cpu_limit_sec"),
                stdin=arguments.get("stdin"),
                high_memory=True,
            )
        elif kind == "restrict":
            result = await handler(arguments)
        else:
            raise ValueError(f"Unknown handler kind: {kind!r}")
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

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
