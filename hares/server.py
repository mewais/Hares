"""Hares MCP stdio server.

Thin wrapper around `Runner` that exposes a single tool —
`execute_command` — over the MCP stdio transport. Designed to be
launched from any MCP-aware client's config:

    {
      "mcpServers": {
        "shell": {
          "command": "hares-mcp",
          "env": {
            "HARES_MAX_CONCURRENT": "2",
            "HARES_MEM_LIMIT_MB":   "7168",
            "HARES_CPU_LIMIT_SEC":  "1200"
          }
        }
      }
    }

The MCP framing uses the official `mcp` Python SDK; we just register
one tool and let the SDK handle handshake / tools/list / tools/call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .config import load_config
from .runner import Runner

logger = logging.getLogger(__name__)


def _build_server(runner: Runner) -> Server:
    """Build the MCP Server with the single execute_command tool wired
    to the supplied runner. Factored out so tests can inject a mock
    runner if needed."""
    server: Server = Server("hares")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="execute_command",
                description=(
                    "Run a shell command under Hares's resource caps. "
                    "Memory is limited to HARES_MEM_LIMIT_MB per process, "
                    "CPU to HARES_CPU_LIMIT_SEC seconds, and only "
                    "HARES_MAX_CONCURRENT commands run at once across "
                    "all callers. Subprocesses are pinned to a small CPU "
                    "set so tools like pytest-xdist auto-detect a safe "
                    "worker count. Known overcommit patterns (e.g., "
                    "`pytest -n auto`, `make -j`) are rewritten to fit "
                    "the cap; the rewrites are reported in stdout and in "
                    "the `rewrites` field of the result."
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
                    },
                    "required": ["command"],
                },
            ),
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name != "execute_command":
            raise ValueError(f"Unknown tool: {name}")
        result = await runner.execute(
            command=arguments["command"],
            cwd=arguments.get("cwd"),
            env=arguments.get("env"),
            timeout=float(arguments.get("timeout", 300.0)),
            weight=int(arguments.get("weight", 1)),
        )
        # Return as a single text block of pretty JSON. Agents parse
        # this back; the wrapping keeps stdout/stderr inline so the
        # LLM sees them in context.
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


async def _serve() -> None:
    cfg = load_config(default_cwd=os.getcwd())
    logger.info(
        "Hares starting: max_concurrent=%d, mem=%dMB, cpu=%ds, sandbox=%s",
        cfg.max_concurrent, cfg.mem_limit_mb, cfg.cpu_limit_sec,
        "bwrap" if cfg.sandbox.enabled else "off",
    )
    if cfg.sandbox.enabled:
        logger.info(
            "Sandbox: rw_binds=%s ro_binds=%s network=%s",
            list(cfg.sandbox.rw_binds), list(cfg.sandbox.ro_binds),
            "on" if cfg.sandbox.allow_network else "off",
        )
    runner = Runner(
        max_concurrent=cfg.max_concurrent,
        mem_limit_mb=cfg.mem_limit_mb,
        cpu_limit_sec=cfg.cpu_limit_sec,
        rss_poll_interval=cfg.rss_poll_interval,
        rss_overshoot_ratio=cfg.rss_overshoot_ratio,
        sandbox=cfg.sandbox,
    )
    server = _build_server(runner)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    """Console-script entry point (`hares-mcp`)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s hares: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        logger.info("Hares shutting down (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
