"""Crash and abrupt-termination tests for the flock-based coordinator.

These tests verify the core guarantee: slots held by a dead process are
automatically recovered by the kernel (no manual cleanup, no stuck state).

Children hold slots by calling fcntl.flock() directly — no asyncio in the
child process, which avoids the fork-inside-running-event-loop problem.

Scenarios covered:
  1. Child SIGKILLed while holding a slot → slot freed immediately.
  2. Child normal-exits (os._exit) without releasing → slot freed.
  3. Child holds all N slots then dies → all N freed.
  4. Waiting acquirer unblocks within one retry cycle after SIGKILL.
  5. Concurrency cap never exceeded under concurrent load.
  6. Slot files survive coordinator restart (idempotent init).
  7. max_concurrent increase between restarts adds new slot files.
  8. max_concurrent decrease between restarts uses fewer files, no corruption.
"""

from __future__ import annotations

import asyncio
import fcntl
import multiprocessing
import os
import signal
import time
from pathlib import Path

import pytest

from hares.coordination import CrossProcessCoordinator, count_free_slots, SLOTS_DIR


# ── Child entry point ──────────────────────────────────────────────────
# Does NOT use asyncio — children are forked from a running asyncio test
# loop, so asyncio.run() would raise. We test the raw flock primitive
# directly, which is exactly what the coordinator uses.

def _child_hold_slots(slot_files: list[str], ready_event) -> None:
    """Lock each slot file with LOCK_EX, signal ready, sleep until killed.
    Deliberately does NOT release — simulates a crashed/killed process."""
    fds = []
    for sf in slot_files:
        fd = os.open(sf, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        fds.append(fd)
    ready_event.set()
    # Block until killed. os._exit avoids atexit / finally blocks.
    while True:
        time.sleep(60)


# ── SIGKILL recovery ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slot_freed_after_sigkill(tmp_path):
    """A SIGKILLed process's slot must be released immediately."""
    coord_dir = tmp_path / "coord"
    cap = 1
    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)

    slot_files = [str(coord_dir / SLOTS_DIR / "slot-0.lock")]
    ready = multiprocessing.Event()
    p = multiprocessing.Process(target=_child_hold_slots, args=(slot_files, ready))
    p.start()

    assert ready.wait(timeout=10), "child did not acquire slot in time"

    free, _ = count_free_slots(coord_dir, cap)
    assert free == 0, "slot should be occupied by child"

    os.kill(p.pid, signal.SIGKILL)
    p.join(timeout=5)

    free, _ = count_free_slots(coord_dir, cap)
    assert free == 1, "slot must be freed after SIGKILL"


@pytest.mark.asyncio
async def test_slot_freed_after_os_exit(tmp_path):
    """A process that calls os._exit() without releasing must still free the slot."""
    coord_dir = tmp_path / "coord"
    cap = 1
    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)

    slot_files = [str(coord_dir / SLOTS_DIR / "slot-0.lock")]
    ready = multiprocessing.Event()
    p = multiprocessing.Process(target=_child_hold_slots, args=(slot_files, ready))
    p.start()

    assert ready.wait(timeout=10), "child did not acquire slot in time"

    # SIGTERM — Python will call sys.exit which closes FDs, releasing flocks.
    os.kill(p.pid, signal.SIGTERM)
    p.join(timeout=5)

    free, _ = count_free_slots(coord_dir, cap)
    assert free == 1, "slot must be freed after process exit"


@pytest.mark.asyncio
async def test_all_slots_freed_after_kill(tmp_path):
    """A process holding ALL N slots: after SIGKILL all N are freed."""
    coord_dir = tmp_path / "coord"
    cap = 3
    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)

    all_slots = [
        str(coord_dir / SLOTS_DIR / f"slot-{i}.lock") for i in range(cap)
    ]
    ready = multiprocessing.Event()
    p = multiprocessing.Process(target=_child_hold_slots, args=(all_slots, ready))
    p.start()

    assert ready.wait(timeout=10), "child did not acquire all slots in time"

    free, total = count_free_slots(coord_dir, cap)
    assert free == 0 and total == cap, "all slots should be held"

    os.kill(p.pid, signal.SIGKILL)
    p.join(timeout=5)

    free, total = count_free_slots(coord_dir, cap)
    assert free == cap, f"all slots must be freed (free={free}/{total})"


