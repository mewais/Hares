"""Tests for the LSF backend (hares.cluster.lsf).

All tests monkeypatch LsfExecutor._run to avoid requiring a real LSF
scheduler. This lets the full submit/wait/cancel/jobs logic run in CI
without bsub/bjobs/bkill on PATH.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hares.cluster.base import shell_quote
from hares.cluster.lsf import (
    JobSpec,
    LsfConfig,
    LsfExecutor,
    load_lsf_config,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def make_config(tmp_path: Path, **overrides) -> LsfConfig:
    defaults = dict(
        poll_interval_sec=0.01,
        default_timeout_sec=10.0,
        output_dir=tmp_path / "lsf-out",
        queue="test_queue",
        default_resource_spec=None,
        bsub_bin="bsub",
        bjobs_bin="bjobs",
        bkill_bin="bkill",
    )
    defaults.update(overrides)
    return LsfConfig(**defaults)


def make_executor(tmp_path: Path, ceiling: Path | None = None, **cfg_kw) -> LsfExecutor:
    return LsfExecutor(cfg=make_config(tmp_path, **cfg_kw), ceiling=ceiling)


def bsub_ok(job_id: str = "12345678") -> tuple[int, str, str]:
    return 0, f"Job <{job_id}> is submitted to queue <test_queue>.\n", ""


# ── load_lsf_config ───────────────────────────────────────────────────────────

def test_load_lsf_config_defaults(tmp_path):
    # HARES_* env vars wiped by tests/conftest.py — defaults apply.
    cfg = load_lsf_config(session_tmp=tmp_path)
    assert cfg.queue is None
    assert cfg.default_resource_spec is None
    assert cfg.poll_interval_sec == 10.0
    assert cfg.default_timeout_sec == 86400.0
    assert cfg.output_dir == tmp_path
    assert cfg.bsub_bin == "bsub"
    assert cfg.bjobs_bin == "bjobs"
    assert cfg.bkill_bin == "bkill"


def test_load_lsf_config_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HARES_LSF_QUEUE", "gpu")
    monkeypatch.setenv("HARES_LSF_DEFAULT_RESOURCE_SPEC", "rusage[mem=4096]")
    monkeypatch.setenv("HARES_LSF_POLL_INTERVAL_SEC", "5")
    monkeypatch.setenv("HARES_LSF_DEFAULT_TIMEOUT_SEC", "3600")
    monkeypatch.setenv("HARES_LSF_BSUB_BIN", "/opt/lsf/bin/bsub")
    cfg = load_lsf_config(session_tmp=tmp_path)
    assert cfg.queue == "gpu"
    assert cfg.default_resource_spec == "rusage[mem=4096]"
    assert cfg.poll_interval_sec == 5.0
    assert cfg.default_timeout_sec == 3600.0
    assert cfg.bsub_bin == "/opt/lsf/bin/bsub"


# ── shell_quote (shared helper) ──────────────────────────────────────────────

def test_shell_quote_plain():
    assert shell_quote("hello world") == "'hello world'"


def test_shell_quote_with_single_quotes():
    assert shell_quote("it's fine") == "'it'\\''s fine'"


# ── submit ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_single_job(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=bsub_ok("99"))
    results = await ex.submit([JobSpec(command="echo hello")])
    assert len(results) == 1
    r = results[0]
    assert r["job_id"] == "99"
    assert r["status"] == "PEND"
    assert "submitted_at" in r


@pytest.mark.asyncio
async def test_submit_multiple_jobs(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(side_effect=[bsub_ok(jid) for jid in ["11", "22", "33"]])
    results = await ex.submit([
        JobSpec(command="echo 1"),
        JobSpec(command="echo 2"),
        JobSpec(command="echo 3"),
    ])
    assert [r["job_id"] for r in results] == ["11", "22", "33"]


@pytest.mark.asyncio
async def test_submit_empty_command_returns_error(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock()
    results = await ex.submit([JobSpec(command="")])
    assert results[0]["job_id"] is None
    assert "non-empty" in results[0]["error"]
    ex._run.assert_not_called()


@pytest.mark.asyncio
async def test_submit_bsub_failure_returns_error(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=(1, "", "queue not found"))
    results = await ex.submit([JobSpec(command="echo hi")])
    assert results[0]["job_id"] is None
    # Error message uses scheduler-name-prefixed wording from base.
    assert "lsf submit failed" in results[0]["error"]


@pytest.mark.asyncio
async def test_submit_bsub_no_job_id_in_output_returns_error(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=(0, "Unexpected output\n", ""))
    results = await ex.submit([JobSpec(command="echo hi")])
    assert results[0]["job_id"] is None
    assert "not found in output" in results[0]["error"]


@pytest.mark.asyncio
async def test_submit_bad_cwd_outside_ceiling_returns_error(tmp_path):
    ceiling = tmp_path / "project"
    ceiling.mkdir()
    ex = make_executor(tmp_path, ceiling=ceiling)
    ex._run = AsyncMock()
    results = await ex.submit([JobSpec(command="echo hi", cwd="/tmp/outside")])
    assert results[0]["job_id"] is None
    assert "ceiling" in results[0]["error"].lower()
    ex._run.assert_not_called()


@pytest.mark.asyncio
async def test_submit_cwd_within_ceiling_accepted(tmp_path):
    ceiling = tmp_path
    cwd = tmp_path / "subdir"
    cwd.mkdir()
    ex = make_executor(tmp_path, ceiling=ceiling)
    ex._run = AsyncMock(return_value=bsub_ok("42"))
    results = await ex.submit([JobSpec(command="echo hi", cwd=str(cwd))])
    assert results[0]["job_id"] == "42"


@pytest.mark.asyncio
async def test_submit_with_resource_spec_and_queue(tmp_path):
    ex = make_executor(tmp_path, queue="gpu")
    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return bsub_ok("77")

    ex._run = fake_run
    await ex.submit([JobSpec(command="train.py", resource_spec="rusage[ngpus_excl_p=2]")])
    assert "-q" in argv_captured
    assert "gpu" in argv_captured
    assert "-R" in argv_captured
    assert "rusage[ngpus_excl_p=2]" in argv_captured


@pytest.mark.asyncio
async def test_submit_uses_default_resource_spec_when_none_given(tmp_path):
    ex = make_executor(tmp_path, default_resource_spec="rusage[mem=2048]")
    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return bsub_ok("55")

    ex._run = fake_run
    await ex.submit([JobSpec(command="echo hi")])
    assert "-R" in argv_captured
    assert "rusage[mem=2048]" in argv_captured


@pytest.mark.asyncio
async def test_submit_per_job_resource_spec_overrides_default(tmp_path):
    ex = make_executor(tmp_path, default_resource_spec="rusage[mem=2048]")
    argv_captured = []

    async def fake_run(argv):
        argv_captured.extend(argv)
        return bsub_ok("56")

    ex._run = fake_run
    await ex.submit([JobSpec(command="echo hi", resource_spec="rusage[mem=8192]")])
    idx = argv_captured.index("-R")
    assert argv_captured[idx + 1] == "rusage[mem=8192]"


@pytest.mark.asyncio
async def test_submit_mixed_success_and_failure(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(side_effect=[bsub_ok("10"), (1, "", "queue full")])
    results = await ex.submit([JobSpec(command="ok"), JobSpec(command="bad")])
    assert results[0]["job_id"] == "10"
    assert results[1]["job_id"] is None
    assert "lsf submit failed" in results[1]["error"]


# ── jobs ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_jobs_empty_initially(tmp_path):
    ex = make_executor(tmp_path)
    assert await ex.jobs() == []


@pytest.mark.asyncio
async def test_jobs_returns_submitted_jobs(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=bsub_ok("20"))
    await ex.submit([JobSpec(command="echo")])
    jobs = await ex.jobs()
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "20"
    assert jobs[0]["status"] == "PEND"


# ── wait ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wait_unknown_job_returns_error(tmp_path):
    ex = make_executor(tmp_path)
    results = await ex.wait(["999"], timeout_sec=1.0)
    assert results["999"]["status"] == "ERROR"
    assert "not known" in results["999"]["error"]


@pytest.mark.asyncio
async def test_wait_job_done_immediately(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=bsub_ok("30"))
    await ex.submit([JobSpec(command="echo done")])

    rec = ex._jobs["30"]
    Path(rec.stdout_file).write_text("hello output\n")
    Path(rec.stderr_file).write_text("")
    Path(rec.exitcode_file).write_text("0\n")

    ex._run = AsyncMock(return_value=(0, "30 user DONE queue host host\n", ""))
    results = await ex.wait(["30"], timeout_sec=5.0)
    assert results["30"]["status"] == "DONE"
    assert results["30"]["exit_code"] == 0
    assert "hello output" in results["30"]["stdout"]


@pytest.mark.asyncio
async def test_wait_job_transitions_pend_to_run_to_done(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=bsub_ok("40"))
    await ex.submit([JobSpec(command="sleep 1")])

    rec = ex._jobs["40"]
    Path(rec.stdout_file).write_text("result\n")
    Path(rec.stderr_file).write_text("")
    Path(rec.exitcode_file).write_text("0\n")

    poll_responses = [
        (0, "40 user PEND queue host host\n", ""),
        (0, "40 user RUN  queue host host\n", ""),
        (0, "40 user DONE queue host host\n", ""),
    ]
    ex._run = AsyncMock(side_effect=poll_responses)
    results = await ex.wait(["40"], timeout_sec=5.0)
    assert results["40"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_wait_exit_status(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=bsub_ok("50"))
    await ex.submit([JobSpec(command="false")])

    rec = ex._jobs["50"]
    Path(rec.stdout_file).write_text("")
    Path(rec.stderr_file).write_text("error text\n")
    Path(rec.exitcode_file).write_text("1\n")

    ex._run = AsyncMock(return_value=(0, "50 user EXIT queue host host\n", ""))
    results = await ex.wait(["50"], timeout_sec=5.0)
    assert results["50"]["status"] == "EXIT"
    assert results["50"]["exit_code"] == 1
    assert "error text" in results["50"]["stderr"]


@pytest.mark.asyncio
async def test_wait_bjobs_not_found_treated_as_done(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=bsub_ok("60"))
    await ex.submit([JobSpec(command="echo")])

    rec = ex._jobs["60"]
    Path(rec.stdout_file).write_text("output\n")
    Path(rec.stderr_file).write_text("")
    Path(rec.exitcode_file).write_text("0\n")

    ex._run = AsyncMock(return_value=(255, "", "Job <60> is not found"))
    results = await ex.wait(["60"], timeout_sec=5.0)
    assert results["60"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_wait_timeout_returns_timeout_status(tmp_path):
    ex = make_executor(tmp_path, poll_interval_sec=0.01)
    ex._run = AsyncMock(return_value=bsub_ok("70"))
    await ex.submit([JobSpec(command="sleep 999")])

    ex._run = AsyncMock(return_value=(0, "70 user RUN queue host host\n", ""))
    results = await ex.wait(["70"], timeout_sec=0.05)
    assert results["70"]["status"] == "TIMEOUT"
    # Error message references the scheduler-prefixed cancel tool.
    assert "lsf_cancel" in results["70"]["error"]


@pytest.mark.asyncio
async def test_wait_multiple_parallel_jobs(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(side_effect=[bsub_ok("80"), bsub_ok("81")])
    await ex.submit([JobSpec(command="echo 1"), JobSpec(command="echo 2")])

    for jid in ["80", "81"]:
        rec = ex._jobs[jid]
        Path(rec.stdout_file).write_text(f"out{jid}\n")
        Path(rec.stderr_file).write_text("")
        Path(rec.exitcode_file).write_text("0\n")

    def done_response(argv):
        jid = argv[-1]
        return (0, f"{jid} user DONE queue host host\n", "")

    ex._run = AsyncMock(side_effect=done_response)
    results = await ex.wait(["80", "81"], timeout_sec=5.0)
    assert results["80"]["status"] == "DONE"
    assert results["81"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_wait_already_collected_skips_poll(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=bsub_ok("90"))
    await ex.submit([JobSpec(command="echo")])

    rec = ex._jobs["90"]
    Path(rec.stdout_file).write_text("cached\n")
    Path(rec.stderr_file).write_text("")
    Path(rec.exitcode_file).write_text("0\n")

    ex._run = AsyncMock(return_value=(0, "90 user DONE queue host host\n", ""))
    await ex.wait(["90"], timeout_sec=5.0)

    ex._run = AsyncMock()
    results = await ex.wait(["90"], timeout_sec=5.0)
    assert results["90"]["status"] == "DONE"
    ex._run.assert_not_called()


# ── cancel ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_success(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=bsub_ok("100"))
    await ex.submit([JobSpec(command="long job")])

    ex._run = AsyncMock(return_value=(0, "Job <100> is being terminated\n", ""))
    results = await ex.cancel(["100"])
    assert results["100"]["cancelled"] is True
    assert ex._jobs["100"].status == "CANCELED"


@pytest.mark.asyncio
async def test_cancel_bkill_failure(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=bsub_ok("101"))
    await ex.submit([JobSpec(command="long job")])

    ex._run = AsyncMock(return_value=(1, "", "permission denied"))
    results = await ex.cancel(["101"])
    assert results["101"]["cancelled"] is False
    assert "permission denied" in results["101"]["message"]


@pytest.mark.asyncio
async def test_cancel_multiple(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(side_effect=[bsub_ok("110"), bsub_ok("111")])
    await ex.submit([JobSpec(command="j1"), JobSpec(command="j2")])

    ex._run = AsyncMock(return_value=(0, "terminated\n", ""))
    results = await ex.cancel(["110", "111"])
    assert results["110"]["cancelled"] is True
    assert results["111"]["cancelled"] is True


# ── execute_blocking ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_blocking_success(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=bsub_ok("200"))

    original_wait = ex.wait

    async def patched_wait(job_ids, timeout_sec):
        for _, rec in ex._jobs.items():
            Path(rec.stdout_file).write_text("blocking output\n")
            Path(rec.stderr_file).write_text("")
            Path(rec.exitcode_file).write_text("0\n")
        ex._run = AsyncMock(return_value=(0, "200 user DONE queue host host\n", ""))
        return await original_wait(job_ids, timeout_sec)

    ex.wait = patched_wait

    result = await ex.execute_blocking(JobSpec(command="echo blocking"), timeout_sec=5.0)
    assert result["status"] == "DONE"
    assert "blocking output" in result["stdout"]


@pytest.mark.asyncio
async def test_execute_blocking_submit_error(tmp_path):
    ex = make_executor(tmp_path)
    ex._run = AsyncMock(return_value=(1, "", "no such queue"))
    result = await ex.execute_blocking(JobSpec(command="echo"), timeout_sec=5.0)
    assert result["status"] == "ERROR"
    assert "error" in result
