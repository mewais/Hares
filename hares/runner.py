"""Resource-capped subprocess executor.

The Runner class owns:
  - A global asyncio.Semaphore enforcing max concurrent commands.
  - Pre-flight command inspection (rewrites known overcommit patterns
    like `pytest -n auto`, `make -jN`, etc. to fit the per-command
    worker budget).
  - Per-subprocess CPU affinity pinning (Linux only) so tools that
    auto-detect CPU count see only the cores allocated to them.
  - Per-subprocess RLIMIT_AS (virtual address space) and RLIMIT_CPU
    enforced via preexec_fn — kernel-level, no escape.
  - An optional psutil-based RSS poller that aggregates across the
    process tree (catches xdist worker sprawl that RLIMIT_AS misses
    because each worker has its own address space).
  - Wall-clock timeout via asyncio.wait_for + killpg of the whole
    process group on overrun.

Subprocesses are launched in a new session (os.setsid) so we can
SIGKILL the entire process tree with killpg without leaking workers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import signal
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from .inspector import format_rewrite_notice, inspect_command
from .sandbox import SandboxConfig, build_bwrap_argv

logger = logging.getLogger(__name__)

# Avoid a hard import-time dependency on coordination.py; type-only.
if False:  # TYPE_CHECKING
    from .coordination import CrossProcessCoordinator

try:
    import psutil  # type: ignore[import-untyped]
    _HAVE_PSUTIL = True
except ImportError:  # pragma: no cover — psutil is a hard dep, but degrade if missing
    psutil = None  # type: ignore[assignment]
    _HAVE_PSUTIL = False
    logger.warning(
        "psutil not installed — RSS-aggregation monitor disabled; "
        "relying on RLIMIT_AS only.",
    )


def _allowed_cores() -> list[int]:
    """Return the cores this process is allowed to run on (Linux).
    Empty list if affinity isn't supported on this platform."""
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(0))
    return []


