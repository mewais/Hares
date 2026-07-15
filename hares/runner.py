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
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .net_policy import NetworkPolicy
import logging
import os
import resource
import signal
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, TypedDict

from . import memlimit
from .grants import GrantStore
from .inspector import format_rewrite_notice, inspect_command
from .sandbox import SandboxConfig, build_bwrap_argv

logger = logging.getLogger(__name__)

#: Seconds to wait for a killed process tree to drain its stdout/stderr
#: after SIGKILL. Processes wedged in uninterruptible D-state (e.g. a
#: stalled hard-mounted NFS path) ignore SIGKILL, so an unbounded drain
#: would hold the concurrency slot forever and block every session. When
#: this elapses we abandon the drain, release the slot, and leave the
#: (unkillable-until-I/O-returns) process orphaned on the host.
_POST_KILL_DRAIN_TIMEOUT = 30.0


class _ExecuteResultBase(TypedDict):
    """Keys present in EVERY :meth:`Runner.execute` result."""

    #: Child's exit status. Negative = killed by that signal number;
    #: -1 for the servers' policy-rejection payloads.
    exit_code: Optional[int]
    stdout: str
    stderr: str
    #: None on a clean run, else one of "timeout", "rss_exceeded",
    #: "cpu_exceeded", "oom".
    killed_reason: Optional[str]
    #: Pre-flight overcommit rewrites applied to the command (empty
    #: list when none fired). Each entry has kind/original/replacement/
    #: reason keys.
    rewrites: list[dict[str, str]]
    #: Per-process RLIMIT_AS actually applied, in MB (after clamping).
    applied_mem_limit_mb: int
    #: RLIMIT_CPU actually applied, in seconds (after clamping).
    applied_cpu_limit_sec: int
    #: "full", "off", or "allowlist" — the network isolation in effect.
    network_mode: str
    #: "cgroup" when the command tree was wrapped in a cgroup v2 scope,
    #: else "rlimit".
    memory_mode: str
    #: Aggregate (whole-tree) memory cap in MB — the cgroup memory.max
    #: when memory_mode == "cgroup", and the RLIMIT_AS fallback otherwise.
    aggregate_mem_limit_mb: int


class ExecuteResult(_ExecuteResultBase, total=False):
    """Result shape of :meth:`Runner.execute`.

    The base keys (see :class:`_ExecuteResultBase`) are always present.
    The keys below are conditional:

    * ``killed_note`` — present iff ``killed_reason`` is not None; a
      human-readable explanation of the kill plus remediation advice.
      (The MCP servers may append a retry hint pointing at
      ``execute_command_high_memory`` when ``killed_reason == "oom"``.)
    * ``peak_rss_mb`` — present when the RSS monitor observed the
      process tree's peak resident set (best-effort; requires psutil
      and at least one successful poll).
    * ``preexec_note`` — present when the child failed to start because
      ``preexec_fn`` raised (typically: requested RLIMIT exceeds the
      inherited hard limit).
    * ``resource_note`` — present when a caller-supplied per-call
      ``mem_limit_mb`` / ``cpu_limit_sec`` was clamped down to the
      operator ceiling; describes the clamp(s).
    """

    killed_note: str
    peak_rss_mb: int
    preexec_note: str
    resource_note: str

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


def _try_resolve_scope_dir(scope_name: str, pid: int) -> Optional[Path]:
    """Single attempt to resolve the cgroup directory for a scope.

    Tries three strategies in order:
    1. Parse ``/proc/<pid>/cgroup`` of the direct child.
    2. Search psutil descendants (the real command is a grandchild of
       systemd-run and lands in the scope before the top process).
    3. Construct the canonical sysfs path from uid + scope_name.

    Returns the Path if found, None otherwise.  Non-raising.
    """
    # Strategy 1: direct pid.
    scope_dir = memlimit.scope_cgroup_dir(scope_name, member_pid=pid)
    if scope_dir is not None:
        return scope_dir

    # Strategy 2: search descendants.
    if _HAVE_PSUTIL:
        try:
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                scope_dir = memlimit.scope_cgroup_dir(
                    scope_name, member_pid=child.pid,
                )
                if scope_dir is not None:
                    return scope_dir
        except Exception:
            pass

    # Strategy 3: constructed path (no pid needed — checks sysfs directly).
    return memlimit.scope_cgroup_dir(scope_name)


