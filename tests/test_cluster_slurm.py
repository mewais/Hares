"""Tests for the SLURM backend (hares.cluster.slurm).

All tests monkeypatch SlurmExecutor._run to avoid requiring a real
SLURM scheduler. Same approach as the LSF tests: simulate
sbatch/squeue/scancel responses without slurm on PATH.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hares.cluster.slurm import (
    JobSpec,
    SlurmConfig,
    SlurmExecutor,
    load_slurm_config,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def make_config(tmp_path: Path, **overrides) -> SlurmConfig:
    defaults = dict(
        poll_interval_sec=0.01,
        default_timeout_sec=10.0,
        output_dir=tmp_path / "slurm-out",
        partition=None,
        account=None,
        default_resource_spec=None,
        sbatch_bin="sbatch",
        squeue_bin="squeue",
        scancel_bin="scancel",
    )
    defaults.update(overrides)
    return SlurmConfig(**defaults)


def make_executor(tmp_path: Path, ceiling: Path | None = None, **cfg_kw) -> SlurmExecutor:
    return SlurmExecutor(cfg=make_config(tmp_path, **cfg_kw), ceiling=ceiling)


def sbatch_ok(job_id: str = "12345") -> tuple[int, str, str]:
    """sbatch --parsable returns just the job_id on stdout."""
    return 0, f"{job_id}\n", ""


def sbatch_ok_federated(job_id: str = "12345", cluster: str = "alpha") -> tuple[int, str, str]:
    """In federation mode --parsable returns 'JOBID;CLUSTER'."""
    return 0, f"{job_id};{cluster}\n", ""


# ── load_slurm_config ─────────────────────────────────────────────────────────

def test_load_slurm_config_defaults(tmp_path, monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("HARES_SLURM_"):
            monkeypatch.delenv(k, raising=False)
    cfg = load_slurm_config(session_tmp=tmp_path)
    assert cfg.partition is None
    assert cfg.account is None
    assert cfg.default_resource_spec is None
    assert cfg.poll_interval_sec == 10.0
    assert cfg.default_timeout_sec == 86400.0
    assert cfg.output_dir == tmp_path
    assert cfg.sbatch_bin == "sbatch"
    assert cfg.squeue_bin == "squeue"
    assert cfg.scancel_bin == "scancel"


def test_load_slurm_config_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HARES_SLURM_PARTITION", "gpu")
    monkeypatch.setenv("HARES_SLURM_ACCOUNT", "research")
    monkeypatch.setenv("HARES_SLURM_DEFAULT_RESOURCE_SPEC", "--mem=4096 --time=01:00:00")
    monkeypatch.setenv("HARES_SLURM_POLL_INTERVAL_SEC", "5")
    monkeypatch.setenv("HARES_SLURM_DEFAULT_TIMEOUT_SEC", "3600")
    monkeypatch.setenv("HARES_SLURM_SBATCH_BIN", "/opt/slurm/bin/sbatch")
    cfg = load_slurm_config(session_tmp=tmp_path)
    assert cfg.partition == "gpu"
    assert cfg.account == "research"
    assert cfg.default_resource_spec == "--mem=4096 --time=01:00:00"
    assert cfg.poll_interval_sec == 5.0
    assert cfg.default_timeout_sec == 3600.0
    assert cfg.sbatch_bin == "/opt/slurm/bin/sbatch"


# ── submit ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_single_job(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("99"))
    results = await ex.submit([JobSpec(command="echo hello")])
    assert len(results) == 1
    r = results[0]
    assert r["job_id"] == "99"
    assert r["status"] == "PEND"


@pytest.mark.asyncio
async def test_submit_parses_federated_jobid(tmp_path):
    """In SLURM federation, --parsable returns JOBID;CLUSTER. We keep
    only the JOBID."""
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok_federated("4242", "alpha"))
    results = await ex.submit([JobSpec(command="echo")])
    assert results[0]["job_id"] == "4242"


@pytest.mark.asyncio
async def test_submit_uses_parsable_flag(tmp_path):
    """Critical: without --parsable, sbatch prints 'Submitted batch job
    12345' which our parser does not handle."""
    ex = make_executor(tmp_path)
    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return sbatch_ok("11")

    ex._run = fake_run
    await ex.submit([JobSpec(command="echo")])
    assert "--parsable" in argv_captured