class Runner:
    """Resource-capped subprocess executor.

    Instance state is the semaphore + the limit values + a small core
    allocator; everything else flows through `execute`. Designed to be
    embeddable: instantiate once per server process, await `execute`
    from any task.
    """

    def __init__(
        self,
        max_concurrent: int,
        mem_limit_mb: int,
        cpu_limit_sec: int,
        rss_poll_interval: float = 2.0,
        rss_overshoot_ratio: float = 1.2,
        pin_cpu: bool = True,
        rewrite_overcommits: bool = True,
        sandbox: Optional[SandboxConfig] = None,
        coordinator: Optional["CrossProcessCoordinator"] = None,
        ceiling: Optional["Path"] = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if mem_limit_mb < 1:
            raise ValueError("mem_limit_mb must be >= 1")
        if cpu_limit_sec < 1:
            raise ValueError("cpu_limit_sec must be >= 1")
        self._max_concurrent = max_concurrent
        self._mem_bytes = mem_limit_mb * 1024 * 1024
        self._cpu_sec = cpu_limit_sec
        self._rss_poll = rss_poll_interval
        self._rss_overshoot = rss_overshoot_ratio
        self._pin_cpu = pin_cpu and hasattr(os, "sched_setaffinity")
        self._rewrite = rewrite_overcommits
        self._sandbox = sandbox if (sandbox and sandbox.enabled) else None
        self._coordinator = coordinator
        self._ceiling: Optional[Path] = ceiling

        # Concurrency: when a coordinator is injected, defer entirely to
        # it (cross-process semaphore). Else fall back to per-process
        # asyncio.Semaphore (0.1 behavior).
        if coordinator is None:
            self._sem: Optional[asyncio.Semaphore] = asyncio.Semaphore(max_concurrent)
        else:
            self._sem = None

        # Core allocator: when a coordinator is injected, claim cores
        # from it (non-overlapping across sibling Hares processes).
        # Else fall back to first-N cores (0.1 behavior, overlapping
        # if multiple Hares processes run side-by-side).
        if coordinator is not None:
            self._core_pool = coordinator.claim_cores()
        else:
            cores = _allowed_cores()
            self._core_pool = cores[:max_concurrent] if cores else []
        # Async lock around the per-Runner free/claimed bookkeeping.
        # (Note: this is INTRA-process; the coordinator's claim_cores
        # already partitions cores INTER-process so this Runner only
        # ever schedules within its own slice.)
        self._core_lock = asyncio.Lock()
        self._core_in_use: set[int] = set()

        # Active-scope state — used to derive the bwrap RW mount list
        # at execute() time. Set by ``set_active_scope`` when a caller
        # invokes ``restrict_paths``. None = use self._sandbox as-is
        # (operator-set HARES_SANDBOX_RW only).
        self._active_scope_paths: Optional[list[Path]] = None
        self._active_scope_read_only: bool = False

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def update_ceiling(self, ceiling: Path) -> None:
        """Update the ceiling used for bwrap mounts.

        Called when the ceiling is derived from MCP roots after session
        init rather than being known at server startup. Thread-safe for
        asyncio: the new ceiling takes effect on the next execute() call.
        """
        self._ceiling = ceiling

    def set_active_scope(
        self,
        paths: list[Path],
        *,
        read_only: bool = False,
    ) -> None:
        """Update the active scope used for bwrap mount construction.

        Called by the shell server when a caller invokes
        ``restrict_paths`` — narrows the bwrap RW mount list
        to ``paths`` (read-write) under the existing ceiling. With
        ``read_only=True``, paths are mounted RO instead of RW
        (subprocess kernel-rejected on writes).

        No-op when ``self._sandbox is None`` (sandbox disabled).
        Currently-running subprocesses are unaffected — each was
        spawned with its own bwrap argv at the time it started; only
        the NEXT execute() call will see the new scope.
        """
        self._active_scope_paths = list(paths) if paths else None
        self._active_scope_read_only = read_only

    def _effective_sandbox(self, cwd: Optional[str]) -> Optional[SandboxConfig]:
        """Compose the sandbox config for THIS execute() call.

        Mount priority (lowest to highest, each layer composes on top):
          1. HARES_SANDBOX_RO / HARES_SANDBOX_RW extras — always included.
          2. Ceiling — mounted RW by default so the project directory is
             accessible without an explicit cwd. Mounted RO when the
             instance is in --read-only mode (active scope read_only flag).
             Without this, a subprocess with no cwd sees only /tmp and
             system paths; project files are inaccessible.
          3. Active scope paths (from restrict_paths) — RW (or RO if
             read_only). When set, these override the ceiling mount for
             their subtrees, narrowing write authority.

        This matches the documented design: ceiling defines what's
        observable; active scope defines what's modifiable.
        """
        if self._sandbox is None:
            return None

        ceiling_str = str(self._ceiling) if self._ceiling else None

        if self._active_scope_paths is None:
            # No active scope — mount ceiling as RW (default) or RO
            # (read-only mode). Before this fix the ceiling wasn't mounted
            # at all, making project files inaccessible without a cwd.
            if not ceiling_str:
                return self._sandbox
            if self._active_scope_read_only:
                new_ro = tuple(self._sandbox.ro_binds) + (ceiling_str,)
                return replace(self._sandbox, ro_binds=new_ro)
            new_rw = tuple(self._sandbox.rw_binds) + (ceiling_str,)
            return replace(self._sandbox, rw_binds=new_rw)

        # Active scope set — scope paths are RW (or RO), ceiling is RO
        # for anything not already covered by an RW scope path.
        extra_paths = tuple(str(p) for p in self._active_scope_paths)
        if self._active_scope_read_only:
            new_ro = tuple(self._sandbox.ro_binds) + extra_paths
            return replace(self._sandbox, ro_binds=new_ro)
        # RW scope + ceiling as RO for the rest of the tree.
        new_rw = tuple(self._sandbox.rw_binds) + extra_paths
        new_ro = tuple(self._sandbox.ro_binds)
        if ceiling_str and ceiling_str not in new_rw:
            new_ro = new_ro + (ceiling_str,)
        return replace(self._sandbox, rw_binds=new_rw, ro_binds=new_ro)

    async def _claim_cores(self, weight: int) -> list[int]:
        """Grab `weight` free cores from the pool. Returns [] if the
        pool is empty (no affinity support / nothing to allocate)."""
        if not self._core_pool:
            return []
        async with self._core_lock:
            free = [c for c in self._core_pool if c not in self._core_in_use]
            # If there aren't enough free cores, fall back to time-sharing
            # the available ones — kernel will round-robin. We still pin
            # to a small subset so cpu_count() inside the child is small.
            if not free:
                free = self._core_pool
            chosen = free[:weight]
            for c in chosen:
                self._core_in_use.add(c)
            return chosen

    async def _release_cores(self, cores: list[int]) -> None:
        if not cores:
            return
        async with self._core_lock:
            for c in cores:
                self._core_in_use.discard(c)

    def _make_preexec(
        self,
        pinned_cores: list[int],
        *,
        mem_bytes: Optional[int] = None,
        cpu_sec: Optional[int] = None,
    ):
        """Build a fork-side initializer that applies all caps for one
        specific subprocess. Captured cores are baked into the closure
        so we don't need shared state inside the child.

        ``mem_bytes`` / ``cpu_sec`` override the Runner's defaults for
        this one call (used by the per-call resource-spec path). When
        None, falls back to the instance-level limits.
        """
        mem_bytes = mem_bytes if mem_bytes is not None else self._mem_bytes
        cpu_sec = cpu_sec if cpu_sec is not None else self._cpu_sec
        pin_cpu = self._pin_cpu
        cores = list(pinned_cores)

        def _preexec() -> None:
            if pin_cpu and cores:
                # Restrict the child (and any descendants) to these cores.
                # pytest-xdist's `-n auto` reads len(os.sched_getaffinity(0)),
                # so this naturally caps its worker count.
                os.sched_setaffinity(0, set(cores))
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec))
            os.setsid()

        return _preexec

    async def _monitor_rss(
        self,
        pid: int,
        kill_flag: dict[str, Any],
        *,
        mem_bytes: Optional[int] = None,
    ) -> None:
        """Belt-and-suspenders RSS monitor.

        RLIMIT_AS caps the virtual address space of a single process,
        but pytest-xdist (and any multiprocessing) creates additional
        children that each get their own AS budget. Aggregate RSS
        across the whole tree and SIGKILL it if the sum exceeds the
        per-process cap by `rss_overshoot_ratio` (default 1.2× as a
        small allowance for shared mappings).

        ``mem_bytes`` overrides the Runner's default for this call —
        the threshold is computed against the per-call cap so a
        right-sized small command isn't allowed to balloon up to the
        global default. Falls back to the instance-level limit when None.

        Sets kill_flag["reason"] = "rss_exceeded" so the caller can
        report it accurately.
        """
        if not _HAVE_PSUTIL:
            return
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        effective_mem = mem_bytes if mem_bytes is not None else self._mem_bytes
        threshold = int(effective_mem * self._rss_overshoot)
        while not kill_flag.get("done"):
            try:
                procs = [parent, *parent.children(recursive=True)]
                rss = sum(p.memory_info().rss for p in procs)
            except psutil.NoSuchProcess:
                return
            except psutil.Error:
                # Transient — try again next poll.
                await asyncio.sleep(self._rss_poll)
                continue
            if rss > threshold:
                kill_flag["reason"] = "rss_exceeded"
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                return
            await asyncio.sleep(self._rss_poll)

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        timeout: float = 300.0,
        weight: int = 1,
        mem_limit_mb: Optional[int] = None,
        cpu_limit_sec: Optional[int] = None,
        stdin: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run `command` in a shell, return stdout/stderr/exit_code/killed_reason.

        Args:
          command: Shell command (run via /bin/sh -c).
          cwd: Working directory for the child.
          env: Environment overrides (merged on top of os.environ).
          timeout: Wall-clock timeout in seconds. SIGKILL on overrun;
            killed_reason="timeout".
          weight: Number of semaphore slots to occupy (and the number
            of cores to pin to). Heavy commands (parallel pytest, builds)
            can pass weight=2 to reserve more capacity. Capped at
            max_concurrent.
          mem_limit_mb: Optional per-call RLIMIT_AS override (and RSS-
            overshoot threshold). Clamped to the Runner's instance
            default — callers can request LESS memory than the global
            default but never more (operator's HARES_MEM_LIMIT_MB is
            the hard ceiling). Useful for right-sizing known-small
            commands so failures surface earlier and the kill happens
            at the intended budget instead of the system default.
          cpu_limit_sec: Same idea for RLIMIT_CPU. Clamped to the
            instance default.
          stdin: Optional UTF-8 text to write to the child's stdin
            before it runs. Closed after the write so the child sees
            EOF and reaches its normal end-of-input branch. When None
            (default), stdin is inherited from the parent — same as
            the 0.4 behavior. Use this for commands that read input
            (``jq``, ``python -``, ``patch``, ``mail``) instead of
            wrapping them in ``/bin/sh -c 'echo ... | cmd'``.

        Returns:
          dict with keys: exit_code, stdout, stderr, killed_reason,
          rewrites (list of dicts describing any pre-flight edits).
          killed_reason is one of: None, "timeout", "rss_exceeded",
          "cpu_exceeded".
        """
        if not command:
            raise ValueError("command must be non-empty")
        weight = max(1, min(int(weight), self._max_concurrent))

        # Per-call resource overrides clamp DOWN to the operator's
        # defaults — callers can ask for less, never more. Lets an
        # agent right-size known-small commands without giving it the
        # ability to exceed the operator's policy.
        effective_mem_bytes: Optional[int] = None
        if mem_limit_mb is not None:
            requested = max(1, int(mem_limit_mb)) * 1024 * 1024
            effective_mem_bytes = min(requested, self._mem_bytes)
        effective_cpu_sec: Optional[int] = None
        if cpu_limit_sec is not None:
            requested_cpu = max(1, int(cpu_limit_sec))
            effective_cpu_sec = min(requested_cpu, self._cpu_sec)

        # Pre-flight: rewrite known overcommit patterns BEFORE spawning.
        rewrite_notice = ""
        rewrites_dump: list[dict[str, str]] = []
        if self._rewrite:
            inspection = inspect_command(command, weight, self._max_concurrent)
            if inspection.rewrites:
                command = inspection.command
                rewrite_notice = format_rewrite_notice(inspection.rewrites)
                rewrites_dump = [
                    {
                        "kind": r.kind,
                        "original": r.original,
                        "replacement": r.replacement,
                        "reason": r.reason,
                    }
                    for r in inspection.rewrites
                ]

        # Build the child's env. Start from os.environ so PATH etc.
        # carry through, then layer caller-provided overrides.
        child_env = {**os.environ, **(env or {})}

        # Acquire `weight` slots — from the coordinator if present
        # (cross-process global cap) or from the in-process semaphore
        # (0.1 fallback). We acquire one at a time so cancellation
        # while partially acquired releases what we got.
        acquired = 0
        pinned_cores: list[int] = []
        try:
            if self._coordinator is not None:
                await self._coordinator.acquire_subprocess_slot(weight)
                acquired = weight
            else:
                assert self._sem is not None
                for _ in range(weight):
                    await self._sem.acquire()
                    acquired += 1

            pinned_cores = await self._claim_cores(weight)

            # Route stdin via PIPE only when caller supplied input.
            # Leaving it None preserves 0.4 behavior (inherit from parent)
            # for callers that don't need stdin.
            stdin_kw = asyncio.subprocess.PIPE if stdin is not None else None
            stdin_bytes = stdin.encode("utf-8") if stdin is not None else None

            effective_sandbox = self._effective_sandbox(cwd)
            if effective_sandbox is not None:
                # Wrap the command in a bwrap invocation. bwrap handles
                # --chdir internally, so we don't pass cwd to the
                # subprocess (otherwise bwrap would itself try to
                # chdir there in the host namespace before mounting,
                # which is unnecessary and breaks if cwd is a sandbox-
                # only path).
                argv = build_bwrap_argv(effective_sandbox, command, cwd)
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=stdin_kw,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=None,
                    env=child_env,
                    preexec_fn=self._make_preexec(
                        pinned_cores,
                        mem_bytes=effective_mem_bytes,
                        cpu_sec=effective_cpu_sec,
                    ),
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdin=stdin_kw,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=child_env,
                    preexec_fn=self._make_preexec(
                        pinned_cores,
                        mem_bytes=effective_mem_bytes,
                        cpu_sec=effective_cpu_sec,
                    ),
                )

            kill_flag: dict[str, Any] = {}
            monitor = asyncio.create_task(self._monitor_rss(
                proc.pid, kill_flag, mem_bytes=effective_mem_bytes,
            ))
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes), timeout=timeout,
                )
                killed_reason = kill_flag.get("reason")
            except asyncio.TimeoutError:
                # Wall-clock exceeded — kill the whole tree.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                stdout, stderr = await proc.communicate()
                killed_reason = "timeout"
            finally:
                kill_flag["done"] = True
                await monitor

            # Distinguish CPU vs AS based on signal where we can.
            if killed_reason is None and proc.returncode is not None:
                if proc.returncode == -signal.SIGXCPU:
                    killed_reason = "cpu_exceeded"
                elif proc.returncode == -signal.SIGKILL:
                    # SIGKILL without a more specific reason is most
                    # commonly OOM here; flag generically.
                    killed_reason = "rss_exceeded"

            stdout_str = stdout.decode("utf-8", errors="replace")
            if rewrite_notice:
                stdout_str = rewrite_notice + stdout_str

            return {
                "exit_code": proc.returncode,
                "stdout": stdout_str,
                "stderr": stderr.decode("utf-8", errors="replace"),
                "killed_reason": killed_reason,
                "rewrites": rewrites_dump,
            }
        finally:
            await self._release_cores(pinned_cores)
            if self._coordinator is not None:
                if acquired:
                    await self._coordinator.release_subprocess_slot(acquired)
            else:
                assert self._sem is not None
                for _ in range(acquired):
                    self._sem.release()