async def _resolve_scope_dir(scope_name: str, pid: int) -> Optional[Path]:
    """Resolve the cgroup directory for a newly-spawned scope, with retries.

    ``pid`` is the direct child PID (systemd-run for cgroup-wrapped calls,
    or bwrap/sh for unwrapped calls). When the direct pid's /proc/cgroup
    entry does not yet contain the scope name (systemd-run may still be
    setting up the scope), we fall back to searching descendants via psutil
    — the real command is a grandchild of systemd-run and lands in the scope
    first.

    Retries for up to ~0.3 s (6 × 50 ms asyncio sleeps) to accommodate the
    small race window between systemd-run spawning and the cgroup directory
    appearing in sysfs.  Using asyncio.sleep keeps the event loop free during
    each wait.

    Returns None if the directory cannot be found (non-fatal; falls back
    to RLIMIT-only bounding and disables OOM polling for this call).
    """
    for attempt in range(6):
        result = _try_resolve_scope_dir(scope_name, pid)
        if result is not None:
            return result
        if attempt < 5:
            await asyncio.sleep(0.05)
    return None


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
        network_policy: Optional["NetworkPolicy"] = None,
        mem_limit_max_mb: Optional[int] = None,
        use_cgroup: Optional[bool] = None,
        grant_store: Optional[GrantStore] = None,
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

        # Warn once at startup if the configured limits exceed what the
        # kernel will actually allow (inherited hard limits from a parent
        # Hares process). Every execute_command will silently clamp to
        # the hard limit; this gives operators/users early visibility.
        try:
            _, as_hard = resource.getrlimit(resource.RLIMIT_AS)
            if as_hard >= 0 and self._mem_bytes > as_hard:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "HARES_MEM_LIMIT_MB=%dMB exceeds the inherited RLIMIT_AS "
                    "hard limit (%dMB). Commands will be capped at %dMB. "
                    "Run outside a resource-constrained environment or lower "
                    "HARES_MEM_LIMIT_MB to suppress this warning.",
                    mem_limit_mb, as_hard // 1024 // 1024,
                    as_hard // 1024 // 1024,
                )
                self._mem_bytes = as_hard  # pre-clamp so rewrites are accurate
            _, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
            if cpu_hard >= 0 and self._cpu_sec > cpu_hard:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "HARES_CPU_LIMIT_SEC=%ds exceeds the inherited RLIMIT_CPU "
                    "hard limit (%ds). Commands will be capped at %ds.",
                    cpu_limit_sec, cpu_hard, cpu_hard,
                )
                self._cpu_sec = cpu_hard  # pre-clamp
        except Exception:
            pass  # rlimit unavailable on this platform; ignore

        # mem_limit_max_mb is the CEILING for high_memory=True calls.
        # It defaults to mem_limit_mb when not supplied so existing callers
        # without this param continue to work without any behavior change.
        # Clamped to the inherited RLIMIT_AS hard limit the same way
        # _mem_bytes is, so a high_memory call can never exceed the kernel's
        # hard limit either.
        raw_max_bytes = (mem_limit_max_mb or mem_limit_mb) * 1024 * 1024
        try:
            _, as_hard = resource.getrlimit(resource.RLIMIT_AS)
            if as_hard >= 0:
                raw_max_bytes = min(raw_max_bytes, as_hard)
        except Exception:
            pass
        # Ensure max is at least as large as the normal cap so that the
        # ceiling is never BELOW the per-process default.
        self._mem_max_bytes: int = max(raw_max_bytes, self._mem_bytes)

        # Whether to use cgroup v2 scopes (systemd-run --user --scope) for
        # aggregate memory bounding. When use_cgroup is None, we auto-detect
        # by calling memlimit.cgroup_memory_available(). Passing True/False
        # explicitly lets tests force the mode on or off without the overhead
        # of the functional probe.
        if use_cgroup is not None:
            self._cgroup_ok: bool = use_cgroup
        else:
            self._cgroup_ok = memlimit.cgroup_memory_available()
        self._rss_poll = rss_poll_interval
        self._rss_overshoot = rss_overshoot_ratio
        self._pin_cpu = pin_cpu and hasattr(os, "sched_setaffinity")
        self._rewrite = rewrite_overcommits
        self._sandbox = sandbox if (sandbox and sandbox.enabled) else None
        self._coordinator = coordinator
        self._ceiling: Optional[Path] = ceiling
        self._network_policy = network_policy
        # Runtime request_path_access grants (see hares.grants). None
        # when the caller never wired one up — _effective_sandbox and
        # execute() both no-op gracefully in that case. In combined
        # mode, this is the SAME GrantStore instance shared with the
        # fs operation handlers (see hares.combined.server).
        self._grant_store = grant_store

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
          4. Active request_path_access grants (see GrantStore) — RW or
             RO extra binds OUTSIDE the ceiling, added last among the
             "allow" tiers (still BEFORE the deny tier below, which
             ``build_bwrap_argv`` always applies last regardless).

        This matches the documented design: ceiling defines what's
        observable; active scope defines what's modifiable; grants
        widen past the ceiling on a per-path, human-approved basis.

        Pure / side-effect-free — does NOT consume "once" grants (this
        method is called more than once per execute() call). See
        ``execute()`` for the single consumption point.
        """
        if self._sandbox is None:
            return None

        ceiling_str = str(self._ceiling) if self._ceiling else None

        # Resolve the in-ceiling blacklist entries (HARES_SANDBOX_PROTECT /
        # HARES_SANDBOX_EXCLUDE) against the ceiling now — load_sandbox_config
        # stored them raw because it doesn't know the ceiling. build_bwrap_argv
        # applies them LAST regardless of which branch below runs, so deny
        # always beats the rw/active-scope/grant binds.
        base = self._resolve_blacklist(self._sandbox)

        if self._active_scope_paths is None:
            # No active scope — mount ceiling as RW (default) or RO
            # (read-only mode). Before this fix the ceiling wasn't mounted
            # at all, making project files inaccessible without a cwd.
            if not ceiling_str:
                result = base
            elif self._active_scope_read_only:
                new_ro = tuple(base.ro_binds) + (ceiling_str,)
                result = replace(base, ro_binds=new_ro)
            else:
                new_rw = tuple(base.rw_binds) + (ceiling_str,)
                result = replace(base, rw_binds=new_rw)
        elif self._active_scope_read_only:
            # Active scope set — scope paths are RW (or RO), ceiling is RO
            # for anything not already covered by an RW scope path.
            extra_paths = tuple(str(p) for p in self._active_scope_paths)
            new_ro = tuple(base.ro_binds) + extra_paths
            result = replace(base, ro_binds=new_ro)
        else:
            # RW scope + ceiling as RO for the rest of the tree.
            extra_paths = tuple(str(p) for p in self._active_scope_paths)
            new_rw = tuple(base.rw_binds) + extra_paths
            new_ro = tuple(base.ro_binds)
            if ceiling_str and ceiling_str not in new_rw:
                new_ro = new_ro + (ceiling_str,)
            result = replace(base, rw_binds=new_rw, ro_binds=new_ro)

        return self._apply_grant_mounts(result)

    def _apply_grant_mounts(self, cfg: SandboxConfig) -> SandboxConfig:
        """Add active request_path_access grant roots (see
        hares.grants.GrantStore) as extra bind mounts.

        Appended to ``rw_binds`` / ``ro_binds`` per grant mode — i.e.
        BEFORE ``build_bwrap_argv`` applies ``protect_binds`` /
        ``exclude_binds`` (always LAST in that function), so a
        granted path that also happens to be excluded/protected is
        still denied: deny beats grant, kernel-enforced, no extra
        code needed here beyond "add the bind in the right tier."

        Pure / side-effect-free: does NOT consume "once" grants (this
        method may be called more than once per execute() — see the
        two call sites in ``execute()``). Consumption happens exactly
        once, in ``execute()``, right before the subprocess spawns —
        see the docstring on ``GrantStore.consume_all_once``.
        """
        if self._grant_store is None:
            return cfg
        grants = self._grant_store.list_active()
        if not grants:
            return cfg
        rw_extra = tuple(g["path"] for g in grants if g["mode"] == "rw")
        ro_extra = tuple(g["path"] for g in grants if g["mode"] == "ro")
        if not rw_extra and not ro_extra:
            return cfg
        return replace(
            cfg,
            rw_binds=tuple(cfg.rw_binds) + rw_extra,
            ro_binds=tuple(cfg.ro_binds) + ro_extra,
        )

    def _resolve_blacklist(self, sandbox: SandboxConfig) -> SandboxConfig:
        """Resolve HARES_SANDBOX_PROTECT / HARES_SANDBOX_EXCLUDE entries
        against the ceiling (relative entries become ceiling-relative,
        matching how fs ops resolve a relative tool-call path). Returns
        the sandbox unchanged when neither list is set."""
        if not sandbox.protect_binds and not sandbox.exclude_binds:
            return sandbox

        def _resolve(entries: tuple[str, ...]) -> tuple[str, ...]:
            out: list[str] = []
            for entry in entries:
                p = Path(entry)
                if not p.is_absolute() and self._ceiling is not None:
                    p = self._ceiling / p
                out.append(str(p.resolve(strict=False)))
            return tuple(out)

        return replace(
            sandbox,
            protect_binds=_resolve(sandbox.protect_binds),
            exclude_binds=_resolve(sandbox.exclude_binds),
        )

    async def _spawn_sandboxed(
        self,
        effective_sandbox: SandboxConfig,
        command: str,
        cwd: Optional[str],
        child_env: dict,
        stdin_kw: Optional[int],
        pinned_cores: list[int],
        effective_mem_bytes: Optional[int],
        effective_cpu_sec: Optional[int],
        effective_aggregate_bytes: int,
        scope_name: Optional[str] = None,
    ) -> tuple["asyncio.subprocess.Process", Optional[Any]]:
        """Spawn a sandboxed subprocess, coordinating slirp4netns when a
        network allowlist is configured, and optionally wrapping the argv
        in a systemd-run cgroup scope for aggregate memory bounding.

        Without an allowlist: builds the bwrap argv normally; if cgroup mode
        is active wraps the bwrap argv in a systemd-run scope and returns
        ``(proc, scope_dir)``; otherwise returns ``(proc, None)``.

        With an allowlist (slirp4netns path): composing the --sync-fd
        handshake with an outer systemd-run wrapper is fragile because
        systemd-run sits between us and bwrap — the write-end of the sync
        pipe would reference bwrap's PID relative to a cgroup we cannot
        easily resolve before bwrap execs. To avoid breaking this critical
        path we intentionally SKIP cgroup wrapping when a network allowlist
        is active and fall back to RLIMIT_AS for memory bounding instead.
        The result dict's ``memory_mode`` will reflect ``'rlimit'`` for this
        call. This does not affect the per-process RLIMIT_AS that applies
        through bwrap — it only means the *aggregate* cgroup bound is absent.

        Returns ``(proc, scope_dir)`` where ``scope_dir`` is the resolved
        cgroup directory Path (or None if cgroup mode is off or resolution
        failed).
        """
        policy = self._network_policy
        use_allowlist = (
            policy is not None
            and policy.enabled
            and effective_sandbox is not None
        )

        if not use_allowlist:
            # Build the inner bwrap argv.
            inner_argv = build_bwrap_argv(effective_sandbox, command, cwd)
            # Optionally wrap in a cgroup scope.
            if scope_name is not None:
                argv = memlimit.build_scope_argv(
                    inner_argv,
                    mem_bytes=effective_aggregate_bytes,
                    scope_name=scope_name,
                    swap_max_bytes=0,
                )
            else:
                argv = inner_argv
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
            # Resolve the cgroup directory for OOM monitoring/timeout.
            # Uses async retries to handle the race between systemd-run
            # spawning and the cgroup directory appearing in sysfs.
            scope_dir: Optional[Path] = None
            if scope_name is not None:
                scope_dir = await _resolve_scope_dir(scope_name, proc.pid)
            return proc, scope_dir

        # ── Allowlist mode (slirp4netns + nftables) ───────────────────────
        # Cgroup wrapping is intentionally skipped here: the --sync-fd
        # handshake requires us to wait for a byte written by bwrap after
        # it creates namespaces, then pass slirp4netns bwrap's PID. Wrapping
        # with systemd-run inserts an extra process layer that makes the PID
        # resolution and pipe coordination unreliable. We fall back to
        # RLIMIT_AS only; the result's memory_mode will be 'rlimit'.

        from .net_policy import (
            build_inner_setup_script,
            slirp4netns_available,
            SLIRP4NETNS_BIN,
            SLIRP_TAP,
        )

        if not slirp4netns_available():
            logger.warning(
                "HARES_SANDBOX_NETWORK_ALLOW is set but slirp4netns is not "
                "on PATH (%s). Falling back to NETWORK=off — the process "
                "will have NO external connectivity. Install slirp4netns to "
                "enable allowlist-filtered network access.",
                SLIRP4NETNS_BIN,
            )
            # Fall back: just unshare-net, no connectivity.
            argv = build_bwrap_argv(
                effective_sandbox, command, cwd,
                network_setup_script=None,
            )
            argv.insert(argv.index("--unshare-net") + 0, "--unshare-net")
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
            return proc, None

        # Build the inner setup script (nftables + exec real command).
        setup = build_inner_setup_script(policy, command)

        # Create the sync pipe.
        r_fd, w_fd = os.pipe()

        argv = build_bwrap_argv(
            effective_sandbox, command, cwd,
            sync_fd=r_fd,
            network_setup_script=setup,
        )

        # Start bwrap; it'll block at the sync-fd waiting for us.
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
            pass_fds=(r_fd,),
        )
        os.close(r_fd)  # child has it; close our copy

        # Wait for bwrap to signal namespace readiness (non-blocking via thread).
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: os.read(w_fd, 1)),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.error("bwrap --sync-fd: timed out waiting for namespace ready")
            proc.kill()
            os.close(w_fd)
            return proc, None

        # Start slirp4netns to give the isolated netns connectivity.
        try:
            slirp = await asyncio.create_subprocess_exec(
                SLIRP4NETNS_BIN, "--configure", str(proc.pid), SLIRP_TAP,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Give slirp4netns time to bring up the tap interface.
            await asyncio.sleep(0.3)
            logger.debug(
                "slirp4netns started for bwrap PID %d (slirp PID %d)",
                proc.pid, slirp.pid,
            )
        except Exception as exc:
            logger.error(
                "Failed to start slirp4netns: %s. Network allowlist will "
                "have no connectivity.", exc,
            )

        # Signal bwrap to exec the setup script + real command.
        try:
            os.write(w_fd, b"\x01")
        finally:
            os.close(w_fd)

        return proc, None

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
            # Clamp to the inherited hard limit. Without this, setting
            # RLIMIT_AS > the inherited hard limit throws ValueError in
            # the forked child, which Python surfaces as "Exception
            # occurred in preexec_fn." — a silent, cryptic failure.
            # This happens when Hares runs inside another Hares process
            # (e.g. the test suite) whose hard limit is lower than our
            # configured soft limit.
            _, as_hard = resource.getrlimit(resource.RLIMIT_AS)
            effective_mem = min(mem_bytes, as_hard) if as_hard >= 0 else mem_bytes
            resource.setrlimit(resource.RLIMIT_AS, (effective_mem, effective_mem))
            _, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
            effective_cpu = min(cpu_sec, cpu_hard) if cpu_hard >= 0 else cpu_sec
            resource.setrlimit(resource.RLIMIT_CPU, (effective_cpu, effective_cpu))
            os.setsid()

        return _preexec

    async def _monitor_rss(
        self,
        pid: int,
        kill_flag: dict[str, Any],
        *,
        mem_bytes: Optional[int] = None,
        scope_dir: Optional[Path] = None,
        scope_name: Optional[str] = None,
    ) -> None:
        """RSS / OOM monitor.

        **Cgroup mode** (``scope_name`` is not None): polls
        ``memlimit.read_oom_kill_count(scope_dir)`` every
        ``self._rss_poll`` seconds. When the counter rises above its
        initial value the cgroup OOM killer has already terminated the
        process tree — we simply RECORD it by setting
        ``kill_flag['reason'] = 'oom'`` without sending any signal (the
        tree is already dead). No psutil RSS aggregation runs in this mode.

        If ``scope_dir`` is None at entry but ``scope_name`` is provided,
        the monitor will attempt to lazily resolve the cgroup directory on
        each poll iteration until it is found or the command finishes.
        This handles the race where a fast command exits before
        ``_resolve_scope_dir`` completes: for such commands there is no
        OOM to detect, so the lazy path is a no-op. For slower commands
        that do trigger OOM, the cgroup dir will be resolved before the
        OOM event and polling will proceed normally.

        **RLIMIT mode** (``scope_name`` is None): the legacy psutil path.
        RLIMIT_AS caps the virtual address space of a single process, but
        pytest-xdist / multiprocessing creates additional children that
        each have their own AS budget. Aggregate RSS across the whole tree
        and SIGKILL it if the sum exceeds the per-process cap by
        ``rss_overshoot_ratio`` (default 1.2× as a small allowance for
        shared mappings). Sets ``kill_flag['reason'] = 'rss_exceeded'``.

        ``mem_bytes`` overrides the Runner's default threshold for RLIMIT
        mode — the threshold is computed against the per-call cap so a
        right-sized small command isn't allowed to balloon up to the global
        default. Ignored in cgroup mode (the kernel enforces the cap).
        """
        if scope_name is not None:
            # ── Cgroup OOM polling ──────────────────────────────────────
            # scope_dir may be None here for fast commands where the cgroup
            # directory was not yet visible in sysfs when the command exited.
            # We lazily attempt to resolve it on each poll until found.
            #
            # OOM counter reads are cheap (a single file read), so we poll
            # at a FIXED short interval (0.1 s) rather than self._rss_poll
            # which is sized for the more expensive psutil RSS aggregation.
            # This ensures we catch OOM events within ~100 ms rather than
            # within ~2 s, giving us time to read the counter before systemd
            # removes the scope directory (~100–200 ms after OOM fires).
            _CGROUP_POLL_INTERVAL = 0.1
            resolved_dir = scope_dir
            initial_count: Optional[int] = None
            if resolved_dir is not None:
                initial_count = memlimit.read_oom_kill_count(resolved_dir)
                if initial_count is None:
                    initial_count = 0
                # Publish the baseline so the post-communicate OOM check
                # can detect a rise even if the scope dir disappears before
                # communicate() returns (systemd removes it right after OOM).
                kill_flag["_oom_baseline"] = initial_count
            while not kill_flag.get("done"):
                await asyncio.sleep(_CGROUP_POLL_INTERVAL)
                # Lazy resolution: if we still don't have the dir, try now.
                if resolved_dir is None:
                    resolved_dir = _try_resolve_scope_dir(scope_name, pid)
                    if resolved_dir is not None:
                        # First time we have the dir — read the baseline.
                        initial_count = memlimit.read_oom_kill_count(resolved_dir)
                        if initial_count is None:
                            initial_count = 0
                        kill_flag["_oom_baseline"] = initial_count
                if resolved_dir is not None:
                    current = memlimit.read_oom_kill_count(resolved_dir)
                    if current is not None:
                        # Always track the last known oom_kill count. The
                        # scope dir may disappear right after OOM fires;
                        # saving the peak lets the post-communicate check
                        # detect the rise even when the dir is gone.
                        kill_flag["_oom_last_seen"] = current
                        if current > (initial_count or 0):
                            # The cgroup OOM killer fired — the process tree
                            # is already terminated. Just record the reason.
                            kill_flag["reason"] = "oom"
                            return
            # Store the resolved dir back into kill_flag so the post-communicate
            # OOM check can use it even if scope_dir was None at spawn time.
            # (The dir may be gone by the time communicate() returns; the
            # _oom_last_seen fallback covers that case.)
            if resolved_dir is not None:
                kill_flag["_resolved_scope_dir"] = resolved_dir
            return

        # ── RLIMIT / psutil RSS aggregation ────────────────────────────
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
            # Track peak for reporting in the result.
            if rss > kill_flag.get("peak_rss_mb", 0) * 1024 * 1024:
                kill_flag["peak_rss_mb"] = rss // 1024 // 1024
            if rss > threshold:
                kill_flag["reason"] = "rss_exceeded"
                kill_flag["peak_rss_mb"] = rss // 1024 // 1024
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
        high_memory: bool = False,
    ) -> ExecuteResult:
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
          mem_limit_mb: Optional per-call memory override. In normal mode
            (high_memory=False) this is clamped DOWN to the Runner's
            instance default so callers can ask for less, never more. In
            high_memory mode the ceiling is self._mem_max_bytes instead,
            permitting up to the machine-safe maximum; any value above that
            ceiling is still clamped. Useful for right-sizing known-small
            commands so failures surface earlier.
          cpu_limit_sec: Same idea for RLIMIT_CPU. Clamped to the
            instance default.
          stdin: Optional UTF-8 text to write to the child's stdin
            before it runs. Closed after the write so the child sees
            EOF and reaches its normal end-of-input branch. When None
            (default), stdin is inherited from the parent — same as
            the 0.4 behavior. Use this for commands that read input
            (``jq``, ``python -``, ``patch``, ``mail``) instead of
            wrapping them in ``/bin/sh -c 'echo ... | cmd'``.
          high_memory: When True, the mem_limit_mb ceiling is raised to
            self._mem_max_bytes (the machine-safe max) rather than the
            normal per-command cap. The caller should have obtained user
            approval before setting this flag. If mem_limit_mb is not
            provided and high_memory is True, the budget defaults to the
            full self._mem_max_bytes.

        Returns:
          An :class:`ExecuteResult` dict — see that class's docstring
          for the full key set and when each conditional key appears.
          killed_reason is one of: None, "timeout", "rss_exceeded",
          "cpu_exceeded", "oom".
        """
        if not command:
            raise ValueError("command must be non-empty")
        weight = max(1, min(int(weight), self._max_concurrent))

        # Per-call resource overrides. The CEILING depends on high_memory:
        #   - Normal: cap at self._mem_bytes (operator's default).
        #   - High-memory: cap at self._mem_max_bytes (machine-safe max).
        # If high_memory and no mem_limit_mb supplied, default to the
        # full high-memory budget.
        ceiling_bytes = self._mem_max_bytes if high_memory else self._mem_bytes
        effective_mem_bytes: Optional[int]
        if mem_limit_mb is not None:
            requested = max(1, int(mem_limit_mb)) * 1024 * 1024
            effective_mem_bytes = min(requested, ceiling_bytes)
        elif high_memory:
            # Default budget is the full machine-safe ceiling.
            effective_mem_bytes = ceiling_bytes
        else:
            effective_mem_bytes = None

        # effective_aggregate_bytes is BOTH the cgroup memory.max AND the
        # per-process RLIMIT_AS (defence in depth: the cgroup bounds the
        # aggregate tree; RLIMIT_AS kills a single runaway process early).
        effective_aggregate_bytes: int = (
            effective_mem_bytes if effective_mem_bytes is not None else self._mem_bytes
        )

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
        # (cross-process flock-based cap, crash-safe) or from the
        # in-process semaphore (0.1 fallback, single-process only).
        slot_token: list[int] = []
        acquired = 0
        pinned_cores: list[int] = []
        try:
            if self._coordinator is not None:
                slot_token = await self._coordinator.acquire_subprocess_slot(weight)
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

            # Determine if we should use cgroup wrapping for this call.
            # The allowlist (slirp4netns) path skips cgroup wrapping — see
            # the note in _spawn_sandboxed for the rationale.
            policy = self._network_policy
            use_allowlist = (
                policy is not None
                and policy.enabled
                and self._effective_sandbox(cwd) is not None
            )
            use_cgroup_this_call = self._cgroup_ok and not use_allowlist

            # Prepare the scope name if we are wrapping in a cgroup.
            scope_name: Optional[str] = (
                memlimit.new_scope_name() if use_cgroup_this_call else None
            )

            # Track the resolved cgroup directory (set after spawn).
            scope_dir: Optional[Path] = None

            effective_sandbox = self._effective_sandbox(cwd)
            if effective_sandbox is not None:
                # Single consumption point for "once" request_path_access
                # grants: bwrap mounts are recomputed fresh on every
                # execute() call (there's no persistent mount namespace to
                # incrementally update), so a "once" grant is defined to
                # apply to exactly this upcoming execute() call and is
                # then removed — regardless of whether the command
                # actually touches the granted path (bwrap mount
                # composition can't cheaply introspect that). This must
                # run exactly ONCE per execute() call: _effective_sandbox
                # itself stays side-effect-free (it's called twice above,
                # for the use_allowlist probe and for the real mount
                # list) so consuming inside it would double-consume.
                if self._grant_store is not None:
                    self._grant_store.consume_all_once()
                # Sandboxed path: bwrap (possibly with slirp4netns for
                # network allowlist). _spawn_sandboxed handles both cases
                # and now returns (proc, scope_dir).
                proc, scope_dir = await self._spawn_sandboxed(
                    effective_sandbox, command, cwd,
                    child_env, stdin_kw, pinned_cores,
                    effective_mem_bytes, effective_cpu_sec,
                    effective_aggregate_bytes, scope_name,
                )
            else:
                if scope_name is not None:
                    # No-sandbox path with cgroup wrapping.
                    # create_subprocess_shell does not accept an explicit argv
                    # so we switch to create_subprocess_exec with an explicit
                    # ['/bin/sh', '-c', command] form so we can prepend the
                    # systemd-run wrapper.
                    inner_argv = ["/bin/sh", "-c", command]
                    argv = memlimit.build_scope_argv(
                        inner_argv,
                        mem_bytes=effective_aggregate_bytes,
                        scope_name=scope_name,
                        swap_max_bytes=0,
                    )
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
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
                    scope_dir = await _resolve_scope_dir(scope_name, proc.pid)
                else:
                    # No-sandbox, no cgroup: original create_subprocess_shell path.
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

            # Track whether the command was wrapped in a cgroup scope.
            # This is TRUE whenever scope_name was set — the kernel enforced
            # memory.max for this command regardless of whether we managed to
            # resolve the cgroup directory for monitoring purposes.
            # scope_dir is SEPARATE: it is best-effort/Optional, used only for
            # OOM counter polling and cgroup_kill on timeout.
            wrapped_in_cgroup: bool = scope_name is not None
            memory_mode: str = "cgroup" if wrapped_in_cgroup else "rlimit"

            kill_flag: dict[str, Any] = {}
            monitor = asyncio.create_task(self._monitor_rss(
                proc.pid,
                kill_flag,
                mem_bytes=effective_mem_bytes,
                scope_dir=scope_dir,
                scope_name=scope_name,
            ))
            timed_out = False
            drain_stalled = False
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes), timeout=timeout,
                )
                # ── Final OOM check (normal exit path) ─────────────────
                # The cgroup OOM killer may have fired between the last
                # monitor poll and when communicate() returned.
                #
                # TIMING NOTE: systemd removes the scope cgroup directory
                # very quickly (~100 ms) after OOM fires. We do this check
                # HERE — while the process just exited and scope_dir is
                # most likely still present — rather than after
                # ``await monitor`` where the dir may already be gone.
                #
                # The monitor polls at 0.1 s intervals; if OOM fires just
                # before communicate() returns, the monitor may not have
                # run its poll yet (asyncio scheduling gives priority to
                # the communicator returning). Reading scope_dir directly
                # here is the most reliable window.
                if wrapped_in_cgroup and kill_flag.get("reason") is None:
                    # Use scope_dir (resolved at spawn time) as the
                    # primary read target; fall back to lazily-resolved
                    # dir stored in kill_flag by the monitor if available.
                    chk_dir = scope_dir or kill_flag.get("_resolved_scope_dir")
                    if chk_dir is not None:
                        final_oom = memlimit.read_oom_kill_count(chk_dir)
                        baseline = kill_flag.get("_oom_baseline", 0)
                        if final_oom is not None and final_oom > baseline:
                            kill_flag["reason"] = "oom"
                    # Fallback: if the monitor already read a count above
                    # baseline before the dir disappeared, trust that.
                    if kill_flag.get("reason") is None:
                        last_seen = kill_flag.get("_oom_last_seen")
                        baseline = kill_flag.get("_oom_baseline", 0)
                        if last_seen is not None and last_seen > baseline:
                            kill_flag["reason"] = "oom"
            except asyncio.TimeoutError:
                # Wall-clock exceeded — kill the whole tree.
                # In cgroup mode, also write to cgroup.kill for belt-and-
                # suspenders — cgroup.kill is instantaneous and covers
                # processes that ignore SIGKILL inside a pid namespace.
                if scope_dir is not None:
                    memlimit.cgroup_kill(scope_dir)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                # Drain output with a second timeout. Processes in
                # uninterruptible D-state (e.g. waiting on a stalled
                # hard-mounted NFS path) ignore SIGKILL; without a
                # timeout here the slot flock is held forever and all
                # subsequent commands from every session block indefinitely.
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=_POST_KILL_DRAIN_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # The tree ignored SIGKILL even after the drain window —
                    # almost certainly wedged in D-state on stalled I/O. Give
                    # up the drain so we release the slot; the process stays
                    # orphaned on the host until its I/O returns. Surface this
                    # loudly rather than reporting a plain empty-output timeout.
                    drain_stalled = True
                    stdout, stderr = b"", b""
                    logger.warning(
                        "Post-kill drain timed out after %.0fs (pid=%s); "
                        "process is likely wedged in D-state on stalled I/O "
                        "(e.g. a hard-mounted NFS path). Releasing the slot "
                        "and abandoning the orphaned process tree.",
                        _POST_KILL_DRAIN_TIMEOUT, proc.pid,
                    )
                timed_out = True
            finally:
                kill_flag["done"] = True
                await monitor  # drain the monitor task

            if timed_out:
                # After monitor exits, we can use the lazily-resolved dir
                # for cgroup_kill in case scope_dir was None at timeout.
                effective_scope_dir = scope_dir or kill_flag.get("_resolved_scope_dir")
                if effective_scope_dir is not None and scope_dir is None:
                    memlimit.cgroup_kill(effective_scope_dir)
                killed_reason = "timeout"
            else:
                killed_reason = kill_flag.get("reason")

            # Distinguish CPU vs AS/OOM based on signal where we can. The
            # counter-based OOM detection in _monitor_rss is the primary,
            # precise signal; the inference below is the fallback for when it
            # could not observe the transient scope's memory.events in time.
            if killed_reason is None and proc.returncode is not None:
                if proc.returncode == -signal.SIGXCPU:
                    killed_reason = "cpu_exceeded"
                elif wrapped_in_cgroup and proc.returncode in (
                    -signal.SIGKILL, -signal.SIGTERM,
                ):
                    # Cgroup-mode OOM fallback. The counter read depends on
                    # inspecting the transient scope's memory.events BEFORE
                    # systemd tears the cgroup down — which races on some
                    # hosts (and CI) — and does not fire at all when a
                    # userspace killer (systemd-oomd) reaps the scope with
                    # SIGTERM (exit 143) instead of the kernel cgroup OOM
                    # killer's SIGKILL (exit 137). By this point timeouts
                    # (killed_reason set above) and CPU limits (SIGXCPU) are
                    # already accounted for, so a memory-bounded tree that
                    # died by SIGKILL/SIGTERM with no other cause is an OOM.
                    killed_reason = "oom"
                elif proc.returncode == -signal.SIGKILL:
                    # Non-cgroup mode: SIGKILL without a more specific reason
                    # is most commonly OOM here; flag generically.
                    killed_reason = "rss_exceeded"

            stdout_str = stdout.decode("utf-8", errors="replace")
            if rewrite_notice:
                stdout_str = rewrite_notice + stdout_str
            stderr_str = stderr.decode("utf-8", errors="replace")

            # Compute the limits that were *actually* applied so the
            # caller (and any LLM reading the result) can see if their
            # request was silently clamped by the operator ceiling or
            # the inherited hard limit.
            applied_mem_mb = (effective_mem_bytes or self._mem_bytes) // 1024 // 1024
            applied_cpu_s  = effective_cpu_sec or self._cpu_sec
            aggregate_mem_limit_mb = effective_aggregate_bytes // 1024 // 1024

            # ── Diagnostic context fields ────────────────────────────────────

            # Network mode that was in effect for this command.
            if self._sandbox is None:
                network_mode_str = "full"
            elif self._network_policy is not None and self._network_policy.enabled:
                network_mode_str = "allowlist"
            elif not self._sandbox.allow_network:
                network_mode_str = "off"
            else:
                network_mode_str = "full"

            # Human-readable explanation for each kill reason.
            peak_rss_mb: Optional[int] = kill_flag.get("peak_rss_mb")
            killed_note: Optional[str] = None
            if killed_reason == "oom":
                killed_note = (
                    f"Command tree exceeded the {aggregate_mem_limit_mb}MB aggregate "
                    "memory cap and was killed under its cgroup (out of memory); your "
                    "session was unaffected. If this command legitimately needs more "
                    "memory, it can be re-run with a higher (machine-safe) budget that "
                    "requires user approval."
                )
            elif killed_reason == "rss_exceeded":
                peak_str = f" (peak RSS: {peak_rss_mb}MB)" if peak_rss_mb is not None else ""
                killed_note = (
                    f"Process tree killed: RSS exceeded the {applied_mem_mb}MB limit"
                    f"{peak_str}. Reduce memory usage or increase mem_limit_mb "
                    f"(ceiling: HARES_MEM_LIMIT_MB={applied_mem_mb}MB)."
                )
            elif killed_reason == "cpu_exceeded":
                killed_note = (
                    f"Process killed: CPU time exceeded the {applied_cpu_s}s limit "
                    f"(SIGXCPU). Reduce CPU usage or increase cpu_limit_sec "
                    f"(ceiling: HARES_CPU_LIMIT_SEC={applied_cpu_s}s)."
                )
            elif killed_reason == "timeout":
                killed_note = (
                    f"Process killed: wall-clock timeout of {timeout}s exceeded. "
                    "Increase the timeout parameter or split the command into "
                    "smaller steps."
                )
                if drain_stalled:
                    killed_note += (
                        f" NOTE: the process ignored SIGKILL and could not be "
                        f"drained within {_POST_KILL_DRAIN_TIMEOUT:.0f}s — it is "
                        "likely wedged in uninterruptible D-state on stalled I/O "
                        "(commonly a hard-mounted NFS path). Its stdout/stderr "
                        "were unavailable and it remains orphaned on the host "
                        "until the I/O returns. The concurrency slot was released."
                    )

            # Detect preexec_fn failures (e.g. requested RLIMIT exceeds the
            # inherited hard limit — common when Hares runs inside Hares).
            preexec_note: Optional[str] = None
            if "Exception occurred in preexec_fn" in stderr_str:
                preexec_note = (
                    f"The subprocess could not start: preexec_fn raised an exception. "
                    f"This usually means the requested RLIMIT (mem={applied_mem_mb}MB, "
                    f"cpu={applied_cpu_s}s) exceeds the inherited hard limit. "
                    "Lower mem_limit_mb / cpu_limit_sec or raise the ulimit before "
                    "starting Hares."
                )

            result: ExecuteResult = {
                "exit_code": proc.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "killed_reason": killed_reason,
                "rewrites": rewrites_dump,
                "applied_mem_limit_mb": applied_mem_mb,
                "applied_cpu_limit_sec": applied_cpu_s,
                "network_mode": network_mode_str,
                "memory_mode": memory_mode,
                "aggregate_mem_limit_mb": aggregate_mem_limit_mb,
            }
            if killed_note is not None:
                result["killed_note"] = killed_note
            if peak_rss_mb is not None:
                result["peak_rss_mb"] = peak_rss_mb
            if preexec_note is not None:
                result["preexec_note"] = preexec_note

            # Surface a friendly note when per-call overrides were clamped.
            if mem_limit_mb is not None:
                requested_mb = max(1, int(mem_limit_mb))
                if requested_mb > applied_mem_mb:
                    result["resource_note"] = (
                        f"mem_limit_mb clamped {requested_mb}MB → {applied_mem_mb}MB "
                        f"(operator ceiling / inherited hard limit). "
                        f"Lower your request or increase HARES_MEM_LIMIT_MB."
                    )
            if cpu_limit_sec is not None:
                requested_cpu = max(1, int(cpu_limit_sec))
                if requested_cpu > applied_cpu_s:
                    note = (
                        f"cpu_limit_sec clamped {requested_cpu}s → {applied_cpu_s}s "
                        f"(operator ceiling / inherited hard limit)."
                    )
                    result["resource_note"] = (
                        result.get("resource_note", "") + (" " if "resource_note" in result else "") + note
                    ).strip()
            return result
        finally:
            await self._release_cores(pinned_cores)
            if self._coordinator is not None:
                if slot_token or acquired:
                    await self._coordinator.release_subprocess_slot(
                        slot_token, acquired,
                    )
            else:
                assert self._sem is not None
                for _ in range(acquired):
                    self._sem.release()
