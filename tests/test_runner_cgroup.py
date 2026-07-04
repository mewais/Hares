"""Cgroup-mode tests for the Runner subprocess executor.

These tests exercise the aggregate-memory bounding added in the cgroup
integration layer (memlimit + runner). Live tests are guarded by
``pytest.mark.skipif(not memlimit.cgroup_memory_available())``.

KEY PROPERTY: the multi-process OOM test asserts that ``os.getpid()``
(the test process itself) is still alive after the memory bomb is
killed — this is the whole point of cgroup bounding. Without cgroup
bounding the kernel's global OOM killer might take out the test process
(and Claude Code / the MCP client) instead.
"""

from __future__ import annotations

import os
import sys

import pytest

from hares import memlimit
from hares.runner import Runner


# Linux-only — Runner itself is Linux-only.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Hares targets Linux",
)

# Shorthand guard for tests that require working cgroup v2 + systemd-run.
_cgroup_ok = memlimit.cgroup_memory_available()
needs_cgroup = pytest.mark.skipif(
    not _cgroup_ok,
    reason="cgroup v2 + systemd-run --user not available on this host",
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _runner_no_cgroup(mem_limit_mb: int = 1024) -> Runner:
    """Runner with cgroup explicitly disabled (use_cgroup=False)."""
    return Runner(
        max_concurrent=1,
        mem_limit_mb=mem_limit_mb,
        cpu_limit_sec=60,
        rss_poll_interval=0.3,
        use_cgroup=False,
    )


def _runner_cgroup(
    mem_limit_mb: int = 512,
    mem_limit_max_mb: int = 1024,
) -> Runner:
    """Runner with cgroup explicitly enabled (use_cgroup=True)."""
    return Runner(
        max_concurrent=1,
        mem_limit_mb=mem_limit_mb,
        cpu_limit_sec=60,
        rss_poll_interval=0.3,
        use_cgroup=True,
        mem_limit_max_mb=mem_limit_max_mb,
    )


# ── Smoke test: use_cgroup=False leaves existing behavior intact ───────────────


async def test_no_cgroup_smoke_echo():
    """use_cgroup=False: a simple command succeeds with memory_mode='rlimit'."""
    r = _runner_no_cgroup()
    result = await r.execute("echo hello")
    assert result["exit_code"] == 0, result
    assert result["stdout"].strip() == "hello"
    assert result["killed_reason"] is None
    assert result["memory_mode"] == "rlimit", result


async def test_no_cgroup_result_fields_present():
    """Diagnostic fields are present regardless of cgroup mode."""
    r = _runner_no_cgroup()
    result = await r.execute("echo hi")
    assert "memory_mode" in result
    assert "aggregate_mem_limit_mb" in result
    assert result["aggregate_mem_limit_mb"] > 0


# ── LIVE: multi-process memory bomb kills only the command, not the test ───────


@needs_cgroup
async def test_oom_multiprocess_kills_command_not_session(tmp_path):
    """Core property: a forked multi-process memory bomb under a small cgroup
    cap is killed with killed_reason='oom', and the test process itself
    remains alive throughout (os.getpid() is accessible after the call).

    This is the proof that cgroup bounding works: without it, the global
    OOM killer might select the test process (or the MCP client) as the
    victim instead.

    Uses 256 MB aggregate cap; the bomb tries to allocate ~400 MB via
    multiple forked children so the aggregate triggers the cgroup OOM killer
    before any single RLIMIT_AS would fire.
    """
    # Write the bomb script to a file so we avoid shell quoting complications.
    bomb_py = tmp_path / "oom_bomb.py"
    bomb_py.write_text(
        "import os, sys, time\n"
        "CHUNK_MB = 80\n"
        "N_CHILDREN = 5\n"  # 5 × 80 MB = 400 MB > 256 MB cap
        "pids = []\n"
        "for _ in range(N_CHILDREN):\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        data = bytearray(CHUNK_MB * 1024 * 1024)\n"
        "        for i in range(0, len(data), 4096):\n"
        "            data[i] = 1\n"
        "        time.sleep(60)\n"
        "        sys.exit(0)\n"
        "    pids.append(pid)\n"
        "time.sleep(60)\n"
        "sys.exit(0)\n"
    )

    # 256 MB aggregate cap — small enough to guarantee OOM for 400 MB total.
    runner = Runner(
        max_concurrent=1,
        mem_limit_mb=256,
        cpu_limit_sec=60,
        rss_poll_interval=0.5,
        use_cgroup=True,
        mem_limit_max_mb=1024,
    )
    cmd = f"{sys.executable} {bomb_py}"

    # Record that WE are alive before the bomb.
    my_pid = os.getpid()
    assert os.getpid() == my_pid, "sanity: can read own PID"

    result = await runner.execute(cmd, timeout=60.0)

    # The command must have been terminated.
    assert result["exit_code"] != 0, (
        f"expected non-zero exit from OOM bomb, got: {result}"
    )

    # The killed reason must be 'oom' (the cgroup OOM killer fired).
    assert result["killed_reason"] == "oom", (
        f"expected killed_reason='oom', got: {result['killed_reason']!r}\n"
        f"full result: {result}"
    )

    # Diagnostic fields must reflect cgroup mode.
    assert result["memory_mode"] == "cgroup", result
    assert result["aggregate_mem_limit_mb"] == 256, result

    # The most important assertion: OUR process is still alive.
    # If this line executes, the test process survived.
    assert os.getpid() == my_pid, (
        "test process was killed during the OOM bomb — cgroup bounding failed!"
    )

    # A human-readable note must be present and mention the cgroup + OOM.
    assert "killed_note" in result, result
    note = result["killed_note"]
    assert "cgroup" in note and "out of memory" in note, note


# ── LIVE: high_memory=True raises the ceiling ──────────────────────────────────


@needs_cgroup
async def test_high_memory_permits_above_normal_cap(tmp_path):
    """high_memory=True allows a command that needs more than the normal
    cgroup cap to succeed, as long as it is within mem_limit_max_mb.

    Setup: mem_limit_mb=128 MB aggregate cap, mem_limit_max_mb=600 MB.
    A multi-process script allocates ~320 MB in aggregate via forked children
    (so no single child hits RLIMIT_AS, which equals the cgroup limit in each
    mode). With high_memory=True the aggregate cap is raised to 600 MB →
    success. With high_memory=False the 128 MB cgroup cap fires → OOM.
    """
    # We use forked children so that the AGGREGATE exceeds the cgroup limit
    # without any single child hitting its own per-process RLIMIT_AS.
    # Each child is well within RLIMIT_AS; the cgroup is the binding constraint.
    alloc_py = tmp_path / "alloc.py"
    alloc_py.write_text(
        "import os, sys, time\n"
        "# 4 children × 60 MB = 240 MB aggregate\n"
        "for _ in range(4):\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        d = bytearray(60 * 1024 * 1024)\n"
        "        for i in range(0, len(d), 4096):\n"
        "            d[i] = 1\n"
        "        time.sleep(20)\n"
        "        sys.exit(0)\n"
        "time.sleep(20)\n"
        "sys.exit(0)\n"
    )

    runner = Runner(
        max_concurrent=1,
        mem_limit_mb=128,       # normal cgroup cap: 128 MB aggregate
        mem_limit_max_mb=600,   # high-memory ceiling: 600 MB
        cpu_limit_sec=60,
        rss_poll_interval=0.3,
        use_cgroup=True,
    )
    cmd = f"{sys.executable} {alloc_py}"

    # With high_memory=True: 240 MB aggregate < 600 MB ceiling → should succeed.
    result_high = await runner.execute(cmd, timeout=30.0, high_memory=True)
    assert result_high["exit_code"] == 0, (
        f"high_memory=True: expected success, got: {result_high}"
    )
    assert result_high["killed_reason"] is None, result_high
    assert result_high["memory_mode"] == "cgroup", result_high
    # The aggregate limit should be at the high-memory ceiling (full
    # mem_limit_max_mb because no per-call mem_limit_mb was specified).
    assert result_high["aggregate_mem_limit_mb"] == 600, result_high

    # Without high_memory (normal mode): 240 MB > 128 MB cap → OOM kill.
    result_normal = await runner.execute(cmd, timeout=30.0, high_memory=False)
    assert result_normal["exit_code"] != 0, (
        f"high_memory=False: expected OOM kill, got: {result_normal}"
    )
    # The cgroup OOM killer fires; killed_reason must be 'oom'.
    assert result_normal["killed_reason"] == "oom", (
        f"expected killed_reason='oom', got: {result_normal['killed_reason']!r}\n"
        f"full result: {result_normal}"
    )


@needs_cgroup
async def test_high_memory_clamped_to_mem_limit_max_mb():
    """Requesting mem_limit_mb above mem_limit_max_mb is clamped to the
    machine-safe ceiling even in high_memory mode."""
    runner = Runner(
        max_concurrent=1,
        mem_limit_mb=128,
        mem_limit_max_mb=512,
        cpu_limit_sec=60,
        use_cgroup=True,
    )
    # Request 2048 MB — must clamp to 512 MB.
    result = await runner.execute("echo clamped", timeout=10.0,
                                  high_memory=True, mem_limit_mb=2048)
    assert result["exit_code"] == 0, result
    # aggregate_mem_limit_mb is clamped to mem_limit_max_mb (512).
    assert result["aggregate_mem_limit_mb"] == 512, (
        f"expected aggregate=512MB (clamped from 2048), got: {result['aggregate_mem_limit_mb']}"
    )


# ── LIVE: timeout in cgroup mode is reported as 'timeout', NOT 'oom' ──────────


@needs_cgroup
async def test_cgroup_timeout_reported_as_timeout_not_oom():
    """A sleep-based command that times out must report killed_reason='timeout',
    not 'oom'. The timeout kill uses cgroup_kill + killpg; the OOM monitor
    must not mis-attribute this."""
    runner = _runner_cgroup(mem_limit_mb=512, mem_limit_max_mb=1024)
    result = await runner.execute("sleep 60", timeout=2.0)
    assert result["killed_reason"] == "timeout", (
        f"expected 'timeout', got: {result['killed_reason']!r}\n"
        f"full result: {result}"
    )
    assert result["exit_code"] != 0, result
    assert result["memory_mode"] == "cgroup", result


# ── LIVE: per-process RLIMIT_AS still applies through systemd-run wrapper ──────


@needs_cgroup
async def test_rlimit_applies_through_cgroup_wrapper():
    """RLIMIT_AS set in the preexec_fn must be visible inside the cgroup-wrapped
    subprocess. We read back resource.getrlimit(RLIMIT_AS) from the child and
    assert it matches the Runner's configured cap (defense-in-depth).

    The async retry loop in _resolve_scope_dir handles the race window between
    systemd-run spawning and the cgroup directory appearing in sysfs, so no
    manual sleep is needed in the command itself.
    """
    mem_limit_mb = 512
    runner = Runner(
        max_concurrent=1,
        mem_limit_mb=mem_limit_mb,
        cpu_limit_sec=60,
        rss_poll_interval=0.3,
        use_cgroup=True,
        mem_limit_max_mb=1024,
    )
    cmd = (
        f"{sys.executable} -c "
        "\"import resource; "
        "print(resource.getrlimit(resource.RLIMIT_AS))\""
    )
    result = await runner.execute(cmd, timeout=15.0)
    assert result["exit_code"] == 0, (
        f"expected RLIMIT read to succeed, got: {result}"
    )
    assert result["memory_mode"] == "cgroup", result

    out = result["stdout"].strip().splitlines()[-1]
    soft, _hard = eval(out)  # safe: only digits + brackets + commas
    # Soft limit must be the Runner's mem_limit_mb (or lower if the inherited
    # hard limit is smaller).
    assert soft <= mem_limit_mb * 1024 * 1024, (
        f"expected RLIMIT_AS soft <= {mem_limit_mb}MB, got soft={soft // 1024 // 1024}MB"
    )
    assert soft > 0, "RLIMIT_AS must be positive"


# ── LIVE: memory_mode field reflects actual cgroup usage ───────────────────────


@needs_cgroup
async def test_memory_mode_cgroup_for_normal_command():
    """A command under a cgroup-enabled Runner reports memory_mode='cgroup'
    and aggregate_mem_limit_mb > 0.

    memory_mode is now derived from whether the command was WRAPPED in a
    systemd-run scope (scope_name is not None), NOT from whether the cgroup
    directory was successfully resolved for monitoring.  This makes the field
    deterministic: wrapping is decided before the subprocess is spawned, so
    there is no race with cgroup directory visibility in sysfs.
    """
    runner = _runner_cgroup()
    result = await runner.execute("echo alive", timeout=10.0)
    assert result["exit_code"] == 0, result
    assert result["memory_mode"] == "cgroup", result
    assert result["aggregate_mem_limit_mb"] > 0, result
    assert result["killed_reason"] is None, result


@needs_cgroup
async def test_memory_mode_cgroup_fast_command_no_race():
    """Regression test: even an ultra-fast command (echo) must report
    memory_mode='cgroup' under a cgroup-enabled Runner, regardless of
    whether the cgroup directory was resolvable before the command exited.

    Previously, memory_mode was derived from scope_dir resolution (best-effort,
    racy for fast commands).  The fix derives it from wrapped_in_cgroup
    (scope_name is not None), which is set BEFORE spawn and is always
    deterministic.  We run the command many times in a tight loop to guard
    against any residual non-determinism.
    """
    runner = _runner_cgroup()
    for i in range(20):
        result = await runner.execute("echo alive", timeout=10.0)
        assert result["memory_mode"] == "cgroup", (
            f"iteration {i}: expected memory_mode='cgroup', got "
            f"{result['memory_mode']!r}\nfull result: {result}"
        )
        assert result["exit_code"] == 0, f"iteration {i}: {result}"
        assert result["killed_reason"] is None, f"iteration {i}: {result}"