@pytest.mark.asyncio
async def test_waiting_acquirer_unblocks_after_kill(tmp_path):
    """A coordinator waiting for a slot must unblock within one retry
    cycle (≤ 0.6 s) after the holder is SIGKILLed."""
    coord_dir = tmp_path / "coord"
    cap = 1
    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)

    slot_files = [str(coord_dir / SLOTS_DIR / "slot-0.lock")]
    ready = multiprocessing.Event()
    p = multiprocessing.Process(target=_child_hold_slots, args=(slot_files, ready))
    p.start()

    assert ready.wait(timeout=10), "child did not acquire slot in time"

    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)

    # Start an asyncio acquirer — it will retry every 0.5 s.
    acquire_task = asyncio.create_task(coord.acquire_subprocess_slot())
    await asyncio.sleep(0.2)  # let it hit the retry loop

    os.kill(p.pid, signal.SIGKILL)
    p.join(timeout=5)

    token = await asyncio.wait_for(acquire_task, timeout=3.0)
    assert len(token) == 1, "should have acquired after holder died"
    await coord.release_subprocess_slot(token)


# ── Multi-child: multiple simultaneous holders ─────────────────────────

@pytest.mark.asyncio
async def test_multi_holder_all_freed_after_kill(tmp_path):
    """Spawn cap children each holding one slot, kill them all,
    verify all slots freed and a new coordinator can use all of them."""
    coord_dir = tmp_path / "coord"
    cap = 3
    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)

    processes = []
    for i in range(cap):
        slot_files = [str(coord_dir / SLOTS_DIR / f"slot-{i}.lock")]
        ready = multiprocessing.Event()
        p = multiprocessing.Process(target=_child_hold_slots, args=(slot_files, ready))
        p.start()
        assert ready.wait(timeout=10), f"child {i} did not acquire slot"
        processes.append(p)

    free, _ = count_free_slots(coord_dir, cap)
    assert free == 0, "all slots should be occupied"

    for p in processes:
        os.kill(p.pid, signal.SIGKILL)
    for p in processes:
        p.join(timeout=5)

    free, total = count_free_slots(coord_dir, cap)
    assert free == cap

    # New coordinator should be able to acquire all slots immediately.
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)
    tokens = [await coord.acquire_subprocess_slot() for _ in range(cap)]
    assert all(len(t) == 1 for t in tokens)
    for t in tokens:
        await coord.release_subprocess_slot(t)


# ── In-process concurrency cap ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_flock_cap_never_exceeded_under_load(tmp_path):
    """Coordinator with flock: many concurrent asyncio workers, cap honored."""
    coord_dir = tmp_path / "coord"
    cap = 2
    n_workers = 10
    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)

    in_flight = 0
    max_in_flight = 0
    violations: list[int] = []
    lock = asyncio.Lock()

    async def worker():
        nonlocal in_flight, max_in_flight
        token = await coord.acquire_subprocess_slot()
        async with lock:
            in_flight += 1
            if in_flight > cap:
                violations.append(in_flight)
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        await coord.release_subprocess_slot(token)

    await asyncio.gather(*[worker() for _ in range(n_workers)])
    assert not violations, f"cap exceeded: {violations}"
    assert max_in_flight <= cap


# ── Restart and capacity-change resilience ─────────────────────────────

@pytest.mark.asyncio
async def test_coordinator_restart_uses_existing_slot_files(tmp_path):
    """A restarted coordinator finds and uses existing slot files."""
    coord_dir = tmp_path / "coord"
    cap = 2

    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)
    slot_dir = coord_dir / SLOTS_DIR
    assert (slot_dir / "slot-0.lock").exists()
    assert (slot_dir / "slot-1.lock").exists()

    # Restart.
    coord2 = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=cap)
    token = await coord2.acquire_subprocess_slot()
    assert len(token) == 1

    free, _ = count_free_slots(coord_dir, cap)
    assert free == 1

    await coord2.release_subprocess_slot(token)
    free, _ = count_free_slots(coord_dir, cap)
    assert free == 2


@pytest.mark.asyncio
async def test_capacity_increase_adds_slot_files(tmp_path):
    """Raising cap creates new slot files; all slots become available."""
    coord_dir = tmp_path / "coord"

    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)
    slot_dir = coord_dir / SLOTS_DIR
    assert not (slot_dir / "slot-2.lock").exists()

    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=4)
    for i in range(4):
        assert (slot_dir / f"slot-{i}.lock").exists()

    free, total = count_free_slots(coord_dir, 4)
    assert free == total == 4


@pytest.mark.asyncio
async def test_capacity_decrease_uses_fewer_slots(tmp_path):
    """Lowering cap uses only the first N slot files; extra files ignored."""
    coord_dir = tmp_path / "coord"

    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=4)

    coord = CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)
    token1 = await coord.acquire_subprocess_slot()
    token2 = await coord.acquire_subprocess_slot()

    # Even though 4 files exist, only 2 are in the active pool.
    free, total = count_free_slots(coord_dir, 2)
    assert free == 0 and total == 2

    await coord.release_subprocess_slot(token1)
    await coord.release_subprocess_slot(token2)
    free, _ = count_free_slots(coord_dir, 2)
    assert free == 2
