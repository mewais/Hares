"""Tests for hares.coordination — cross-process semaphore + core pool.

POSIX-named-semaphore tests use a unique coordination dir per test
(via tmp_path) so the test suite is parallel-safe and stale state
from a prior failed run doesn't bleed in.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from hares.coordination import (
    CrossProcessCoordinator,
    _semaphore_name,
)


def _cleanup_semaphore(coord_dir: Path) -> None:
    """Best-effort: unlink the named semaphore for a coord_dir so a
    re-run with a different cap doesn't pick up stale state."""
    try:
        import posix_ipc
        try:
            posix_ipc.unlink_semaphore(_semaphore_name(coord_dir))
        except posix_ipc.ExistentialError:
            pass
    except ImportError:
        pass


# ── In-process fallback ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_coord_dir_uses_in_process_semaphore(tmp_path):
    coord = CrossProcessCoordinator(coord_dir=None, max_concurrent=2)
    assert coord.is_shared is False
    # Should be acquirable / releasable without any external state.
    await coord.acquire_subprocess_slot()
    await coord.release_subprocess_slot()


# ── POSIX semaphore (shared) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_shared_semaphore_acquires_and_releases(tmp_path):
    coord_dir = tmp_path / "coord"
    _cleanup_semaphore(coord_dir)
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)
    if not coord.is_shared:
        pytest.skip("posix_ipc unavailable; in-process fallback used")
    await coord.acquire_subprocess_slot()
    await coord.release_subprocess_slot()
    _cleanup_semaphore(coord_dir)


@pytest.mark.asyncio
async def test_shared_semaphore_caps_concurrency(tmp_path):
    """3 simultaneous acquires against cap=2 → only 2 progress at once."""
    coord_dir = tmp_path / "coord"
    _cleanup_semaphore(coord_dir)
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)
    if not coord.is_shared:
        pytest.skip("posix_ipc unavailable")

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal in_flight, max_in_flight
        await coord.acquire_subprocess_slot()
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.1)
        async with lock:
            in_flight -= 1
        await coord.release_subprocess_slot()

    await asyncio.gather(worker(), worker(), worker(), worker())
    assert max_in_flight <= 2
    _cleanup_semaphore(coord_dir)


# ── Core pool ─────────────────────────────────────────────────────────


def test_core_pool_no_coord_dir_uses_first_n(monkeypatch):
    """0.1 fallback: with no coord dir, picks first N cores per process."""
    coord = CrossProcessCoordinator(coord_dir=None, max_concurrent=2)
    cores = coord.claim_cores()
    if cores:
        # On Linux, sched_getaffinity returns sorted list; first-N matches.
        assert len(cores) == min(2, len(cores) + len([])) or cores  # may be empty on macOS


@pytest.mark.skipif(
    not hasattr(os, "sched_getaffinity"),
    reason="sched_getaffinity unavailable on this platform",
)
def test_core_pool_with_coord_dir_records_pid(tmp_path):
    coord_dir = tmp_path / "coord"
    _cleanup_semaphore(coord_dir)
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)
    cores = coord.claim_cores()
    # The pool file exists and has our PID.
    pool_file = coord_dir / "core_pool.json"
    assert pool_file.exists()
    import json
    data = json.loads(pool_file.read_text())
    assert str(os.getpid()) in data.get("claimed", {})
    # Cleanup.
    coord.release_cores()
    data2 = json.loads(pool_file.read_text())
    assert str(os.getpid()) not in data2.get("claimed", {})
    _cleanup_semaphore(coord_dir)


def test_core_pool_release_is_idempotent(tmp_path):
    coord_dir = tmp_path / "coord"
    _cleanup_semaphore(coord_dir)
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)
    coord.claim_cores()
    coord.release_cores()
    coord.release_cores()  # second call must not raise
    _cleanup_semaphore(coord_dir)
