"""Cross-process resource coordination for multi-instance Hares.

When multiple Hares processes run side-by-side (e.g. an umbrella
orchestrator spawns 5+ instances per workflow), each process's
in-process semaphore plus each process's first-N CPU pinning would
let total resource use balloon to N × the operator-set caps. This
module provides shared accounting via filesystem coordination so
the caps stay GLOBAL.

Two shared resources:

* **Subprocess concurrency** — N per-slot lock files under
  ``HARES_COORDINATION_DIR/slots/slot-{i}.lock``, one per allowed
  concurrent subprocess. Each Hares process acquires ``weight`` of
  them with ``fcntl.flock(LOCK_EX | LOCK_NB)`` before spawning and
  releases (closes) them after. The kernel automatically releases any
  lock held by a process when that process dies — crash-safe by
  construction, no manual cleanup ever needed.

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

* **Slot file crash safety** — ``fcntl.flock(LOCK_EX)`` is released
  by the kernel when the file descriptor is closed or the owning
  process dies (any cause: clean exit, SIGKILL, OOM, crash). There
  is no persistent kernel state to clean up between runs. The slot
  files themselves are empty marker files and persist harmlessly.

* **Capacity changes** — if ``HARES_MAX_CONCURRENT`` changes between
  runs, new slot files are created (for a higher cap) or fewer slot
  files are used (for a lower cap). No stale state; no manual
  intervention required.

* **Core-pool stale-PID cleanup** — at allocator entry under fcntl
  lock, prune entries whose PID no longer exists on the host. This
  recovers cores from crashed Hares processes without manual
  intervention.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Slot file directory name ────────────────────────────────────────────

SLOTS_DIR = "slots"


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

    **Slot acquisition** (coord_dir set): acquires ``weight`` slot
    files via ``flock(LOCK_EX | LOCK_NB)``, retrying with
    ``asyncio.sleep(0.5)`` until enough are free. The kernel releases
    all locks held by a process when it dies, so crashed Hares
    processes never strand capacity.

    **Slot acquisition** (coord_dir unset): acquires from an in-process
    ``asyncio.Semaphore``. No cross-process coordination; each Hares
    process has its own independent cap.
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
        self._claimed_cores: list[int] = []

        self._slot_dir: Optional[Path] = None
        self._slot_files: list[Path] = []
        self._fallback_sem: Optional[asyncio.Semaphore] = None

        if coord_dir is None:
            # In-process fallback (0.1 behavior).
            self._fallback_sem = asyncio.Semaphore(max_concurrent)
            return

        coord_dir.mkdir(parents=True, exist_ok=True)
        self._slot_dir = coord_dir / SLOTS_DIR
        self._slot_dir.mkdir(exist_ok=True)
        self._slot_files = [
            self._slot_dir / f"slot-{i}.lock"
            for i in range(max_concurrent)
        ]
        for sf in self._slot_files:
            sf.touch()

    # ── Public API ────────────────────────────────────────────────────

    @property
    def is_shared(self) -> bool:
        """True iff coordination uses shared slot files (cross-process).
        False = in-process asyncio.Semaphore fallback."""
        return self._slot_dir is not None

    async def acquire_subprocess_slot(self, weight: int = 1) -> list[int]:
        """Acquire ``weight`` subprocess slots.

        With coord_dir: tries each slot file with LOCK_EX|LOCK_NB,
        retrying every 0.5s until ``weight`` are available. Returns a
        token (list of open, locked FDs) that must be passed to
        :meth:`release_subprocess_slot`.

        Without coord_dir: acquires from the in-process semaphore and
        returns an empty list (the semaphore itself is the token).

        Blocks indefinitely — slots are released when commands finish
        or when a holding process dies (kernel guarantee), so callers
        eventually make progress.
        """
        weight = max(1, min(weight, self._weight_cap))

        if self._slot_dir is not None:
            return await self._acquire_flock_slots(weight)

        assert self._fallback_sem is not None
        for _ in range(weight):
            await self._fallback_sem.acquire()
        return []

    async def _acquire_flock_slots(self, weight: int) -> list[int]:
        """Non-blocking flock attempt per slot, retry until weight acquired."""
        while True:
            acquired: list[int] = []
            for sf in self._slot_files:
                if len(acquired) >= weight:
                    break
                fd = os.open(sf, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired.append(fd)
                except OSError as exc:
                    os.close(fd)
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        # Unexpected error; release partial and propagate.
                        for afd in acquired:
                            try:
                                fcntl.flock(afd, fcntl.LOCK_UN)
                                os.close(afd)
                            except OSError:
                                pass
                        raise

            if len(acquired) >= weight:
                logger.debug(
                    "Acquired %d slot(s) via flock (fds=%s)", weight, acquired,
                )
                return acquired

            # Not enough free; release partial and yield to the event loop.
            for fd in acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                except OSError:
                    pass
            await asyncio.sleep(0.5)

    async def release_subprocess_slot(
        self,
        slot_token: list[int],
        weight: int = 1,
    ) -> None:
        """Release slots previously acquired by :meth:`acquire_subprocess_slot`.

        ``slot_token`` is the list of FDs returned by acquire (flock
        path). ``weight`` is used only for the in-process fallback path
        (where slot_token is empty).
        """
        if self._slot_dir is not None:
            for fd in slot_token:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                except OSError:
                    pass
            return

        assert self._fallback_sem is not None
        weight = max(1, min(weight, self._weight_cap))
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
        # flock(LOCK_EX) blocks until the brief critical section in any
        # sibling process completes; it auto-releases if we crash.
        pool_path = _core_pool_path(self._coord_dir)
        with self._core_pool_lock(pool_path):
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
            with self._core_pool_lock(pool_path):
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

    # ── Internal: core-pool file I/O under fcntl.flock ────────────────

    class _LockCtx:
        """Context manager for an exclusive fcntl.flock on the core-pool
        file. Creates the file if needed. Blocking: the critical section
        held by siblings is a few microseconds; blocking is correct and
        the kernel auto-releases on process death."""
        def __init__(self, path: Path) -> None:
            self._path = path
            self._fd: Optional[int] = None

        def __enter__(self) -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(self._fd, fcntl.LOCK_EX)

        def __exit__(self, exc_type, exc, tb) -> None:
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)

    def _core_pool_lock(self, path: Path) -> "_LockCtx":
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
    on exit. Slot files do not need cleanup — the kernel releases all
    flocks when the process exits."""
    import atexit
    atexit.register(coordinator.release_cores)


# ── Slot file diagnostics (used by doctor) ─────────────────────────────


def count_free_slots(coord_dir: Path, max_concurrent: int) -> tuple[int, int]:
    """Return (free, total) slot counts by attempting non-blocking flocks.

    Only valid when called from a process that holds no slots itself
    (e.g. the doctor command). Each probe opens and immediately releases
    the slot file, so this is a best-effort snapshot.
    """
    slot_dir = coord_dir / SLOTS_DIR
    total = max_concurrent
    free = 0
    for i in range(max_concurrent):
        sf = slot_dir / f"slot-{i}.lock"
        if not sf.exists():
            free += 1  # file missing = never created = free
            continue
        try:
            fd = os.open(sf, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                free += 1
            except OSError:
                pass  # locked by another process
            finally:
                os.close(fd)
        except OSError:
            pass
    return free, total
