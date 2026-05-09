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
from ..runner import Runner

logger = logging.getLogger(__name__)


def _prefixed(name: str, scope_id: Optional[str]) -> str:
    """Return the tool name, optionally prefixed with ``<scope_id>_``."""
    return f"{scope_id}_{name}" if scope_id else name


def _build_server(
    runner: Runner,
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
    read_only: bool = False,
    state_file: Optional[Path] = None,
    auditor: Optional["Auditor"] = None,
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

    exec_tool_name = _prefixed("execute_command", scope_id)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        tools: list[Tool] = [
            Tool(
                name=exec_tool_name,
                description=(
                    "Run a shell command under Hares's resource caps. "
                    "Memory is limited to HARES_MEM_LIMIT_MB per process, "
                    "CPU to HARES_CPU_LIMIT_SEC seconds, and only "
                    "HARES_MAX_CONCURRENT commands run at once across "
                    "all callers (GLOBAL when HARES_COORDINATION_DIR is "
                    "set, else per-process). Subprocesses are pinned to "
                    "a small CPU set so tools like pytest-xdist auto-"
                    "detect a safe worker count. Known overcommit "
                    "patterns (e.g., `pytest -n auto`, `make -j`) are "
                    "rewritten to fit the cap; the rewrites are reported "
                    "in stdout and in the `rewrites` field of the result."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command to run (interpreted by /bin/sh -c).",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Working directory. Defaults to the server's cwd.",
                        },
                        "env": {
                            "type": "object",
                            "description": "Environment overrides merged on top of the server's env.",
                            "additionalProperties": {"type": "string"},
                        },
                        "timeout": {
                            "type": "number",
                            "description": "Wall-clock timeout in seconds. Defaults to 300.",
                        },
                        "weight": {
                            "type": "integer",
                            "description": (
                                "How many semaphore slots (and pinned cores) to occupy. "
                                "Heavy commands (parallel pytest, builds) can pass weight=2; "
                                "capped at HARES_MAX_CONCURRENT."
                            ),
                            "minimum": 1,
                        },
                        "mem_limit_mb": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "Per-call RLIMIT_AS override (MB). RIGHT-SIZE THIS — "
                                "small inspection commands (ls, cat, grep) need 64-256 MB; "
                                "test runs and small builds 1024-4096 MB; large compiles "
                                "or simulators 8192+ MB. Clamped to HARES_MEM_LIMIT_MB "
                                "(operator hard ceiling). Setting it lower means the "
                                "kernel kill fires earlier if the command unexpectedly "
                                "balloons — better debugging signal than letting it "
                                "consume the global default. Defaults to HARES_MEM_LIMIT_MB."
                            ),
                        },
                        "cpu_limit_sec": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "Per-call RLIMIT_CPU override (seconds of CPU time, "
                                "not wall-clock — see 'timeout' for that). Clamped to "
                                "HARES_CPU_LIMIT_SEC. Use a tight value for inspection "
                                "commands so a runaway loop dies via SIGXCPU instead of "
                                "the wall-clock fallback. Defaults to HARES_CPU_LIMIT_SEC."
                            ),
                        },
                        "stdin": {
                            "type": "string",
                            "description": (
                                "UTF-8 text written to the child's stdin and then "
                                "closed (so the child sees EOF). Use this for commands "
                                "that read from stdin — `jq '.x'`, `python -`, `patch`, "
                                "`mail`, etc. — instead of wrapping the whole thing in "
                                "/bin/sh -c with shell-side echo/heredoc. When omitted, "
                                "stdin behavior is unchanged from prior versions."
                            ),
                        },
                    },
                    "required": ["command"],
                },
            ),
        ]
        tools.extend(restrict_descriptors)
        return tools

    @server.call_tool()
    @audited(auditor, scope_id=scope_id)
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == exec_tool_name:
            result = await runner.execute(
                command=arguments["command"],
                cwd=arguments.get("cwd"),
                env=arguments.get("env"),
                timeout=float(arguments.get("timeout", 300.0)),
                weight=int(arguments.get("weight", 1)),
                mem_limit_mb=arguments.get("mem_limit_mb"),
                cpu_limit_sec=arguments.get("cpu_limit_sec"),
                stdin=arguments.get("stdin"),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        if name in restrict_handlers:
            result = await restrict_handlers[name](arguments)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        raise ValueError(f"Unknown tool: {name}")

    return server


async def _serve_async(
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
    read_only: bool = False,
    state_file: Optional[Path] = None,
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
    runner = Runner(
        max_concurrent=cfg.max_concurrent,
        mem_limit_mb=cfg.mem_limit_mb,
        cpu_limit_sec=cfg.cpu_limit_sec,
        rss_poll_interval=cfg.rss_poll_interval,
        rss_overshoot_ratio=cfg.rss_overshoot_ratio,
        sandbox=cfg.sandbox,
        coordinator=coord,
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
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def serve(
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
    read_only: bool = False,
    state_file: Optional[Path] = None,
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
