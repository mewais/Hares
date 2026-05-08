"""Hares cluster MCP server — exposes the five cluster-scheduler tools.

Parameterized by scheduler so LSF and SLURM share one server module.
The tool-name prefix (``lsf`` or ``slurm``) and the descriptions vary;
the dispatch logic does not.

Tool surface (with PFX = scheduler name):

  <scope_>PFX_execute_blocking  Submit one job and wait for it.
  <scope_>PFX_submit            Submit one or more jobs (non-blocking).
  <scope_>PFX_wait              Wait for a list of jobs to finish.
  <scope_>PFX_cancel            Cancel a list of jobs.
  <scope_>PFX_jobs              List all jobs submitted in this session.

Security note printed in every tool description: no bwrap, no RLIMIT,
no active-scope enforcement. The cluster node runs jobs unrestricted.
Pre-submission ceiling check on cwd is best-effort only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..audit import Auditor, audited, load_auditor
from .base import ClusterExecutor, JobSpec
from .lsf import LsfExecutor, load_lsf_config
from .slurm import SlurmExecutor, load_slurm_config

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


def _resource_spec_help(scheduler: str) -> str:
    sizing_guidance = (
        "RIGHT-SIZE THIS PER JOB. Schedulers prioritize jobs whose "
        "resource asks fit in current cluster slack — small asks "
        "(e.g. 512 MB / 5 min for a smoke test) start almost "
        "immediately, while over-allocated jobs wait in the queue "
        "for matching nodes to free up. Estimate from the actual "
        "command: a unit-test run is not a multi-GPU training job. "
        "When omitted, the operator-configured default applies, "
        "which is typically sized for the largest expected job."
    )
    if scheduler == "lsf":
        return (
            "LSF resource specification passed to bsub -R. "
            "Examples: 'rusage[mem=512]' for a small test, "
            "'rusage[mem=8192] span[hosts=1]' for a single-node build, "
            "'rusage[mem=32768,ngpus_excl_p=1]' for GPU work. "
            "Overrides HARES_LSF_DEFAULT_RESOURCE_SPEC for this job. "
            + sizing_guidance
        )
    if scheduler == "slurm":
        return (
            "SLURM resource flags appended to sbatch verbatim "
            "(shlex-split). Examples: "
            "'--mem=512 --time=00:05:00' for a smoke test, "
            "'--mem=8192 --cpus-per-task=4 --time=01:00:00' for a "
            "single-node build, '--mem=32768 --gres=gpu:1 --time=04:00:00' "
            "for GPU work. Overrides HARES_SLURM_DEFAULT_RESOURCE_SPEC "
            "for this job. " + sizing_guidance
        )
    return "Scheduler-specific resource specification."


def _name_flag_help(scheduler: str) -> str:
    flag = "bsub -J" if scheduler == "lsf" else "sbatch --job-name"
    return f"Human-readable job name ({flag}). Auto-generated if omitted."


def build_server(
    executor: ClusterExecutor,
    *,
    scheduler: str,
    scope_id: Optional[str] = None,
    auditor: Optional[Auditor] = None,
) -> Server:
    """Build an MCP Server exposing the five cluster tools for one backend."""
    server: Server = Server(f"hares-{scheduler}")

    # Pre-compute all tool names once.
    T_BLOCKING = _p(f"{scheduler}_execute_blocking", scope_id)
    T_SUBMIT   = _p(f"{scheduler}_submit",           scope_id)
    T_WAIT     = _p(f"{scheduler}_wait",             scope_id)
    T_CANCEL   = _p(f"{scheduler}_cancel",           scope_id)
    T_JOBS     = _p(f"{scheduler}_jobs",             scope_id)
    ALL_TOOLS  = {T_BLOCKING, T_SUBMIT, T_WAIT, T_CANCEL, T_JOBS}

    timeout_env = (
        "HARES_LSF_DEFAULT_TIMEOUT_SEC" if scheduler == "lsf"
        else "HARES_SLURM_DEFAULT_TIMEOUT_SEC"
    )
    poll_env = (
        "HARES_LSF_POLL_INTERVAL_SEC" if scheduler == "lsf"
        else "HARES_SLURM_POLL_INTERVAL_SEC"
    )

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
                    "description": _resource_spec_help(scheduler),
                },
                "name": {
                    "type": "string",
                    "description": _name_flag_help(scheduler),
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
                    f"Submit a single job to {scheduler.upper()} and wait "
                    "(blocking) until it completes. Returns stdout, stderr, "
                    f"exit_code, and status. Use this for single sequential jobs. "
                    f"Use {scheduler}_submit + {scheduler}_wait when you have "
                    "multiple independent jobs to run in parallel. "
                    + _SECURITY_NOTE
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        **job_spec_schema["properties"],
                        "timeout_sec": {
                            "type": "number",
                            "description": (
                                f"Maximum seconds to wait for the job to finish. "
                                f"Defaults to ${timeout_env} (86400). "
                                "On timeout the job keeps running; call "
                                f"{scheduler}_cancel to stop it."
                            ),
                        },
                    },
                    "required": ["command"],
                },
            ),
            Tool(
                name=T_SUBMIT,
                description=(
                    f"Submit one or more jobs to {scheduler.upper()} without "
                    "waiting. Returns a job_id for each submitted job. Call "
                    f"{scheduler}_wait with the returned job_ids to collect "
                    "results. Submitting multiple jobs in one call allows them "
                    "to run in parallel on the cluster. " + _SECURITY_NOTE
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
                    f"Wait for one or more {scheduler.upper()} jobs (by "
                    f"job_id) to finish. Polls every ${poll_env} seconds. "
                    "Returns as soon as all listed jobs reach a terminal state "
                    "or timeout_sec elapses. Timed-out jobs keep running on "
                    f"the cluster — call {scheduler}_cancel to stop them."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                f"Job IDs returned by {scheduler}_submit or "
                                f"{scheduler}_execute_blocking."
                            ),
                            "minItems": 1,
                        },
                        "timeout_sec": {
                            "type": "number",
                            "description": (
                                f"Maximum seconds to wait. "
                                f"Defaults to ${timeout_env} (86400)."
                            ),
                        },
                    },
                    "required": ["job_ids"],
                },
            ),
            Tool(
                name=T_CANCEL,
                description=(
                    f"Cancel one or more {scheduler.upper()} jobs. Returns a "
                    "per-job result indicating whether the cancel succeeded. "
                    "Jobs that have already finished are typically silently "
                    "ignored by the scheduler."
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
                    f"List all {scheduler.upper()} jobs submitted in this "
                    "Hares session with their current status. Useful for "
                    "recovering job_ids if they were lost from context, or "
                    "for auditing what is running on the cluster."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    @server.call_tool()
    @audited(auditor, scope_id=scope_id)
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


# ── Per-scheduler entry points ───────────────────────────────────────────────

async def _serve_lsf_async(
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
    auditor = load_auditor()
    if auditor is not None:
        logger.info("Audit log enabled: dest=%r", auditor.dest)
    server = build_server(executor, scheduler="lsf", scope_id=scope_id, auditor=auditor)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


async def _serve_slurm_async(
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
) -> None:
    session_tmp = Path(tempfile.mkdtemp(prefix="hares-slurm-out-"))
    cfg = load_slurm_config(session_tmp=session_tmp)
    logger.info(
        "Hares SLURM starting: scope_id=%r ceiling=%r partition=%r account=%r "
        "poll_interval=%.1fs default_timeout=%.0fs output_dir=%s",
        scope_id,
        str(ceiling) if ceiling else None,
        cfg.partition,
        cfg.account,
        cfg.poll_interval_sec,
        cfg.default_timeout_sec,
        cfg.output_dir,
    )
    executor = SlurmExecutor(cfg=cfg, ceiling=ceiling)
    auditor = load_auditor()
    if auditor is not None:
        logger.info("Audit log enabled: dest=%r", auditor.dest)
    server = build_server(executor, scheduler="slurm", scope_id=scope_id, auditor=auditor)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def serve_lsf(
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
) -> None:
    """Synchronous entry — called by the CLI dispatcher for --enable=lsf."""
    try:
        asyncio.run(_serve_lsf_async(scope_id=scope_id, ceiling=ceiling))
    except KeyboardInterrupt:
        logger.info("Hares LSF shutting down (KeyboardInterrupt)")


def serve_slurm(
    *,
    scope_id: Optional[str] = None,
    ceiling: Optional[Path] = None,
) -> None:
    """Synchronous entry — called by the CLI dispatcher for --enable=slurm."""
    try:
        asyncio.run(_serve_slurm_async(scope_id=scope_id, ceiling=ceiling))
    except KeyboardInterrupt:
        logger.info("Hares SLURM shutting down (KeyboardInterrupt)")
