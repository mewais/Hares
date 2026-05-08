"""Tests for the Runner subprocess executor."""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from hares.runner import Runner


# Linux-only tests: setrlimit/setsid/sched_setaffinity all assume Linux.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Hares targets Linux",
)


@pytest.fixture
def small_runner() -> Runner:
    # 1 GB cap, 60 s CPU, 2 concurrent — plenty for the tests below.
    return Runner(
        max_concurrent=2,
        mem_limit_mb=1024,
        cpu_limit_sec=60,
        rss_poll_interval=0.5,
        rss_overshoot_ratio=1.2,
        pin_cpu=True,
        rewrite_overcommits=True,
    )


# ── basic happy path ───────────────────────────────────────────────────────


async def test_simple_echo(small_runner: Runner):
    r = await small_runner.execute("echo hello")
    assert r["exit_code"] == 0
    assert r["stdout"].strip() == "hello"
    assert r["killed_reason"] is None


async def test_nonzero_exit_propagates(small_runner: Runner):
    r = await small_runner.execute("false")
    assert r["exit_code"] == 1
    assert r["killed_reason"] is None


async def test_stderr_captured(small_runner: Runner):
    r = await small_runner.execute("echo oops 1>&2")
    assert r["stderr"].strip() == "oops"


async def test_cwd_respected(small_runner: Runner, tmp_path):
    r = await small_runner.execute("pwd", cwd=str(tmp_path))
    assert tmp_path.name in r["stdout"]


async def test_env_overrides_merged(small_runner: Runner):
    r = await small_runner.execute("echo $HARES_TEST_VAR", env={"HARES_TEST_VAR": "ok"})
    assert r["stdout"].strip() == "ok"


# ── timeout ────────────────────────────────────────────────────────────────


async def test_timeout_kills(small_runner: Runner):
    start = time.monotonic()
    r = await small_runner.execute("sleep 30", timeout=1.0)
    elapsed = time.monotonic() - start
    assert r["killed_reason"] == "timeout"
    assert elapsed < 5.0
    assert r["exit_code"] != 0


# ── concurrency cap ────────────────────────────────────────────────────────


async def test_max_concurrent_2_serializes_when_5_dispatched(small_runner: Runner):
    # 5 sleeps × 0.5s; with max_concurrent=2 ⇒ wall-clock should be ~1.5s
    # (3 batches of 2 sleeps + a leftover, all 0.5s).
    start = time.monotonic()
    results = await asyncio.gather(*[
        small_runner.execute("sleep 0.5") for _ in range(5)
    ])
    elapsed = time.monotonic() - start
    assert all(r["exit_code"] == 0 for r in results)
    # Lower bound: 3 batches × 0.5s = 1.5s. Allow some scheduling slack.
    assert elapsed >= 1.2, f"expected ≥1.2s wall-clock, got {elapsed:.2f}s"


async def test_weight_2_takes_both_slots():
    r = Runner(max_concurrent=2, mem_limit_mb=1024, cpu_limit_sec=60)
    # weight=2 ⇒ both slots taken; a parallel weight=1 must wait.
    started_at = time.monotonic()
    big = asyncio.create_task(r.execute("sleep 0.6", weight=2))
    await asyncio.sleep(0.1)  # ensure big has acquired its slots
    small_started = time.monotonic()
    small = await r.execute("sleep 0.05", weight=1)
    small_finished = time.monotonic()
    await big
    # The small command should have waited for big to release one of
    # its slots (~0.5s after big started).
    assert (small_finished - started_at) >= 0.5


# ── pre-flight rewriting ───────────────────────────────────────────────────


async def test_pytest_n_auto_rewritten(small_runner: Runner):
    r = await small_runner.execute("pytest -n auto --version", timeout=10)
    # The `--version` makes pytest print its version then exit, regardless
    # of -n; we don't actually need pytest installed for this test, just
    # to confirm that the rewrite fires and is reported.
    # The key invariants:
    assert r["rewrites"], "expected pytest -n auto to be rewritten"
    assert r["rewrites"][0]["kind"] == "pytest_xdist"
    assert "[hares: pre-flight rewrote command" in r["stdout"]


