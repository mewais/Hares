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
from typing import Any, Optional

from .inspector import format_rewrite_notice, inspect_command

logger = logging.getLogger(__name__)

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
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if mem_limit_mb < 1:
            raise ValueError("mem_limit_mb must be >= 1")
        if cpu_limit_sec < 1:
            raise ValueError("cpu_limit_sec must be >= 1")
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._mem_bytes = mem_limit_mb * 1024 * 1024
        self._cpu_sec = cpu_limit_sec
        self._rss_poll = rss_poll_interval
        self._rss_overshoot = rss_overshoot_ratio
        self._pin_cpu = pin_cpu and hasattr(os, "sched_setaffinity")
        self._rewrite = rewrite_overcommits

        # Core allocator: pool of currently-free cores from the parent's
        # affinity set, capped to max_concurrent so we never claim more
        # cores than the host promised us.
        cores = _allowed_cores()
        if cores:
            self._core_pool = cores[:max_concurrent]
        else:
            self._core_pool = []
        # Async lock around the core pool's free/claimed bookkeeping.
        self._core_lock = asyncio.Lock()
        self._core_in_use: set[int] = set()

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

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

    def _make_preexec(self, pinned_cores: list[int]):
        """Build a fork-side initializer that applies all caps for one
        specific subprocess. Captured cores are baked into the closure
        so we don't need shared state inside the child."""
        mem_bytes = self._mem_bytes
        cpu_sec = self._cpu_sec
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

    async def _monitor_rss(self, pid: int, kill_flag: dict[str, Any]) -> None:
        """Belt-and-suspenders RSS monitor.

        RLIMIT_AS caps the virtual address space of a single process,
        but pytest-xdist (and any multiprocessing) creates additional
        children that each get their own AS budget. Aggregate RSS
        across the whole tree and SIGKILL it if the sum exceeds the
        per-process cap by `rss_overshoot_ratio` (default 1.2× as a
        small allowance for shared mappings).

        Sets kill_flag["reason"] = "rss_exceeded" so the caller can
        report it accurately.
        """
        if not _HAVE_PSUTIL:
            return
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        threshold = int(self._mem_bytes * self._rss_overshoot)
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

        Returns:
          dict with keys: exit_code, stdout, stderr, killed_reason,
          rewrites (list of dicts describing any pre-flight edits).
          killed_reason is one of: None, "timeout", "rss_exceeded",
          "cpu_exceeded".
        """
        if not command:
            raise ValueError("command must be non-empty")
        weight = max(1, min(int(weight), self._max_concurrent))

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

        # Acquire `weight` slots from the semaphore. We acquire one at
        # a time so cancellation while partially acquired releases what
        # we got.
        acquired = 0
        pinned_cores: list[int] = []
        try:
            for _ in range(weight):
                await self._sem.acquire()
                acquired += 1

            pinned_cores = await self._claim_cores(weight)

            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=child_env,
                preexec_fn=self._make_preexec(pinned_cores),
            )

            kill_flag: dict[str, Any] = {}
            monitor = asyncio.create_task(self._monitor_rss(proc.pid, kill_flag))
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
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
            for _ in range(acquired):
                self._sem.release()