@pytest.mark.asyncio
async def test_submit_passes_partition_and_account(tmp_path):
    ex = make_executor(tmp_path, partition="gpu", account="research")
    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return sbatch_ok("12")

    ex._run = fake_run
    await ex.submit([JobSpec(command="train.py")])
    assert "--partition" in argv_captured
    assert "gpu" in argv_captured
    assert "--account" in argv_captured
    assert "research" in argv_captured


@pytest.mark.asyncio
async def test_submit_resource_spec_shlex_split(tmp_path):
    """Free-form resource_spec is shlex-split into individual sbatch flags."""
    ex = make_executor(tmp_path)
    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return sbatch_ok("13")

    ex._run = fake_run
    await ex.submit([JobSpec(
        command="train.py",
        resource_spec="--mem=8192 --cpus-per-task=4 --gres=gpu:1",
    )])
    assert "--mem=8192" in argv_captured
    assert "--cpus-per-task=4" in argv_captured
    assert "--gres=gpu:1" in argv_captured


@pytest.mark.asyncio
async def test_submit_per_job_resource_spec_overrides_default(tmp_path):
    ex = make_executor(tmp_path, default_resource_spec="--mem=2048")
    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return sbatch_ok("14")

    ex._run = fake_run
    await ex.submit([JobSpec(command="echo", resource_spec="--mem=8192")])
    assert "--mem=8192" in argv_captured
    assert "--mem=2048" not in argv_captured


@pytest.mark.asyncio
async def test_submit_uses_default_resource_spec_when_none(tmp_path):
    ex = make_executor(tmp_path, default_resource_spec="--mem=2048 --time=00:30:00")
    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return sbatch_ok("15")

    ex._run = fake_run
    await ex.submit([JobSpec(command="echo")])
    assert "--mem=2048" in argv_captured
    assert "--time=00:30:00" in argv_captured


@pytest.mark.asyncio
async def test_submit_chdir_for_cwd(tmp_path):
    """SLURM uses --chdir=PATH (not -cwd like LSF)."""
    cwd = tmp_path
    ex = make_executor(tmp_path)
    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return sbatch_ok("16")

    ex._run = fake_run
    await ex.submit([JobSpec(command="echo", cwd=str(cwd))])
    assert any(a.startswith("--chdir=") for a in argv_captured)


@pytest.mark.asyncio
async def test_submit_suppresses_sbatch_output_files(tmp_path):
    """We capture stdout/stderr ourselves via the inner shell; sbatch's
    own --output / --error files must point at /dev/null to avoid
    leaking slurm-NNNN.out files into the cwd."""
    ex = make_executor(tmp_path)
    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return sbatch_ok("17")

    ex._run = fake_run
    await ex.submit([JobSpec(command="echo")])
    assert "--output=/dev/null" in argv_captured
    assert "--error=/dev/null" in argv_captured