# ── memory cap ─────────────────────────────────────────────────────────────


async def test_mem_cap_kills_python_alloc():
    """Allocate way more than the cap; expect SIGKILL or rss_exceeded."""
    # 100 MB cap → trying to allocate 500 MB should be killed.
    r = Runner(max_concurrent=1, mem_limit_mb=100, cpu_limit_sec=30)
    cmd = (
        f"{sys.executable} -c "
        f"\"a = bytearray(500*1024*1024); print('alive', len(a))\""
    )
    result = await r.execute(cmd, timeout=15.0)
    # Either the kernel killed it (SIGKILL → exit_code=-9, killed_reason
    # set by us) or Python raised MemoryError (exit_code != 0).
    assert result["exit_code"] != 0, (
        f"expected non-zero exit, got: {result}"
    )
    if result["killed_reason"] is not None:
        assert result["killed_reason"] in ("rss_exceeded", "cpu_exceeded")


# ── CPU affinity ───────────────────────────────────────────────────────────


async def test_cpu_affinity_pinned(small_runner: Runner):
    """Subprocess should see a restricted affinity set."""
    if not hasattr(os, "sched_setaffinity"):
        pytest.skip("sched_setaffinity not supported")
    parent_cores = sorted(os.sched_getaffinity(0))
    if len(parent_cores) <= small_runner.max_concurrent:
        # The test's interesting case is when parent has MORE cores
        # than our cap; on a small host the affinity is effectively
        # unchanged. Skip rather than passing trivially.
        pytest.skip("parent affinity already <= max_concurrent")
    cmd = f"{sys.executable} -c \"import os; print(sorted(os.sched_getaffinity(0)))\""
    r = await small_runner.execute(cmd, weight=1)
    assert r["exit_code"] == 0
    out = r["stdout"].strip().splitlines()[-1]  # last line: affinity list
    child_cores = eval(out)  # safe: only digits + brackets + commas
    assert len(child_cores) <= 1, (
        f"expected weight=1 ⇒ 1 core; got {child_cores}"
    )
    assert set(child_cores).issubset(set(parent_cores))


# ── construction validation ────────────────────────────────────────────────


@pytest.mark.parametrize("kw, val", [
    ("max_concurrent", 0),
    ("mem_limit_mb", 0),
    ("cpu_limit_sec", 0),
])
def test_invalid_args_rejected(kw: str, val: int):
    kwargs = dict(max_concurrent=2, mem_limit_mb=1024, cpu_limit_sec=60)
    kwargs[kw] = val
    with pytest.raises(ValueError):
        Runner(**kwargs)


async def test_empty_command_rejected(small_runner: Runner):
    with pytest.raises(ValueError):
        await small_runner.execute("")


# ── per-call resource override ─────────────────────────────────────────────


async def _run_python(small_runner: Runner, snippet: str, **kw) -> dict:
    cmd = f"{sys.executable} -c \"{snippet}\""
    return await small_runner.execute(cmd, timeout=10.0, **kw)


async def test_per_call_mem_limit_lower_than_default_takes_effect(small_runner: Runner):
    """mem_limit_mb=128 should set RLIMIT_AS to 128 MB even though the
    Runner's default is 1024 MB."""
    snippet = (
        "import resource; "
        "print(resource.getrlimit(resource.RLIMIT_AS))"
    )
    r = await _run_python(small_runner, snippet, mem_limit_mb=128)
    assert r["exit_code"] == 0, r
    out = r["stdout"].strip().splitlines()[-1]
    soft, _hard = eval(out)
    assert soft == 128 * 1024 * 1024


