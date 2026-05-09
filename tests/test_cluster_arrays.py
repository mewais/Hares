"""Tests for SLURM + LSF job-array support.

Both backends share the array-handling logic in ``ClusterExecutor``
(placeholder substitution into output paths, ``_collect_array_result``
glob+aggregate). Backend-specific differences:

  - SLURM: ``sbatch --array=...`` flag, ``%a`` placeholder,
    ``$SLURM_ARRAY_TASK_ID`` env var.
  - LSF: ``bsub -J 'name[<spec>]'`` (the bracket is part of the job
    name argument, not a separate flag), ``%I`` placeholder,
    ``$LSB_JOBINDEX`` env var.

Tests monkeypatch ``_run`` so neither sbatch nor bsub is required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hares.cluster import JobSpec
from hares.cluster.lsf import LsfConfig, LsfExecutor
from hares.cluster.slurm import SlurmConfig, SlurmExecutor


# ── Fixtures ────────────────────────────────────────────────────────────────

def _slurm_executor(tmp_path: Path) -> SlurmExecutor:
    cfg = SlurmConfig(
        poll_interval_sec=0.01,
        default_timeout_sec=10.0,
        output_dir=tmp_path / "slurm-out",
        partition=None, account=None, default_resource_spec=None,
        sbatch_bin="sbatch", squeue_bin="squeue", scancel_bin="scancel",
    )
    return SlurmExecutor(cfg=cfg)


def _lsf_executor(tmp_path: Path) -> LsfExecutor:
    cfg = LsfConfig(
        poll_interval_sec=0.01,
        default_timeout_sec=10.0,
        output_dir=tmp_path / "lsf-out",
        queue=None, default_resource_spec=None,
        bsub_bin="bsub", bjobs_bin="bjobs", bkill_bin="bkill",
    )
    return LsfExecutor(cfg=cfg)


def _sbatch_ok(jid: str) -> tuple[int, str, str]:
    return 0, f"{jid}\n", ""


def _bsub_ok(jid: str) -> tuple[int, str, str]:
    return 0, f"Job <{jid}> is submitted to queue <q>.\n", ""


# ── SLURM: argv shape ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slurm_array_adds_array_flag(tmp_path):
    ex = _slurm_executor(tmp_path)
    captured: list[str] = []

    async def fake_run(argv):
        captured.extend(argv)
        return _sbatch_ok("1234")

    ex._run = fake_run
    await ex.submit([JobSpec(command="echo task", array="1-10")])
    assert "--array=1-10" in captured


@pytest.mark.asyncio
async def test_slurm_array_inner_shell_uses_task_id_env_var(tmp_path):
    ex = _slurm_executor(tmp_path)
    captured: list[str] = []

    async def fake_run(argv):
        captured.extend(argv)
        return _sbatch_ok("2222")

    ex._run = fake_run
    await ex.submit([JobSpec(command="my_sim", array="1-5")])
    # The --wrap argument is the inner shell.
    wrap_idx = captured.index("--wrap")
    inner = captured[wrap_idx + 1]
    assert "${SLURM_ARRAY_TASK_ID}" in inner
    # Per-task path expansion: the placeholder shouldn't survive
    # literally in the inner shell — it gets replaced by the env-var
    # interpolation.
    assert "%a" not in inner


@pytest.mark.asyncio
async def test_slurm_array_record_stores_template_paths(tmp_path):
    ex = _slurm_executor(tmp_path)
    ex._run = AsyncMock(return_value=_sbatch_ok("3333"))
    await ex.submit([JobSpec(command="echo", array="1-3")])
    rec = ex._jobs["3333"]
    assert rec.array_spec == "1-3"
    # Path templates carry the literal %a so _collect_array_result
    # can glob them later.
    assert rec.exitcode_file.endswith(".%a")
    assert rec.stdout_file.endswith(".%a")


# ── LSF: argv shape ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lsf_array_uses_bracket_in_job_name(tmp_path):
    """LSF expresses arrays as -J 'name[1-100]' rather than a flag."""
    ex = _lsf_executor(tmp_path)
    captured: list[str] = []

    async def fake_run(argv):
        captured.extend(argv)
        return _bsub_ok("4444")

    ex._run = fake_run
    await ex.submit([JobSpec(command="run_sim", name="sweep", array="1-100")])
    j_idx = captured.index("-J")
    assert captured[j_idx + 1] == "sweep[1-100]"


@pytest.mark.asyncio
async def test_lsf_array_inner_shell_uses_jobindex(tmp_path):
    ex = _lsf_executor(tmp_path)
    captured: list[str] = []

    async def fake_run(argv):
        captured.extend(argv)
        return _bsub_ok("5555")

    ex._run = fake_run
    await ex.submit([JobSpec(command="echo", array="1-5")])
    # Inner shell is the last argv entry after /bin/sh -c.
    inner = captured[-1]
    assert "${LSB_JOBINDEX}" in inner
    assert "%I" not in inner


@pytest.mark.asyncio
async def test_lsf_array_record_stores_template_paths(tmp_path):
    ex = _lsf_executor(tmp_path)
    ex._run = AsyncMock(return_value=_bsub_ok("6666"))
    await ex.submit([JobSpec(command="echo", array="1-3")])
    rec = ex._jobs["6666"]
    assert rec.array_spec == "1-3"
    assert rec.exitcode_file.endswith(".%I")


# ── Status aggregation across array tasks ─────────────────────────────────

@pytest.mark.asyncio
async def test_slurm_array_pending_aggregates_to_pend(tmp_path):
    """squeue prints one line per task; if any is PENDING (and none
    RUNNING), the array as a whole is PEND."""
    ex = _slurm_executor(tmp_path)
    ex._run = AsyncMock(return_value=_sbatch_ok("7777"))
    await ex.submit([JobSpec(command="echo", array="1-3")])
    # Mock squeue: 3 tasks all PENDING.
    ex._run = AsyncMock(return_value=(0, "PENDING\nPENDING\nPENDING\n", ""))
    status = await ex._poll_status("7777")
    assert status == "PEND"


@pytest.mark.asyncio
async def test_slurm_array_any_running_aggregates_to_run(tmp_path):
    """If any task is RUNNING, array is RUN even if others are PENDING."""
    ex = _slurm_executor(tmp_path)
    ex._run = AsyncMock(return_value=_sbatch_ok("8888"))
    await ex.submit([JobSpec(command="echo", array="1-3")])
    ex._run = AsyncMock(return_value=(0, "RUNNING\nPENDING\nPENDING\n", ""))
    status = await ex._poll_status("8888")
    assert status == "RUN"


@pytest.mark.asyncio
async def test_lsf_array_running_aggregation(tmp_path):
    ex = _lsf_executor(tmp_path)
    ex._run = AsyncMock(return_value=_bsub_ok("9001"))
    await ex.submit([JobSpec(command="echo", array="1-3")])
    bjobs_out = (
        "9001[1] user RUN  q host host name 01/01-12:00:00\n"
        "9001[2] user PEND q host host name 01/01-12:00:00\n"
        "9001[3] user PEND q host host name 01/01-12:00:00\n"
    )
    ex._run = AsyncMock(return_value=(0, bjobs_out, ""))
    status = await ex._poll_status("9001")
    assert status == "RUN"


# ── End-to-end: per-task file aggregation ──────────────────────────────────

@pytest.mark.asyncio
async def test_slurm_array_collects_per_task_results(tmp_path):
    """Once the inner shell has written per-task files, wait()'s
    _collect_array_result should glob and roll them up."""
    ex = _slurm_executor(tmp_path)
    ex._run = AsyncMock(return_value=_sbatch_ok("1010"))
    await ex.submit([JobSpec(command="echo", array="1-4")])

    # Simulate the inner shell having run each task: write per-task
    # files at the templated paths (substitute %a → task id).
    rec = ex._jobs["1010"]
    base_out = rec.stdout_file.replace("%a", "")
    for tid in ["1", "2", "3", "4"]:
        Path(rec.stdout_file.replace("%a", tid)).write_text(f"out task {tid}\n")
        Path(rec.stderr_file.replace("%a", tid)).write_text("")
        Path(rec.exitcode_file.replace("%a", tid)).write_text("0\n")

    # squeue empty → array left the queue → DONE → _collect_array_result.
    ex._run = AsyncMock(return_value=(0, "", ""))
    results = await ex.wait(["1010"], timeout_sec=5.0)
    r = results["1010"]
    assert r["status"] == "DONE"
    assert r["exit_code"] == 0
    assert set(r["tasks"].keys()) == {"1", "2", "3", "4"}
    assert r["tasks"]["1"]["stdout"] == "out task 1\n"
    assert r["tasks"]["3"]["exit_code"] == 0
    assert r["summary"] == {"done": 4, "failed": 0, "unknown": 0}


@pytest.mark.asyncio
async def test_slurm_array_partial_failure_rolls_up_to_exit(tmp_path):
    ex = _slurm_executor(tmp_path)
    ex._run = AsyncMock(return_value=_sbatch_ok("1111"))
    await ex.submit([JobSpec(command="run", array="1-3")])

    rec = ex._jobs["1111"]
    Path(rec.stdout_file.replace("%a", "1")).write_text("ok\n")
    Path(rec.stderr_file.replace("%a", "1")).write_text("")
    Path(rec.exitcode_file.replace("%a", "1")).write_text("0\n")
    Path(rec.stdout_file.replace("%a", "2")).write_text("")
    Path(rec.stderr_file.replace("%a", "2")).write_text("crash\n")
    Path(rec.exitcode_file.replace("%a", "2")).write_text("2\n")
    Path(rec.stdout_file.replace("%a", "3")).write_text("ok\n")
    Path(rec.stderr_file.replace("%a", "3")).write_text("")
    Path(rec.exitcode_file.replace("%a", "3")).write_text("0\n")

    ex._run = AsyncMock(return_value=(0, "", ""))
    results = await ex.wait(["1111"], timeout_sec=5.0)
    r = results["1111"]
    # Aggregate: 2 done, 1 failed → array is EXIT.
    assert r["status"] == "EXIT"
    assert r["exit_code"] == 1
    assert r["summary"] == {"done": 2, "failed": 1, "unknown": 0}
    assert r["tasks"]["2"]["status"] == "EXIT"
    assert r["tasks"]["2"]["stderr"] == "crash\n"


@pytest.mark.asyncio
async def test_lsf_array_collects_per_task_results(tmp_path):
    """Same end-to-end aggregation, but for LSF (different placeholder)."""
    ex = _lsf_executor(tmp_path)
    ex._run = AsyncMock(return_value=_bsub_ok("2020"))
    await ex.submit([JobSpec(command="echo", array="1-3")])

    rec = ex._jobs["2020"]
    for tid in ["1", "2", "3"]:
        Path(rec.stdout_file.replace("%I", tid)).write_text(f"task {tid}\n")
        Path(rec.stderr_file.replace("%I", tid)).write_text("")
        Path(rec.exitcode_file.replace("%I", tid)).write_text("0\n")

    # bjobs returns "not found" once the array is fully done.
    ex._run = AsyncMock(return_value=(255, "", "Job <2020> is not found"))
    results = await ex.wait(["2020"], timeout_sec=5.0)
    r = results["2020"]
    assert r["status"] == "DONE"
    assert set(r["tasks"].keys()) == {"1", "2", "3"}
    assert r["tasks"]["2"]["stdout"] == "task 2\n"


# ── Empty-result edge case ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slurm_array_no_per_task_files_reports_empty(tmp_path):
    """If no per-task exitcode files exist (e.g. all tasks killed before
    the inner shell wrote anything), wait() returns a clear empty
    result rather than crashing or returning a misleading DONE."""
    ex = _slurm_executor(tmp_path)
    ex._run = AsyncMock(return_value=_sbatch_ok("3030"))
    await ex.submit([JobSpec(command="run", array="1-5")])
    # No files written. squeue empty → terminal → collect.
    ex._run = AsyncMock(return_value=(0, "", ""))
    results = await ex.wait(["3030"], timeout_sec=5.0)
    r = results["3030"]
    assert r["tasks"] == {}
    assert r["summary"] == {"done": 0, "failed": 0, "unknown": 0}
    assert "no per-task exitcode files" in r["stderr"]


# ── Backend without array support refuses cleanly ─────────────────────────

class _NoArrayBackend(SlurmExecutor):
    """Smoke-test that backends declaring ARRAY_TASK_PLACEHOLDER=None
    refuse array submissions with a structured error."""
    ARRAY_TASK_PLACEHOLDER = None


@pytest.mark.asyncio
async def test_backend_without_array_support_refuses(tmp_path):
    cfg = SlurmConfig(
        poll_interval_sec=0.01, default_timeout_sec=10.0,
        output_dir=tmp_path / "noarray",
        partition=None, account=None, default_resource_spec=None,
        sbatch_bin="sbatch", squeue_bin="squeue", scancel_bin="scancel",
    )
    ex = _NoArrayBackend(cfg=cfg)
    ex._run = AsyncMock()
    results = await ex.submit([JobSpec(command="run", array="1-10")])
    assert results[0]["job_id"] is None
    assert "does not support array jobs" in results[0]["error"]
    ex._run.assert_not_called()