@pytest.mark.asyncio
async def test_submit_empty_command_returns_error(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock()
    results = await ex.submit([JobSpec(command="")])
    assert results[0]["job_id"] is None
    assert "non-empty" in results[0]["error"]
    ex._run.assert_not_called()


@pytest.mark.asyncio
async def test_submit_sbatch_failure_returns_error(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=(1, "", "Invalid partition"))
    results = await ex.submit([JobSpec(command="echo")])
    assert results[0]["job_id"] is None
    assert "slurm submit failed" in results[0]["error"]


@pytest.mark.asyncio
async def test_submit_unparseable_output_returns_error(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=(0, "\n", ""))
    results = await ex.submit([JobSpec(command="echo")])
    assert results[0]["job_id"] is None
    assert "not found in output" in results[0]["error"]


@pytest.mark.asyncio
async def test_submit_bad_cwd_outside_ceiling_returns_error(tmp_path):
    ceiling = tmp_path / "project"
    ceiling.mkdir()
    ex = make_executor(tmp_path, ceiling=ceiling)
    ex._run = AsyncMock()
    results = await ex.submit([JobSpec(command="echo", cwd="/tmp/outside")])
    assert results[0]["job_id"] is None
    assert "ceiling" in results[0]["error"].lower()
    ex._run.assert_not_called()


# ── wait — squeue active states ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wait_squeue_pending_then_running_then_done(tmp_path):
    """Active job states from squeue (long-form %T)."""
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("100"))
    await ex.submit([JobSpec(command="train")])

    rec = ex._jobs["100"]
    Path(rec.stdout_file).write_text("model trained\n")
    Path(rec.stderr_file).write_text("")
    Path(rec.exitcode_file).write_text("0\n")

    poll_responses = [
        (0, "PENDING\n", ""),
        (0, "RUNNING\n", ""),
        # Job left the queue: empty squeue output → executor falls back
        # to exitcode file → DONE because exit_code is 0.
        (0, "", ""),
    ]
    ex._run = AsyncMock(side_effect=poll_responses)
    results = await ex.wait(["100"], timeout_sec=5.0)
    assert results["100"]["status"] == "DONE"
    assert results["100"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_wait_squeue_failed_state(tmp_path):
    """SLURM FAILED state is terminal → maps to canonical EXIT."""
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("101"))
    await ex.submit([JobSpec(command="false")])

    rec = ex._jobs["101"]
    Path(rec.stdout_file).write_text("")
    Path(rec.stderr_file).write_text("crashed\n")
    Path(rec.exitcode_file).write_text("2\n")

    ex._run = AsyncMock(return_value=(0, "FAILED\n", ""))
    results = await ex.wait(["101"], timeout_sec=5.0)
    assert results["101"]["status"] == "EXIT"
    assert results["101"]["exit_code"] == 2


@pytest.mark.asyncio
async def test_wait_squeue_completing_state_treated_as_running(tmp_path):
    """COMPLETING is a transient running-ish state; should not be
    treated as terminal (we'd miss the exitcode file write window)."""
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("102"))
    await ex.submit([JobSpec(command="echo")])

    rec = ex._jobs["102"]
    Path(rec.stdout_file).write_text("done\n")
    Path(rec.stderr_file).write_text("")
    Path(rec.exitcode_file).write_text("0\n")

    poll_responses = [
        (0, "COMPLETING\n", ""),  # not terminal
        (0, "", ""),               # job left the queue → exitcode-file fallback
    ]
    ex._run = AsyncMock(side_effect=poll_responses)
    results = await ex.wait(["102"], timeout_sec=5.0)
    assert results["102"]["status"] == "DONE"


# ── wait — exitcode-file fallback when job leaves the queue ──────────────────

