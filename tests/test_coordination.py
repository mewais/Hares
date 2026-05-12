"""Tests for hares.coordination — per-slot flock files + core pool.

Flock-based slot tests use a unique coordination dir per test (via
tmp_path) so the suite is parallel-safe and no cleanup of kernel
state is required between runs.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from hares.coordination import CrossProcessCoordinator, count_free_slots


# ── In-process fallback ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_coord_dir_uses_in_process_semaphore(tmp_path):
    coord = CrossProcessCoordinator(coord_dir=None, max_concurrent=2)
    assert coord.is_shared is False
    token = await coord.acquire_subprocess_slot()
    assert token == []  # no FDs for in-process path
    await coord.release_subprocess_slot(token, weight=1)


@pytest.mark.asyncio
async def test_in_process_caps_concurrency():
    """In-process semaphore: 3 workers against cap=2 → max 2 in flight."""
    coord = CrossProcessCoordinator(coord_dir=None, max_concurrent=2)

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal in_flight, max_in_flight
        token = await coord.acquire_subprocess_slot()
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        await coord.release_subprocess_slot(token, weight=1)

    await asyncio.gather(worker(), worker(), worker())
    assert max_in_flight <= 2


# ── Flock-based slot files (shared) ───────────────────────────────────


@pytest.mark.asyncio
async def test_flock_acquires_and_releases(tmp_path):
    coord_dir = tmp_path / "coord"
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)
    assert coord.is_shared is True

    token = await coord.acquire_subprocess_slot()
    assert len(token) == 1  # weight=1 → one locked FD
    await coord.release_subprocess_slot(token, weight=1)

    # After release the slot is free again.
    free, total = count_free_slots(coord_dir, 2)
    assert free == total == 2


@pytest.mark.asyncio
async def test_flock_caps_concurrency(tmp_path):
    """4 workers, cap=2: at most 2 in flight at once."""
    coord_dir = tmp_path / "coord"
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal in_flight, max_in_flight
        token = await coord.acquire_subprocess_slot()
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        await coord.release_subprocess_slot(token, weight=1)

    await asyncio.gather(worker(), worker(), worker(), worker())
    assert max_in_flight <= 2


@pytest.mark.asyncio
async def test_flock_slot_files_created(tmp_path):
    coord_dir = tmp_path / "coord"
    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=3)
    slot_dir = coord_dir / "slots"
    assert slot_dir.is_dir()
    for i in range(3):
        assert (slot_dir / f"slot-{i}.lock").exists()


@pytest.mark.asyncio
async def test_flock_all_slots_occupied_then_released(tmp_path):
    """Acquire all slots, verify none free, release, verify all free."""
    cap = 2
    coord_dir = tmp_path / "coord"
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)

    tokens = []
    for _ in range(cap):
        t = await coord.acquire_subprocess_slot()
        tokens.append(t)

    free, total = count_free_slots(coord_dir, cap)
    assert free == 0
    assert total == cap

    for t in tokens:
        await coord.release_subprocess_slot(t, weight=1)

    free, total = count_free_slots(coord_dir, cap)
    assert free == total == cap


# ── count_free_slots ───────────────────────────────────────────────────


def test_count_free_slots_all_free(tmp_path):
    coord_dir = tmp_path / "coord"
    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=4)
    free, total = count_free_slots(coord_dir, 4)
    assert free == total == 4


def test_count_free_slots_missing_slot_dir(tmp_path):
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    # No slot dir created — all slots treated as free (missing = never used).
    free, total = count_free_slots(coord_dir, 2)
    assert free == 2


# ── Core pool ─────────────────────────────────────────────────────────


def test_core_pool_no_coord_dir_uses_first_n():
    coord = CrossProcessCoordinator(coord_dir=None, max_concurrent=2)
    cores = coord.claim_cores()
    # Result may be empty on platforms without sched_getaffinity.
    assert isinstance(cores, list)


@pytest.mark.skipif(
    not hasattr(os, "sched_getaffinity"),
    reason="sched_getaffinity unavailable on this platform",
)
def test_core_pool_with_coord_dir_records_pid(tmp_path):
    coord_dir = tmp_path / "coord"
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)
    cores = coord.claim_cores()
    pool_file = coord_dir / "core_pool.json"
    assert pool_file.exists()
    data = json.loads(pool_file.read_text())
    assert str(os.getpid()) in data.get("claimed", {})
    coord.release_cores()
    data2 = json.loads(pool_file.read_text())
    assert str(os.getpid()) not in data2.get("claimed", {})


def test_core_pool_release_is_idempotent(tmp_path):
    coord_dir = tmp_path / "coord"
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)
    coord.claim_cores()
    coord.release_cores()
    coord.release_cores()  # second call must not raise
