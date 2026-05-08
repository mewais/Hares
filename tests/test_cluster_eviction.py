"""Tests for the job-map eviction policy in ClusterExecutor.

The base executor caps how many job records it keeps in memory at the
module-level constant ``hares.cluster.base.MAX_RETAINED_JOBS``. Once
exceeded, OLDEST TERMINAL records are evicted FIFO. PEND/RUN records
are never evicted (so a job stuck forever can't be silently dropped).
Evicted IDs are remembered so a later wait() call returns 'EVICTED'
instead of 'never heard of it'.

Tests use the LSF backend because both backends share the base
implementation; SLURM coverage of the same logic adds nothing.
Tests monkeypatch MAX_RETAINED_JOBS to a small value so we don't have
to actually push 1000 records through to exercise the cap.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hares.cluster import base as cluster_base
from hares.cluster.lsf import (
    JobSpec,
    LsfConfig,
    LsfExecutor,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_executor(tmp_path: Path) -> LsfExecutor:
    cfg = LsfConfig(
        poll_interval_sec=0.01,
        default_timeout_sec=10.0,
        output_dir=tmp_path / "out",
        queue=None,
        default_resource_spec=None,
        bsub_bin="bsub",
        bjobs_bin="bjobs",
        bkill_bin="bkill",
    )
    return LsfExecutor(cfg=cfg)


def _set_cap(monkeypatch, cap: int) -> None:
    monkeypatch.setattr(cluster_base, "MAX_RETAINED_JOBS", cap)


def _bsub_ok(jid: str) -> tuple[int, str, str]:
    return 0, f"Job <{jid}> is submitted to queue <q>.\n", ""


async def _submit_n_done(ex: LsfExecutor, n: int, start: int = 1) -> list[str]:
    """Submit N jobs and walk each through to DONE so the eviction path
    is exercised."""
    job_ids = [str(start + i) for i in range(n)]
    ex._run = AsyncMock(side_effect=[_bsub_ok(jid) for jid in job_ids])
    await ex.submit([JobSpec(command=f"echo {jid}") for jid in job_ids])
    for jid in job_ids:
        rec = ex._jobs[jid]
        Path(rec.stdout_file).write_text(f"out{jid}\n")
        Path(rec.stderr_file).write_text("")
        Path(rec.exitcode_file).write_text("0\n")
    def done(argv):
        jid = argv[-1]
        return (0, f"{jid} user DONE q host host\n", "")
    ex._run = AsyncMock(side_effect=done)
    await ex.wait(job_ids, timeout_sec=5.0)
    return job_ids


# ── Default cap is 1000 ─────────────────────────────────────────────────────

def test_default_cap_is_1000():
    """Sanity check that the module constant is the documented default."""
    assert cluster_base.MAX_RETAINED_JOBS == 1000


# ── No eviction below the cap ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_under_cap_no_eviction(tmp_path, monkeypatch):
    _set_cap(monkeypatch, 10)
    ex = _make_executor(tmp_path)
    await _submit_n_done(ex, 5)
    assert len(ex._jobs) == 5
    assert ex._evicted_ids == set()


# ── At cap + 1 → evict 1 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_at_cap_plus_one_evicts_oldest(tmp_path, monkeypatch):
    _set_cap(monkeypatch, 3)
    ex = _make_executor(tmp_path)
    await _submit_n_done(ex, 4, start=10)  # 10, 11, 12, 13
    # Cap is 3; oldest (10) should be evicted.
    assert "10" in ex._evicted_ids
    assert len(ex._jobs) == 3
    assert set(ex._jobs.keys()) == {"11", "12", "13"}


# ── Many over cap ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_many_over_cap_evicts_to_match(tmp_path, monkeypatch):
    _set_cap(monkeypatch, 2)
    ex = _make_executor(tmp_path)
    await _submit_n_done(ex, 10, start=100)
    # After all 10 done → only the last 2 remain.
    assert len(ex._jobs) == 2
    assert len(ex._evicted_ids) == 8


# ── PEND records are NEVER evicted ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_records_not_evicted(tmp_path, monkeypatch):
    """If the cap is full of PEND records and a new one comes in, we
    don't drop a still-pending job; we just exceed the cap with a
    log line."""
    _set_cap(monkeypatch, 2)
    ex = _make_executor(tmp_path)
    ex._run = AsyncMock(side_effect=[_bsub_ok("1"), _bsub_ok("2"), _bsub_ok("3")])
    await ex.submit([
        JobSpec(command="a"), JobSpec(command="b"), JobSpec(command="c"),
    ])
    async with ex._lock:
        ex._maybe_evict_terminal()
    # All three are still PEND → none eligible for eviction.
    assert len(ex._jobs) == 3
    assert ex._evicted_ids == set()


# ── Mixed PEND + DONE: only DONE evicted ─────────────────────────────────────

@pytest.mark.asyncio
async def test_mixed_pend_and_done_only_done_evicted(tmp_path, monkeypatch):
    """Cap=3 with two DONE + two PEND → one DONE evicted, the PEND
    records stay."""
    _set_cap(monkeypatch, 3)
    ex = _make_executor(tmp_path)

    # Submit 4 in order: 1, 2 (will become DONE) then 3, 4 (stay PEND).
    ids = ["1", "2", "3", "4"]
    ex._run = AsyncMock(side_effect=[_bsub_ok(jid) for jid in ids])
    await ex.submit([JobSpec(command=f"echo {jid}") for jid in ids])

    for jid in ["1", "2"]:
        rec = ex._jobs[jid]
        Path(rec.stdout_file).write_text("ok\n")
        Path(rec.stderr_file).write_text("")
        Path(rec.exitcode_file).write_text("0\n")

    def done(argv):
        jid = argv[-1]
        return (0, f"{jid} user DONE q host host\n", "")
    ex._run = AsyncMock(side_effect=done)
    await ex.wait(["1", "2"], timeout_sec=5.0)

    # 4 jobs total, cap=3 → one evicted; must be 1 (oldest TERMINAL).
    assert "1" in ex._evicted_ids
    assert len(ex._jobs) == 3
    assert set(ex._jobs.keys()) == {"2", "3", "4"}
    assert ex._jobs["3"].status == "PEND"
    assert ex._jobs["4"].status == "PEND"


# ── wait() on an evicted job returns EVICTED entry ───────────────────────────

@pytest.mark.asyncio
async def test_wait_on_evicted_id_returns_evicted_status(tmp_path, monkeypatch):
    _set_cap(monkeypatch, 2)
    ex = _make_executor(tmp_path)
    await _submit_n_done(ex, 4, start=20)  # 20 + 21 evicted
    results = await ex.wait(["20"], timeout_sec=1.0)
    assert results["20"]["status"] == "EVICTED"
    assert "evicted" in results["20"]["error"].lower()
    assert "retention cap" in results["20"]["error"].lower()


# ── wait() distinguishes EVICTED from "never seen" ───────────────────────────

@pytest.mark.asyncio
async def test_wait_on_unknown_id_returns_error_not_evicted(tmp_path, monkeypatch):
    _set_cap(monkeypatch, 10)
    ex = _make_executor(tmp_path)
    results = await ex.wait(["never-submitted"], timeout_sec=1.0)
    r = results["never-submitted"]
    assert r["status"] == "ERROR"
    # Make sure it's the "unknown" message, not the "evicted" one.
    assert "not known" in r["error"]


# ── Cancel triggers eviction too ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_triggers_eviction(tmp_path, monkeypatch):
    _set_cap(monkeypatch, 2)
    ex = _make_executor(tmp_path)
    ids = ["7", "8", "9"]
    ex._run = AsyncMock(side_effect=[_bsub_ok(jid) for jid in ids])
    await ex.submit([JobSpec(command=f"echo {jid}") for jid in ids])
    ex._run = AsyncMock(return_value=(0, "killed\n", ""))
    await ex.cancel(["7", "8"])
    # Cap is 2, 3 records, two are CANCELED → one evicted (oldest CANCELED = 7).
    assert len(ex._jobs) <= 2
    assert "7" in ex._evicted_ids


# ── Cap of 0 disables eviction ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cap_zero_disables_eviction(tmp_path, monkeypatch):
    _set_cap(monkeypatch, 0)
    ex = _make_executor(tmp_path)
    await _submit_n_done(ex, 50, start=500)
    assert len(ex._jobs) == 50
    assert ex._evicted_ids == set()
