"""Shared ``execute_command`` / ``execute_command_high_memory`` tool
plumbing used by both the shell and combined MCP servers.

Modeled on :mod:`hares.fs.tools` (which shares the restrict tools the
same way): this module exposes tool *descriptors* plus bound async
*handlers* that the servers register, keeping the server modules thin
MCP wiring.

What is shared here:

* :func:`prefixed` — the ``<scope_id>_<name>`` tool-name convention.
* :data:`EXEC_INPUT_SCHEMA` — the single input schema both tools use in
  both server flavors (the parameters are identical across modes).
* Descriptor builders (:func:`shell_exec_tool_descriptors`,
  :func:`combined_exec_tool_descriptors`) — same schema; only the
  top-level tool description differs (the combined one adds the
  bwrap-sandbox framing).
* :func:`build_exec_tool_handlers` — the full handler logic for both
  tools: policy deny/elicit gating, the unconditional memory-approval
  elicitation for the high-memory tool, the ``runner.execute(...)``
  call, and the OOM-hint appending.
* :func:`make_roots_refiner` — the one-shot "refine the ceiling from
  MCP roots on the first list_tools() call" behavior.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from mcp.server import Server
from mcp.types import Tool

from .policy import Decision, PolicyEngine, elicit_approval, elicit_memory_approval
from .runner import ExecuteResult, Runner

logger = logging.getLogger(__name__)


EXECUTE_COMMAND_TOOL = "execute_command"
EXECUTE_COMMAND_HIGH_MEMORY_TOOL = "execute_command_high_memory"


def prefixed(name: str, scope_id: Optional[str]) -> str:
    """Return the tool name, optionally prefixed with ``<scope_id>_``."""
    return f"{scope_id}_{name}" if scope_id else name


# ── Input schema ───────────────────────────────────────────────────────
#
# execute_command and execute_command_high_memory share ONE inputSchema,
# used by both the shell and combined (fs+shell) servers. The parameters
# are identical across modes; only the top-level tool *description*
# legitimately differs (the combined descriptor adds the bwrap-sandbox
# framing). Keeping a single schema keeps the agent-facing parameter
# guidance from silently diverging between the two modes.

EXEC_INPUT_SCHEMA: dict = {
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
                "How many concurrency slots (and pinned cores) to occupy. "
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
                "/bin/sh -c with shell-side echo/heredoc."
            ),
        },
    },
    "required": ["command"],
}


# ── Descriptors ────────────────────────────────────────────────────────


def shell_exec_tool_descriptors(
    scope_id: Optional[str],
    *,
    mem_limit_mb: int,
    mem_limit_max_mb: int,
) -> list[Tool]:
    """The two exec-tool descriptors as advertised by the shell server."""
    return [
        Tool(
            name=prefixed(EXECUTE_COMMAND_TOOL, scope_id),
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
            inputSchema=EXEC_INPUT_SCHEMA,
        ),
        Tool(
            name=prefixed(EXECUTE_COMMAND_HIGH_MEMORY_TOOL, scope_id),
            description=(
                f"Run a command that needs MORE MEMORY than the normal cap "
                f"(HARES_MEM_LIMIT_MB = {mem_limit_mb} MB). "
                f"REQUIRES USER APPROVAL — a blocking dialog is shown to the "
                f"user on EVERY call; there is no way to skip this. "
                f"The run is cgroup-bounded to a machine-safe maximum "
                f"(HARES_MEM_LIMIT_MAX_MB = {mem_limit_max_mb} MB) so even a "
                f"multi-process memory bomb cannot take down the MCP session — "
                f"the kernel OOM killer is scoped to the command's cgroup. "
                f"Use this for large compiles, simulators, or any workload that "
                f"legitimately exceeds the standard cap. Provide mem_limit_mb to "
                f"request a specific budget; omit to request the machine maximum."
            ),
            inputSchema=EXEC_INPUT_SCHEMA,
        ),
    ]


def combined_exec_tool_descriptors(
    scope_id: Optional[str],
    *,
    mem_limit_mb: int,
    mem_limit_max_mb: int,
) -> list[Tool]:
    """The two exec-tool descriptors as advertised by the combined server."""
    return [
        Tool(
            name=prefixed(EXECUTE_COMMAND_TOOL, scope_id),
            description=(
                "Run a shell command under Hares's resource caps inside a "
                "bwrap sandbox. Memory is limited to HARES_MEM_LIMIT_MB per "
                "process, CPU to HARES_CPU_LIMIT_SEC seconds, and only "
                "HARES_MAX_CONCURRENT commands run at once across all callers "
                "(GLOBAL when HARES_COORDINATION_DIR is set, else per-process). "
                "The sandbox mounts ONLY the active scope (set via "
                "restrict_paths) writable — writes outside it are kernel-"
                "rejected with EROFS, so prefer paths under the active scope "
                "and use restrict_paths to widen it. Subprocesses are pinned "
                "to a small CPU set so tools like pytest-xdist auto-detect a "
                "safe worker count. Known overcommit patterns (e.g., "
                "`pytest -n auto`, `make -j`) are rewritten to fit the cap; "
                "the rewrites are reported in stdout and in the `rewrites` "
                "field of the result."
            ),
            inputSchema=EXEC_INPUT_SCHEMA,
        ),
        Tool(
            name=prefixed(EXECUTE_COMMAND_HIGH_MEMORY_TOOL, scope_id),
            description=(
                f"Run a command that needs MORE MEMORY than the normal cap "
                f"(HARES_MEM_LIMIT_MB = {mem_limit_mb} MB) inside the bwrap sandbox. "
                f"REQUIRES USER APPROVAL — a blocking dialog is shown to the user on "
                f"EVERY call; there is no way to skip this. "
                f"The run is cgroup-bounded to a machine-safe maximum "
                f"(HARES_MEM_LIMIT_MAX_MB = {mem_limit_max_mb} MB) so even a "
                f"multi-process memory bomb cannot take down the MCP session — "
                f"the kernel OOM killer is scoped to the command's cgroup. "
                f"Use this for large compiles, simulators, or any workload that "
                f"legitimately exceeds the standard cap. Provide mem_limit_mb to "
                f"request a specific budget; omit to request the machine maximum."
            ),
            inputSchema=EXEC_INPUT_SCHEMA,
        ),
    ]


# ── Handlers ───────────────────────────────────────────────────────────

# The ``rejected_reason`` text emitted when the user (or a client with
# no elicitation support) declines an elicit-gated command. The two
# servers historically emit slightly different wording; both are kept
# verbatim because emitted result payloads are frozen. ``{pattern!r}``
# is filled with the policy pattern that triggered the elicitation.
SHELL_ELICIT_DECLINE_TEMPLATE = (
    "Command declined by user or client does not "
    "support elicitation (pattern: {pattern!r})."
)
COMBINED_ELICIT_DECLINE_TEMPLATE = (
    "Command declined by user or elicitation not supported "
    "(pattern: {pattern!r})."
)


def _policy_rejection(pr) -> dict:
    """The result payload for a policy DENY (or declined elicitation
    with a matched pattern)."""
    return {
        "exit_code": -1, "stdout": "", "stderr": "",
        "killed_reason": "rejected_by_policy",
        "rejected_reason": pr.message,
        "matched_pattern": pr.matched_pattern,
    }


def build_exec_tool_handlers(
    *,
    server: Server,
    runner: Runner,
    scope_id: Optional[str],
    policy: Optional[PolicyEngine],
    mem_limit_mb: int,
    mem_limit_max_mb: int,
    elicit_decline_template: str,
) -> dict[str, Callable[[dict], Awaitable[dict]]]:
    """Construct the bound async handlers for ``execute_command`` and
    ``execute_command_high_memory``.

    Args:
      server: The MCP Server instance — needed because elicitation
        (``elicit_approval`` / ``elicit_memory_approval``) reads the
        request context off the server instance.
      runner: The subprocess executor.
      scope_id: Optional tool-name prefix.
      policy: Optional command-policy engine. DENY patterns reject the
        command outright (for BOTH tools — deny wins unconditionally);
        ELICIT patterns ask the user first (normal tool only — the
        high-memory tool already elicits on every call).
      mem_limit_mb: Operator default cap (shown in approval dialogs).
      mem_limit_max_mb: Machine-safe maximum (the default high-memory
        budget when the caller passes no ``mem_limit_mb``).
      elicit_decline_template: ``rejected_reason`` text used when an
        elicit-gated command is declined; ``{pattern!r}`` is filled
        with the matched policy pattern.

    Returns:
      Mapping of (prefixed) tool name → async handler taking the raw
      arguments dict and returning a JSON-serializable result dict.
      The servers wrap the result in TextContent themselves.
    """
    exec_name = prefixed(EXECUTE_COMMAND_TOOL, scope_id)
    high_mem_name = prefixed(EXECUTE_COMMAND_HIGH_MEMORY_TOOL, scope_id)

    async def _run(arguments: dict, *, high_memory: bool) -> ExecuteResult:
        return await runner.execute(
            command=arguments["command"],
            cwd=arguments.get("cwd"),
            env=arguments.get("env"),
            timeout=float(arguments.get("timeout", 300.0)),
            weight=int(arguments.get("weight", 1)),
            mem_limit_mb=arguments.get("mem_limit_mb"),
            cpu_limit_sec=arguments.get("cpu_limit_sec"),
            stdin=arguments.get("stdin"),
            high_memory=high_memory,
        )

    async def _execute(arguments: dict) -> dict:
        command = arguments["command"]

        # Policy gate: deny → immediate error, elicit → ask user, allow → run.
        if policy is not None and policy.active:
            pr = policy.check(command)
            if pr.decision is Decision.DENY:
                return _policy_rejection(pr)
            if pr.decision is Decision.ELICIT:
                # Pass the server instance — request_context is an
                # instance attr, not a module-level attr.
                approved = await elicit_approval(server, command, pr)
                if not approved:
                    return {
                        "exit_code": -1, "stdout": "", "stderr": "",
                        "killed_reason": "rejected_by_policy",
                        "rejected_reason": elicit_decline_template.format(
                            pattern=pr.matched_pattern,
                        ),
                        "matched_pattern": pr.matched_pattern,
                    }

        result = await _run(arguments, high_memory=False)
        # If the command was killed by the cgroup OOM killer, append a
        # hint pointing the caller at the high-memory tool.
        if result.get("killed_reason") == "oom":
            note = result.get("killed_note", "")
            note += (
                f" Retry via the `{high_mem_name}` tool "
                f"(it will ask the user to approve a larger allocation)."
            )
            result["killed_note"] = note
        return result

    async def _execute_high_memory(arguments: dict) -> dict:
        command = arguments["command"]

        # Policy gate: deny wins unconditionally — even high-memory calls
        # are blocked if the operator has denied the pattern.
        if policy is not None and policy.active:
            pr = policy.check(command)
            if pr.decision is Decision.DENY:
                return _policy_rejection(pr)

        # Unconditional memory elicitation — there is NO argument a caller
        # can pass to skip this step.  The requested budget is either the
        # caller-supplied mem_limit_mb or the machine-safe maximum.
        # The input schema declares `minimum: 1`, but don't rely on the
        # client validating it: reject a non-positive explicit value here
        # rather than letting `... or mem_limit_max_mb` silently promote a
        # 0 to the machine max (a value the user never asked to approve).
        raw_mb = arguments.get("mem_limit_mb")
        if raw_mb is not None and raw_mb < 1:
            return {
                "exit_code": -1, "stdout": "", "stderr": "",
                "killed_reason": "rejected_by_policy",
                "rejected_reason": (
                    f"mem_limit_mb must be a positive integer (got {raw_mb!r})."
                ),
            }
        requested_mb: int = raw_mb or mem_limit_max_mb
        approved = await elicit_memory_approval(
            server, command, requested_mb, mem_limit_mb,
        )
        if not approved:
            return {
                "exit_code": -1, "stdout": "", "stderr": "",
                "killed_reason": "rejected_by_policy",
                "rejected_reason": (
                    "High-memory run declined by user or client does not "
                    "support elicitation."
                ),
            }

        return await _run(arguments, high_memory=True)

    return {
        exec_name: _execute,
        high_mem_name: _execute_high_memory,
    }


# ── Roots refinement ───────────────────────────────────────────────────


def make_roots_refiner(
    runner: Runner,
    use_roots: bool,
) -> Callable[[], Awaitable[None]]:
    """Return a one-shot async callable that refines the runner's
    ceiling from the MCP client's declared roots.

    The servers call it at the top of every ``list_tools()`` handler
    (always fired before any tool call); only the FIRST call does the
    work — subsequent calls (and all calls when ``use_roots`` is False)
    return immediately. Failures are silent-and-graceful per
    :mod:`hares.roots`.
    """
    applied: list[bool] = [False]

    async def _maybe_refine() -> None:
        if not use_roots or applied[0]:
            return
        applied[0] = True
        try:
            import mcp.server as _mcp_server
            ctx = _mcp_server.request_context.get(None)
            if ctx is not None:
                from .roots import derive_ceiling_from_roots
                derived = await derive_ceiling_from_roots(ctx.session)
                if derived is not None:
                    runner.update_ceiling(derived)
                    logger.info(
                        "Ceiling updated from MCP roots: %s", derived,
                    )
        except Exception as exc:
            logger.debug("Roots ceiling derivation failed: %s", exc)

    return _maybe_refine
