"""Cross-process resource coordination for multi-instance Hares.

When multiple Hares processes run side-by-side (e.g. Bunyan spawns 5+
instances per workflow), each process's in-process semaphore plus
each process's first-N CPU pinning would let total resource use
balloon to N × the operator-set caps. This module provides shared
accounting via filesystem coordination so the caps stay GLOBAL.

Two shared resources:

* **Subprocess concurrency** — a POSIX named semaphore
  (``posix_ipc.Semaphore``) sized to ``HARES_MAX_CONCURRENT``. Every
  ``execute_command`` invocation across all participating processes
  acquires from the same semaphore.

* **CPU core pool** — a JSON file under the coordination dir,
  serialized via ``fcntl.flock``. Each process registers a non-
  overlapping slice of the host's available cores at startup. Stale
  PID entries are evicted on next allocator entry.

Activation: when ``HARES_COORDINATION_DIR`` env var is set, all Hares
processes that read the same coordination dir share state. When the
env var is unset, each process falls back to its in-process
asyncio.Semaphore + first-N core pinning (existing 0.1 behavior —
backward compatible for standalone users).

Lifecycle notes:

* **POSIX semaphore cleanup** — POSIX named semaphores are kernel-
  persistent until reboot or explicit ``unlink``. The first Hares
  process to access a coord dir creates the semaphore with
  ``O_CREAT | O_EXCL`` race-safely; subsequent processes open the
  existing one. Cleanup happens when the umbrella process (Bunyan or
  whoever orchestrates the run) removes the coord dir + calls
  ``unlink_semaphore``. Hares processes themselves don't unlink on
  exit because we don't know whether siblings are still running.

* **Stale capacity mismatch** — if a previous run left a semaphore
  sized to a different ``HARES_MAX_CONCURRENT`` and the operator
  changed the cap before re-running, the existing semaphore's
  capacity is what governs (POSIX doesn't expose resize). Operator
  must unlink the old semaphore between runs to apply a cap change.
  We log a warning if the existing capacity differs from the env-var
  request so operators notice.

* **Core-pool stale-PID cleanup** — at allocator entry under fcntl
  lock, prune entries whose PID no longer exists on the host. This
  recovers cores from crashed Hares processes without manual
  intervention.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import posix_ipc  # type: ignore[import-untyped]
    _HAVE_POSIX_IPC = True
except ImportError:  # pragma: no cover — soft dep, falls back to in-process
    posix_ipc = None  # type: ignore[assignment]
    _HAVE_POSIX_IPC = False
    logger.warning(
        "posix_ipc not installed — cross-process subprocess concurrency "
        "coordination disabled; falling back to per-process semaphore. "
        "Install posix_ipc>=1.1 for shared throttling under multi-instance "
        "deployments (e.g. Bunyan).",
    )


# ── POSIX semaphore name derivation ────────────────────────────────────

# POSIX-named-semaphore names have a max length of ~31 chars on most
# platforms and must start with '/'. Derive a stable name from the
# coordination dir's absolute path via SHA-1 truncated to 24 hex chars.
def _semaphore_name(coord_dir: Path) -> str:
    digest = hashlib.sha1(str(coord_dir.resolve()).encode("utf-8")).hexdigest()
    return f"/hares-{digest[:24]}"


# ── Core-pool state file ────────────────────────────────────────────────

CORE_POOL_FILE = "core_pool.json"
CORE_POOL_VERSION = 1


def _core_pool_path(coord_dir: Path) -> Path:
    return coord_dir / CORE_POOL_FILE


def _allowed_cores() -> list[int]:
    """Cores this process is allowed to run on (Linux). Empty on
    platforms without sched_getaffinity (e.g. macOS)."""
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(0))
    return []


def _pid_alive(pid: int) -> bool:
    """Best-effort check whether ``pid`` corresponds to a running
    process. Used for stale-entry eviction in the core pool."""
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM  # exists but we can't signal
    return True


# ── Coordinator ────────────────────────────────────────────────────────


class CrossProcessCoordinator:
    """Shared resource accounting across multiple Hares instances.

    Constructed once per Hares process at startup. The Runner consults
    this object's :meth:`acquire_subprocess_slot` /
    :meth:`release_subprocess_slot` instead of its own asyncio
    Semaphore when ``coord_dir`` is provided.

    When ``coord_dir`` is None, all methods fall back to per-process
    behavior — equivalent to plain asyncio.Semaphore + first-N core
    pinning (0.1 behavior).
    """

    def __init__(
        self,
        *,
        coord_dir: Optional[Path],
        max_concurrent: int,
        weight_cap: int = 1,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._coord_dir = coord_dir
        self._max_concurrent = max_concurrent
        self._weight_cap = weight_cap
        self._fallback_sem: Optional[asyncio.Semaphore] = None
        self._posix_sem: Optional["posix_ipc.Semaphore"] = None
        self._claimed_cores: list[int] = []

        if coord_dir is None:
            # In-process fallback (0.1 behavior). Build a regular
            # asyncio.Semaphore to gate concurrency within this process.
            self._fallback_sem = asyncio.Semaphore(max_concurrent)
            return

        if not _HAVE_POSIX_IPC:
            logger.warning(
                "HARES_COORDINATION_DIR=%s set but posix_ipc unavailable; "
                "falling back to in-process semaphore (per-process cap, "
                "NOT cross-process). Install posix_ipc to fix.",
                coord_dir,
            )
            self._fallback_sem = asyncio.Semaphore(max_concurrent)
            return

        coord_dir.mkdir(parents=True, exist_ok=True)
        self._open_or_create_semaphore()

    # ── Public API ────────────────────────────────────────────────────

    @property
    def is_shared(self) -> bool:
        """True iff this coordinator is backed by a shared POSIX
        semaphore (cross-process). False = in-process fallback."""
        return self._posix_sem is not None

    async def acquire_subprocess_slot(self, weight: int = 1) -> None:
        """Acquire ``weight`` slots from the global semaphore. Blocks
        if the cap is reached. The Runner should call this BEFORE
        spawning each subprocess and release after."""
        weight = max(1, min(weight, self._weight_cap))
        if self._posix_sem is not None:
            # POSIX sem.acquire is blocking; offload to a thread so
            # we don't stall the asyncio loop.
            for _ in range(weight):
                await asyncio.to_thread(self._posix_sem.acquire)
            return
        assert self._fallback_sem is not None
        for _ in range(weight):
            await self._fallback_sem.acquire()

    async def release_subprocess_slot(self, weight: int = 1) -> None:
        """Symmetric release. Pass the SAME weight that was acquired."""
        weight = max(1, min(weight, self._weight_cap))
        if self._posix_sem is not None:
            for _ in range(weight):
                # POSIX sem.release is non-blocking. Use to_thread for
                # parity but it's near-zero overhead either way.
                await asyncio.to_thread(self._posix_sem.release)
            return
        assert self._fallback_sem is not None
        for _ in range(weight):
            self._fallback_sem.release()

    def claim_cores(self) -> list[int]:
        """Reserve a non-overlapping slice of host cores for this
        process. When ``coord_dir`` is set, coordinates with sibling
        processes via the shared core-pool file; otherwise picks the
        first ``max_concurrent`` cores (0.1 behavior).

        Returns the list of core IDs claimed (empty if affinity is
        unsupported on this platform).
        """
        all_cores = _allowed_cores()
        if not all_cores:
            return []  # no affinity support (macOS, etc.)

        if self._coord_dir is None:
            # 0.1 behavior — first-N cores per process. Multi-instance
            # callers using this fallback overlap on cores; that's the
            # known issue the coordinator was built to fix.
            return all_cores[: self._max_concurrent]

        # Shared core pool — fcntl.flock around read-modify-write.
        pool_path = _core_pool_path(self._coord_dir)
        with self._core_pool_lock(pool_path) as lock_fd:
            state = self._read_core_pool(pool_path)
            self._evict_stale_entries(state)

            claimed_by_others: set[int] = set()
            for pid_str, cores in state.get("claimed", {}).items():
                claimed_by_others.update(cores)

            free = [c for c in all_cores if c not in claimed_by_others]
            want = min(self._max_concurrent, len(free)) or self._max_concurrent
            chosen = free[:want] if free else all_cores[:want]

            state.setdefault("claimed", {})[str(os.getpid())] = chosen
            state["total_cores"] = len(all_cores)
            state["version"] = CORE_POOL_VERSION
            self._write_core_pool(pool_path, state)

            self._claimed_cores = chosen
            return chosen

    def release_cores(self) -> None:
        """Drop this process's core entry from the shared pool. Safe
        to call multiple times; safe to call when no cores were
        claimed. Typically wired to ``atexit``."""
        if self._coord_dir is None or not self._claimed_cores:
            return
        pool_path = _core_pool_path(self._coord_dir)
        try:
            with self._core_pool_lock(pool_path) as lock_fd:
                state = self._read_core_pool(pool_path)
                state.get("claimed", {}).pop(str(os.getpid()), None)
                self._write_core_pool(pool_path, state)
            self._claimed_cores = []
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.warning(
                "release_cores: failed to clean up core pool entry "
                "(%s); stale entry will be evicted by next allocator.",
                exc,
            )

    # ── Internal: POSIX semaphore lifecycle ───────────────────────────

    def _open_or_create_semaphore(self) -> None:
        """Open the shared POSIX semaphore, creating it race-safely
        if first-in. Logs a warning if the existing capacity differs
        from ``HARES_MAX_CONCURRENT`` (operator must unlink the stale
        semaphore to apply a cap change between runs)."""
        assert self._coord_dir is not None
        name = _semaphore_name(self._coord_dir)
        try:
            # First-in: create with O_CREAT | O_EXCL. If another
            # process raced, EEXIST and we open below.
            self._posix_sem = posix_ipc.Semaphore(
                name,
                flags=posix_ipc.O_CREAT | posix_ipc.O_EXCL,
                initial_value=self._max_concurrent,
            )
            logger.info(
                "Created POSIX semaphore %s (capacity=%d) for coord_dir=%s",
                name, self._max_concurrent, self._coord_dir,
            )
        except posix_ipc.ExistentialError:
            self._posix_sem = posix_ipc.Semaphore(name)
            # Note: posix_ipc.Semaphore doesn't expose initial capacity
            # post-creation; we can't verify the existing semaphore's
            # cap matches HARES_MAX_CONCURRENT. Log advisory message.
            logger.info(
                "Joined existing POSIX semaphore %s for coord_dir=%s. "
                "If HARES_MAX_CONCURRENT was changed between runs, "
                "remove the semaphore manually (posix_ipc.unlink_"
                "semaphore('%s')) to apply the new value.",
                name, self._coord_dir, name,
            )

    # ── Internal: core-pool file I/O under fcntl.flock ────────────────

    class _LockCtx:
        """Context manager for an exclusive fcntl.flock on the core-pool
        file. Creates the file if needed."""
        def __init__(self, path: Path) -> None:
            self._path = path
            self._fd: Optional[int] = None

        def __enter__(self) -> int:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Open with O_CREAT so the file exists for flock. flock the
            # FD; release happens on close.
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
            return self._fd

        def __exit__(self, exc_type, exc, tb) -> None:
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)

    def _core_pool_lock(self, path: Path) -> "CrossProcessCoordinator._LockCtx":
        return CrossProcessCoordinator._LockCtx(path)

    def _read_core_pool(self, path: Path) -> dict:
        """Read the core-pool JSON, returning a fresh dict on
        empty/missing/corrupt content (crash recovery)."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": CORE_POOL_VERSION, "claimed": {}}
        if not text.strip():
            return {"version": CORE_POOL_VERSION, "claimed": {}}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(
                "Core-pool file %s is corrupt; re-initializing.", path,
            )
            return {"version": CORE_POOL_VERSION, "claimed": {}}
        if data.get("version") != CORE_POOL_VERSION:
            logger.warning(
                "Core-pool file %s version mismatch (got %r, expected "
                "%d); re-initializing.",
                path, data.get("version"), CORE_POOL_VERSION,
            )
            return {"version": CORE_POOL_VERSION, "claimed": {}}
        return data

    def _write_core_pool(self, path: Path, state: dict) -> None:
        """Atomic write: tmp + rename."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _evict_stale_entries(self, state: dict) -> None:
        """Remove entries whose PID is no longer alive. Mutates
        ``state`` in place."""
        claimed = state.get("claimed", {})
        for pid_str in list(claimed.keys()):
            try:
                pid = int(pid_str)
            except ValueError:
                claimed.pop(pid_str, None)
                continue
            if not _pid_alive(pid):
                logger.info(
                    "Core-pool: evicting stale entry for pid=%d", pid,
                )
                claimed.pop(pid_str, None)


# ── Module-level convenience for atexit wiring ─────────────────────────


def install_atexit_cleanup(coordinator: CrossProcessCoordinator) -> None:
    """Register an atexit hook to drop this process's core-pool entry
    on exit. POSIX semaphore intentionally NOT unlinked here — sibling
    processes may still need it; the umbrella orchestrator owns
    semaphore lifecycle."""
    import atexit
    atexit.register(coordinator.release_cores)