@pytest.mark.asyncio
async def test_wait_squeue_empty_with_zero_exitcode_is_done(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("110"))
    await ex.submit([JobSpec(command="echo")])

    rec = ex._jobs["110"]
    Path(rec.stdout_file).write_text("ok\n")
    Path(rec.stderr_file).write_text("")
    Path(rec.exitcode_file).write_text("0\n")

    ex._run = AsyncMock(return_value=(0, "", ""))
    results = await ex.wait(["110"], timeout_sec=5.0)
    assert results["110"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_wait_squeue_empty_with_nonzero_exitcode_is_exit(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("111"))
    await ex.submit([JobSpec(command="false")])

    rec = ex._jobs["111"]
    Path(rec.stdout_file).write_text("")
    Path(rec.stderr_file).write_text("err\n")
    Path(rec.exitcode_file).write_text("1\n")

    ex._run = AsyncMock(return_value=(0, "", ""))
    results = await ex.wait(["111"], timeout_sec=5.0)
    assert results["111"]["status"] == "EXIT"
    assert results["111"]["exit_code"] == 1


@pytest.mark.asyncio
async def test_wait_squeue_invalid_job_id_falls_back_to_exitcode(tmp_path):
    """Some squeue versions exit non-zero with 'Invalid job id specified'
    once the job has aged out of accounting; the parser must treat that
    the same as empty squeue output."""
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("112"))
    await ex.submit([JobSpec(command="echo")])

    rec = ex._jobs["112"]
    Path(rec.stdout_file).write_text("late\n")
    Path(rec.stderr_file).write_text("")
    Path(rec.exitcode_file).write_text("0\n")

    ex._run = AsyncMock(return_value=(1, "", "slurm_load_jobs error: Invalid job id specified"))
    results = await ex.wait(["112"], timeout_sec=5.0)
    assert results["112"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_wait_squeue_empty_with_missing_exitcode_file_is_unknown(tmp_path):
    """Inner shell never wrote the exitcode file (e.g. job killed by
    SLURM before completion). We can't classify — neither DONE nor EXIT
    is right — so it's UNKWN and the job stays pending until timeout."""
    ex = make_executor(tmp_path, poll_interval_sec=0.01)
    ex._run = AsyncMock(return_value=sbatch_ok("113"))
    await ex.submit([JobSpec(command="kill_me")])

    # No exitcode file written.
    ex._run = AsyncMock(return_value=(0, "", ""))
    results = await ex.wait(["113"], timeout_sec=0.05)
    # UNKWN is non-terminal → wait loop hits timeout.
    assert results["113"]["status"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_wait_timeout_returns_timeout_status(tmp_path):
    ex = make_executor(tmp_path, poll_interval_sec=0.01)
    ex._run = AsyncMock(return_value=sbatch_ok("120"))
    await ex.submit([JobSpec(command="sleep 999")])

    ex._run = AsyncMock(return_value=(0, "RUNNING\n", ""))
    results = await ex.wait(["120"], timeout_sec=0.05)
    assert results["120"]["status"] == "TIMEOUT"
    assert "slurm_cancel" in results["120"]["error"]


# ── jobs ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_jobs_empty_initially(tmp_path):
    ex = make_executor(tmp_path)
    assert await ex.jobs() == []


@pytest.mark.asyncio
async def test_jobs_returns_submitted_jobs(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("130"))
    await ex.submit([JobSpec(command="echo")])
    jobs = await ex.jobs()
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "130"


# ── cancel ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_success(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("140"))
    await ex.submit([JobSpec(command="long")])

    ex._run = AsyncMock(return_value=(0, "", ""))
    results = await ex.cancel(["140"])
    assert results["140"]["cancelled"] is True
    assert ex._jobs["140"].status == "CANCELED"


@pytest.mark.asyncio
async def test_cancel_failure(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("141"))
    await ex.submit([JobSpec(command="long")])

    ex._run = AsyncMock(return_value=(1, "", "Access/permission denied"))
    results = await ex.cancel(["141"])
    assert results["141"]["cancelled"] is False
    assert "permission denied" in results["141"]["message"]


@pytest.mark.asyncio
async def test_cancel_uses_scancel_binary(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("142"))
    await ex.submit([JobSpec(command="long")])

    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return (0, "", "")

    ex._run = fake_run
    await ex.cancel(["142"])
    assert argv_captured[0] == "scancel"
    assert "142" in argv_captured


# ── execute_blocking ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_blocking_success(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=sbatch_ok("200"))

    original_wait = ex.wait

    async def patched_wait(job_ids, timeout_sec):
        for _, rec in ex._jobs.items():
            Path(rec.stdout_file).write_text("blocking output\n")
            Path(rec.stderr_file).write_text("")
            Path(rec.exitcode_file).write_text("0\n")
        ex._run = AsyncMock(return_value=(0, "COMPLETED\n", ""))
        return await original_wait(job_ids, timeout_sec)

    ex.wait = patched_wait
    result = await ex.execute_blocking(JobSpec(command="echo"), timeout_sec=5.0)
    assert result["status"] == "DONE"
    assert "blocking output" in result["stdout"]


@pytest.mark.asyncio
async def test_execute_blocking_submit_error(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=(1, "", "Invalid partition"))
    result = await ex.execute_blocking(JobSpec(command="echo"), timeout_sec=5.0)
    assert result["status"] == "ERROR"