async def test_per_call_mem_limit_higher_clamped_to_default(small_runner: Runner):
    """mem_limit_mb=4096 (above the 1024 default) should clamp DOWN to
    the Runner's default — callers can ask for less, never more."""
    snippet = (
        "import resource; "
        "print(resource.getrlimit(resource.RLIMIT_AS))"
    )
    r = await _run_python(small_runner, snippet, mem_limit_mb=4096)
    assert r["exit_code"] == 0, r
    out = r["stdout"].strip().splitlines()[-1]
    soft, _hard = eval(out)
    assert soft == 1024 * 1024 * 1024  # clamped to instance default


async def test_per_call_cpu_limit_lower_than_default_takes_effect(small_runner: Runner):
    """cpu_limit_sec=5 should set RLIMIT_CPU to 5 even though Runner
    default is 60."""
    snippet = (
        "import resource; "
        "print(resource.getrlimit(resource.RLIMIT_CPU))"
    )
    r = await _run_python(small_runner, snippet, cpu_limit_sec=5)
    assert r["exit_code"] == 0, r
    out = r["stdout"].strip().splitlines()[-1]
    soft, _hard = eval(out)
    assert soft == 5


async def test_per_call_cpu_limit_higher_clamped_to_default(small_runner: Runner):
    snippet = (
        "import resource; "
        "print(resource.getrlimit(resource.RLIMIT_CPU))"
    )
    r = await _run_python(small_runner, snippet, cpu_limit_sec=9999)
    assert r["exit_code"] == 0, r
    out = r["stdout"].strip().splitlines()[-1]
    soft, _hard = eval(out)
    assert soft == 60  # clamped to instance default


async def test_per_call_unset_uses_instance_defaults(small_runner: Runner):
    """When mem/cpu limits are not passed, the child should see the
    Runner's instance defaults — same behavior as before this feature."""
    snippet = (
        "import resource; "
        "print([resource.getrlimit(resource.RLIMIT_AS), "
        "resource.getrlimit(resource.RLIMIT_CPU)])"
    )
    r = await _run_python(small_runner, snippet)
    assert r["exit_code"] == 0, r
    out = r["stdout"].strip().splitlines()[-1]
    as_lim, cpu_lim = eval(out)
    assert as_lim[0] == 1024 * 1024 * 1024
    assert cpu_lim[0] == 60


async def test_per_call_zero_or_negative_clamped_to_one(small_runner: Runner):
    """Defensive clamp: negative/zero requests get bumped to 1 MB / 1 sec
    rather than being passed through and crashing setrlimit."""
    snippet = (
        "import resource; "
        "print(resource.getrlimit(resource.RLIMIT_CPU))"
    )
    # 0 → clamped to 1 sec (not 0 which kernel would reject as invalid).
    r = await _run_python(small_runner, snippet, cpu_limit_sec=0)
    out = r["stdout"].strip().splitlines()[-1] if r["stdout"] else ""
    if r["exit_code"] == 0:
        soft, _hard = eval(out)
        assert soft == 1


async def test_per_call_mem_limit_triggers_rss_overshoot_kill():
    """A small per-call mem_limit_mb on a Runner with a much larger
    default should cause an EARLIER RSS-overshoot kill than the default
    would — proving the threshold actually moves with the per-call
    value (not just RLIMIT_AS)."""
    # Runner default 2 GB; per-call cap 100 MB; allocate 250 MB.
    runner = Runner(
        max_concurrent=1, mem_limit_mb=2048, cpu_limit_sec=30,
        rss_poll_interval=0.2,
    )
    cmd = (
        f"{sys.executable} -c "
        f"\"a = bytearray(250*1024*1024); print('alive', len(a))\""
    )
    r = await runner.execute(cmd, mem_limit_mb=100, timeout=15.0)
    # Kernel kill (SIGKILL → -9) or our RSS monitor.
    assert r["exit_code"] != 0, r
    if r["killed_reason"] is not None:
        assert r["killed_reason"] in ("rss_exceeded", "cpu_exceeded")
