"""Hares LSF MCP server — exposes the five LSF tools.

Tool surface:

  <prefix>lsf_execute_blocking  Submit one job and wait for it.
  <prefix>lsf_submit            Submit one or more jobs (non-blocking).
  <prefix>lsf_wait              Wait for a list of jobs to finish.
  <prefix>lsf_cancel            Cancel a list of jobs via bkill.
  <prefix>lsf_jobs              List all jobs submitted in this session.

<prefix> is ``<scope_id>_`` when --scope-id is supplied, empty otherwise.

Security note printed in every tool description: no bwrap, no RLIMIT,
no active-scope enforcement. The cluster node runs jobs unrestricted.
Pre-submission ceiling check on cwd is best-effort only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .executor import JobSpec, LsfExecutor, load_lsf_config

logger = logging.getLogger(__name__)

_SECURITY_NOTE = (
    "SECURITY: No bwrap sandbox, no RLIMIT, and no active-scope "
    "enforcement apply to cluster-side execution. Jobs run on a cluster "
    "node with the submitting user's full filesystem permissions. "
    "Pre-submission ceiling check on cwd is best-effort only."
)


def _p(name: str, scope_id: Optional[str]) -> str:
    """Apply scope_id prefix to a tool name."""
    return f"{scope_id}_{name}" if scope_id else name


def _build_server(
    executor: LsfExecutor,
    *,
    scope_id: Optional[str] = None,
) -> Server:
    server: Server = Server("hares-lsf")

    # Pre-compute all tool names once.
    T_BLOCKING = _p("lsf_execute_blocking", scope_id)
    T_SUBMIT   = _p("lsf_submit",           scope_id)
    T_WAIT     = _p("lsf_wait",             scope_id)
    T_CANCEL   = _p("lsf_cancel",           scope_id)
    T_JOBS     = _p("lsf_jobs",             scope_id)
    ALL_TOOLS  = {T_BLOCKING, T_SUBMIT, T_WAIT, T_CANCEL, T_JOBS}

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        job_spec_schema = {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run on the cluster node (via /bin/sh -c).",
                },
                "resource_spec": {
                    "type": "string",
                    "description": (
                        "LSF resource specification passed to bsub -R "
                        "(e.g. 'rusage[mem=8192] span[hosts=1]'). "
                        "Overrides HARES_LSF_DEFAULT_RESOURCE_SPEC for this job."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable job name (bsub -J). Auto-generated if omitted.",
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Working directory for the job on the cluster node. "
                        "Must be on a shared filesystem visible to cluster nodes. "
                        "Pre-submission ceiling check is applied when a ceiling is configured."
                    ),
                },
                "env": {
                    "type": "object",
                    "description": "Environment variable overrides exported inside the job.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["command"],
        }

        return [
            Tool(
                name=T_BLOCKING,
                description=(
                    "Submit a single job to LSF and wait (blocking) until it "
                    "completes. Returns stdout, stderr, exit_code, and status. "
                    "Use this for single sequential jobs. Use lsf_submit + "
                    "lsf_wait when you have multiple independent jobs to run "
                    "in parallel. " + _SECURITY_NOTE
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        **job_spec_schema["properties"],
                        "timeout_sec": {
                            "type": "number",
                            "description": (
                                "Maximum seconds to wait for the job to finish. "
                                "Defaults to HARES_LSF_DEFAULT_TIMEOUT_SEC (86400). "
                                "On timeout the job keeps running; call lsf_cancel to stop it."
                            ),
                        },
                    },
                    "required": ["command"],
                },
            ),
            Tool(
                name=T_SUBMIT,
                description=(
                    "Submit one or more jobs to LSF without waiting. Returns a "
                    "job_id for each submitted job. Call lsf_wait with the "
                    "returned job_ids to collect results. Submitting multiple "
                    "jobs in one call allows them to run in parallel on the "
                    "cluster. " + _SECURITY_NOTE
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "jobs": {
                            "type": "array",
                            "description": "List of jobs to submit.",
                            "items": job_spec_schema,
                            "minItems": 1,
                        },
                    },
                    "required": ["jobs"],
                },
            ),
            Tool(
                name=T_WAIT,
                description=(
                    "Wait for one or more LSF jobs (by job_id) to finish. "
                    "Polls bjobs every HARES_LSF_POLL_INTERVAL_SEC seconds. "
                    "Returns as soon as all listed jobs reach a terminal state "
                    "(DONE or EXIT) or timeout_sec elapses. Timed-out jobs "
                    "keep running on the cluster — call lsf_cancel to stop them."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Job IDs returned by lsf_submit or lsf_execute_blocking.",
                            "minItems": 1,
                        },
                        "timeout_sec": {
                            "type": "number",
                            "description": (
                                "Maximum seconds to wait. "
                                "Defaults to HARES_LSF_DEFAULT_TIMEOUT_SEC (86400)."
                            ),
                        },
                    },
                    "required": ["job_ids"],
                },
            ),
            Tool(
                name=T_CANCEL,
                description=(
                    "Cancel one or more LSF jobs via bkill. Returns a per-job "
                    "result indicating whether bkill succeeded. Jobs that have "
                    "already finished are silently ignored by bkill."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Job IDs to cancel.",
                            "minItems": 1,
                        },
                    },
                    "required": ["job_ids"],
                },
            ),
            Tool(
                name=T_JOBS,
                description=(
                    "List all LSF jobs submitted in this Hares session with "
                    "their current status. Useful for recovering job_ids if "
                    "they were lost from context, or for auditing what is "
                    "running on the cluster."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name not in ALL_TOOLS:
            raise ValueError(f"Unknown tool: {name!r}")

        if name == T_BLOCKING:
            spec = JobSpec(
                command=arguments["command"],
                resource_spec=arguments.get("resource_spec"),
                name=arguments.get("name"),
                cwd=arguments.get("cwd"),
                env=arguments.get("env"),
            )
            timeout = float(
                arguments.get("timeout_sec", executor._cfg.default_timeout_sec)
            )
            result = await executor.execute_blocking(spec, timeout)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == T_SUBMIT:
            specs = [
                JobSpec(
                    command=j["command"],
                    resource_spec=j.get("resource_spec"),
                    name=j.get("name"),
                    cwd=j.get("cwd"),
                    env=j.get("env"),
                )
                for j in arguments["jobs"]
            ]
            result = await executor.submit(specs)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == T_WAIT:
            job_ids = arguments["job_ids"]
            timeout = float(
                arguments.get("timeout_sec", executor._cfg.default_timeout_sec)
            )
            result = await executor.wait(job_ids, timeout)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == T_CANCEL:
            result = await executor.cancel(arguments["job_ids"])
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == T_JOBS:
            result = await executor.jobs()
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        raise ValueError(f"Unhandled tool: {name!r}")  # unreachable

    return server


async def _serve_async(
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
) -> None:
    session_tmp = Path(tempfile.mkdtemp(prefix="hares-lsf-out-"))
    cfg = load_lsf_config(session_tmp=session_tmp)
    logger.info(
        "Hares LSF starting: scope_id=%r ceiling=%r queue=%r "
        "poll_interval=%.1fs default_timeout=%.0fs output_dir=%s",
        scope_id,
        str(ceiling) if ceiling else None,
        cfg.queue,
        cfg.poll_interval_sec,
        cfg.default_timeout_sec,
        cfg.output_dir,
    )
    executor = LsfExecutor(cfg=cfg, ceiling=ceiling)
    server = _build_server(executor, scope_id=scope_id)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def serve(
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
) -> None:
    """Synchronous entry — called by the CLI dispatcher."""
    try:
        asyncio.run(_serve_async(scope_id=scope_id, ceiling=ceiling))
    except KeyboardInterrupt:
        logger.info("Hares LSF shutting down (KeyboardInterrupt)")
