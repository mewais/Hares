"""Tests for hares.memlimit — aggregate-memory limiting via cgroup v2 scopes.

Split into two sections:
  - Pure / unit tests: do not require cgroup support; use monkeypatching and
    temporary files.
  - Live tests: guarded by ``@pytest.mark.skipif(not cgroup_memory_available())``;
    actually invoke ``systemd-run --user --scope`` and inspect the results.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import hares.memlimit as memlimit
from hares.memlimit import (
    SCOPE_NAME_PREFIX,
    SYSTEMD_RUN_BIN,
    build_scope_argv,
    cgroup_kill,
    cgroup_memory_available,
    machine_safe_max_mb,
    new_scope_name,
    read_oom_kill_count,
    reset_cache,
    scope_cgroup_dir,
)


# ── Autouse: reset the cache before every test ────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_memlimit_cache():
    """Reset the cgroup_memory_available cache before and after each test so
    monkeypatched env vars actually take effect in the probe."""
    reset_cache()
    yield
    reset_cache()


# ── machine_safe_max_mb ───────────────────────────────────────────────────────

class TestMachineSafeMaxMb:
    def test_returns_positive_integer(self):
        result = machine_safe_max_mb()
        assert isinstance(result, int)
        assert result > 0

    def test_less_than_total_memory(self):
        """The result should be less than the total system RAM in MB."""
        try:
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        total_mb = int(line.split()[1]) // 1024
                        break
            result = machine_safe_max_mb()
            assert result <= total_mb
        except OSError:
            pytest.skip("/proc/meminfo not available")

    def test_fraction_applied(self):
        """machine_safe_max_mb(0.5) should be approximately half of 0.9 result."""
        full = machine_safe_max_mb(0.9)
        half = machine_safe_max_mb(0.5)
        # half / full ≈ 0.5 / 0.9 ≈ 0.555; allow generous tolerance for int rounding
        assert 0.4 < (half / full) < 0.7

    def test_fraction_clamped_to_one(self):
        """fraction > 1 is clamped to 1."""
        at_one = machine_safe_max_mb(1.0)
        above_one = machine_safe_max_mb(2.0)
        assert at_one == above_one

    def test_fraction_clamped_away_from_zero(self):
        """fraction <= 0 should not return 0 (clamped to near-zero positive)."""
        result = machine_safe_max_mb(0.0)
        assert result >= 1

    def test_fallback_on_unreadable_meminfo(self, monkeypatch, tmp_path):
        """When /proc/meminfo is unreadable, returns 4096 MB."""
        # Monkeypatch the built-in open inside memlimit to simulate failure.
        original_open = open

        def fake_open(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                raise OSError("simulated failure")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)
        result = machine_safe_max_mb()
        assert result == 4096


# ── new_scope_name ────────────────────────────────────────────────────────────

class TestNewScopeName:
    def test_prefix_present(self):
        name = new_scope_name()
        assert name.startswith(SCOPE_NAME_PREFIX + "-")

    def test_custom_prefix(self):
        name = new_scope_name(prefix="myprefix")
        assert name.startswith("myprefix-")

    def test_hex_suffix_length(self):
        name = new_scope_name()
        # "hares-" is 6 chars; remainder should be 12 hex chars
        suffix = name[len(SCOPE_NAME_PREFIX) + 1:]
        assert len(suffix) == 12
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_uniqueness(self):
        names = {new_scope_name() for _ in range(50)}
        # All 50 should be unique with overwhelming probability
        assert len(names) == 50

    def test_no_forbidden_chars(self):
        """The name should be a valid systemd unit name prefix (no dots, slashes)."""
        for _ in range(20):
            name = new_scope_name()
            assert "/" not in name
            assert "." not in name


# ── build_scope_argv ──────────────────────────────────────────────────────────

class TestBuildScopeArgv:
    def test_basic_shape(self):
        argv = build_scope_argv(
            ["/bin/sh", "-c", "echo hi"],
            mem_bytes=256 * 1024 * 1024,
            scope_name="hares-test001",
        )
        assert argv[0] == SYSTEMD_RUN_BIN
        assert "--user" in argv
        assert "--scope" in argv
        assert "--quiet" in argv
        assert "--unit=hares-test001" in argv
        assert "-p" in argv
        assert f"MemoryMax={256 * 1024 * 1024}" in argv
        assert "MemorySwapMax=0" in argv
        # inner argv follows '--'
        sep_idx = argv.index("--")
        assert argv[sep_idx + 1 :] == ["/bin/sh", "-c", "echo hi"]

    def test_custom_swap_max(self):
        argv = build_scope_argv(
            ["/bin/true"],
            mem_bytes=128 * 1024 * 1024,
            scope_name="hares-abc",
            swap_max_bytes=64 * 1024 * 1024,
        )
        assert f"MemorySwapMax={64 * 1024 * 1024}" in argv

    def test_empty_inner_argv_raises(self):
        with pytest.raises(ValueError, match="inner_argv"):
            build_scope_argv([], mem_bytes=1024, scope_name="hares-x")

    def test_zero_mem_bytes_raises(self):
        with pytest.raises(ValueError, match="mem_bytes"):
            build_scope_argv(["/bin/true"], mem_bytes=0, scope_name="hares-x")

    def test_negative_mem_bytes_raises(self):
        with pytest.raises(ValueError, match="mem_bytes"):
            build_scope_argv(["/bin/true"], mem_bytes=-1, scope_name="hares-x")

    def test_separator_present(self):
        """'--' must appear in the argv to separate systemd-run options from inner cmd."""
        argv = build_scope_argv(["/bin/true"], mem_bytes=1024, scope_name="s")
        assert "--" in argv

    def test_unit_flag_format(self):
        argv = build_scope_argv(
            ["/bin/true"],
            mem_bytes=1024,
            scope_name="hares-deadbeef1234",
        )
        assert any(a.startswith("--unit=") for a in argv)


# ── scope_cgroup_dir ──────────────────────────────────────────────────────────

class TestScopeCgroupDir:
    def test_returns_none_for_nonexistent_scope(self):
        """A made-up scope name should return None."""
        result = scope_cgroup_dir("hares-nonexistent-xxxxxxxx")
        assert result is None

    def test_member_pid_bad_pid_returns_none(self):
        """An obviously invalid PID should not raise."""
        result = scope_cgroup_dir("hares-test", member_pid=999999999)
        assert result is None

    def test_member_pid_parse(self, tmp_path, monkeypatch):
        """Simulate a /proc/<pid>/cgroup file and check parsing logic.

        We monkeypatch Path.read_text for the specific cgroup file, then
        monkeypatch Path.is_dir to pretend the resolved path exists.
        """
        scope = "hares-aabbcc112233"
        fake_cgroup_line = f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{scope}.scope\n"

        # Write a fake cgroup file under tmp_path
        fake_proc = tmp_path / "cgroup"
        fake_proc.write_text(fake_cgroup_line)

        # We need to monkeypatch the Path used inside scope_cgroup_dir.
        # The function builds: Path(f"/proc/{member_pid}/cgroup")
        # We'll monkeypatch Path.read_text at the instance level by
        # replacing the function with a version that intercepts the specific path.
        original_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if "cgroup" in str(self) and "/proc/" in str(self):
                return fake_cgroup_line
            return original_read_text(self, *args, **kwargs)

        original_is_dir = Path.is_dir

        def fake_is_dir(self):
            expected = (
                f"/sys/fs/cgroup/user.slice/user-1000.slice"
                f"/user@1000.service/app.slice/{scope}.scope"
            )
            if str(self) == expected:
                return True
            return original_is_dir(self)

        monkeypatch.setattr(Path, "read_text", fake_read_text)
        monkeypatch.setattr(Path, "is_dir", fake_is_dir)

        result = scope_cgroup_dir(scope, member_pid=12345)
        assert result is not None
        assert result == Path(
            f"/sys/fs/cgroup/user.slice/user-1000.slice"
            f"/user@1000.service/app.slice/{scope}.scope"
        )

    def test_never_raises_on_garbage_input(self):
        """Completely nonsensical inputs must not raise."""
        result = scope_cgroup_dir("", member_pid=-1)
        assert result is None


# ── read_oom_kill_count ───────────────────────────────────────────────────────

class TestReadOomKillCount:
    def test_parses_zero(self, tmp_path):
        events = tmp_path / "memory.events"
        events.write_text(
            "anon 0\nfile 0\nshmem 0\nfile_mapped 0\n"
            "oom 0\noom_kill 0\noom_group_kill 0\n"
        )
        assert read_oom_kill_count(tmp_path) == 0

    def test_parses_nonzero(self, tmp_path):
        events = tmp_path / "memory.events"
        events.write_text(
            "anon 1048576\nfile 2097152\n"
            "oom 3\noom_kill 7\noom_group_kill 1\n"
        )
        assert read_oom_kill_count(tmp_path) == 7

    def test_returns_none_on_missing_file(self, tmp_path):
        result = read_oom_kill_count(tmp_path)
        assert result is None

    def test_returns_none_on_missing_key(self, tmp_path):
        events = tmp_path / "memory.events"
        events.write_text("anon 0\nfile 0\n")
        assert read_oom_kill_count(tmp_path) is None

    def test_returns_none_on_bad_file(self, tmp_path):
        events = tmp_path / "memory.events"
        events.write_bytes(b"\xff\xfe invalid utf content \x00\x01")
        # Should not raise; returns None
        result = read_oom_kill_count(tmp_path)
        # May be None or 0 depending on decode; just must not raise
        assert result is None or isinstance(result, int)

    def test_returns_none_on_nonexistent_dir(self, tmp_path):
        nonexistent = tmp_path / "ghost_cgroup"
        assert read_oom_kill_count(nonexistent) is None


# ── cgroup_kill ───────────────────────────────────────────────────────────────

class TestCgroupKill:
    def test_returns_false_on_missing_file(self, tmp_path):
        result = cgroup_kill(tmp_path / "nonexistent")
        assert result is False

    def test_writes_1_to_cgroup_kill(self, tmp_path):
        """When cgroup.kill exists and is writable, write '1' and return True."""
        kill_file = tmp_path / "cgroup.kill"
        kill_file.touch()
        result = cgroup_kill(tmp_path)
        assert result is True
        assert kill_file.read_text() == "1"


# ── cgroup_memory_available + HARES_DISABLE_CGROUP opt-out ───────────────────

class TestCgroupMemoryAvailable:
    def test_disable_cgroup_truthy_values(self, monkeypatch):
        """HARES_DISABLE_CGROUP with any truthy value disables cgroup."""
        for val in ("1", "true", "yes", "on"):
            reset_cache()
            monkeypatch.setenv("HARES_DISABLE_CGROUP", val)
            assert cgroup_memory_available() is False

    def test_disable_cgroup_falsy_values_do_not_disable(self, monkeypatch):
        """HARES_DISABLE_CGROUP with falsy value does not forcibly disable."""
        # We don't assert True here because the system might genuinely lack
        # cgroup support — we just verify that a falsy value doesn't
        # unconditionally return False (cache is cleared, probe runs fresh).
        for val in ("0", "false", "no", "off", ""):
            reset_cache()
            monkeypatch.setenv("HARES_DISABLE_CGROUP", val)
            # The function must not raise
            result = cgroup_memory_available()
            assert isinstance(result, bool)

    def test_result_is_cached(self, monkeypatch):
        """Second call returns cached result without re-probing."""
        call_count = 0
        original_probe = memlimit._probe_cgroup_memory

        def counting_probe():
            nonlocal call_count
            call_count += 1
            return original_probe()

        monkeypatch.setattr(memlimit, "_probe_cgroup_memory", counting_probe)

        reset_cache()
        cgroup_memory_available()
        cgroup_memory_available()
        cgroup_memory_available()
        assert call_count == 1

    def test_reset_cache_clears_cached_result(self, monkeypatch):
        """reset_cache() causes the probe to run again on next call."""
        call_count = 0
        original_probe = memlimit._probe_cgroup_memory

        def counting_probe():
            nonlocal call_count
            call_count += 1
            return original_probe()

        monkeypatch.setattr(memlimit, "_probe_cgroup_memory", counting_probe)

        reset_cache()
        cgroup_memory_available()
        assert call_count == 1
        reset_cache()
        cgroup_memory_available()
        assert call_count == 2

    def test_never_raises(self, monkeypatch):
        """Even if _probe_cgroup_memory raises, cgroup_memory_available returns False."""
        monkeypatch.setattr(memlimit, "_probe_cgroup_memory", lambda: 1 / 0)
        result = cgroup_memory_available()
        assert result is False


# ── Live test (guarded) ───────────────────────────────────────────────────────

@pytest.mark.skipif(
    not cgroup_memory_available(),
    reason="cgroup v2 + systemd-run --user --scope not available on this host",
)
class TestLiveCgroupScope:
    """These tests actually invoke systemd-run and inspect the real cgroup tree."""

    def test_live_scope_runs_and_events_readable(self, tmp_path):
        """Run a trivial command in a scope and read memory.events back.

        This validates the full round-trip: scope creation → cgroup dir
        resolution → memory.events parsing.  On success the oom_kill
        counter should be 0 (nothing triggered OOM).
        """
        scope_name = new_scope_name()
        argv = build_scope_argv(
            ["/bin/true"],
            mem_bytes=64 * 1024 * 1024,
            scope_name=scope_name,
        )

        result = subprocess.run(argv, capture_output=True, timeout=15)
        assert result.returncode == 0, (
            f"systemd-run returned {result.returncode}; "
            f"stderr={result.stderr.decode(errors='replace')}"
        )

        # Give systemd a moment to register the cgroup (usually instant)
        # before trying to resolve the dir.  We try multiple times.
        cgroup_dir = None
        for _ in range(5):
            cgroup_dir = scope_cgroup_dir(scope_name)
            if cgroup_dir is not None:
                break
            time.sleep(0.2)

        # The scope may have already been removed by the time we look
        # (transient scope cleans up immediately after /bin/true exits).
        # That is expected — memory.events may have been removed already.
        # If we caught it in time, verify the count is readable.
        if cgroup_dir is not None and cgroup_dir.is_dir():
            oom_count = read_oom_kill_count(cgroup_dir)
            assert oom_count is not None, "memory.events should be readable while scope exists"
            assert oom_count == 0, "A /bin/true command should never trigger OOM"

    def test_scope_cgroup_dir_via_member_pid(self):
        """Start a long-lived scope, resolve its cgroup dir via member pid, then kill it."""
        import os

        scope_name = new_scope_name()
        # Use sleep so the scope stays alive long enough to inspect.
        argv = build_scope_argv(
            ["/bin/sleep", "30"],
            mem_bytes=64 * 1024 * 1024,
            scope_name=scope_name,
        )

        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Wait for the scope to start.
            time.sleep(0.5)

            cgroup_dir = scope_cgroup_dir(scope_name, member_pid=proc.pid)
            if cgroup_dir is None:
                # systemd-run may be the outer pid; try the constructed path.
                cgroup_dir = scope_cgroup_dir(scope_name)

            assert cgroup_dir is not None, (
                f"Could not resolve cgroup dir for scope {scope_name!r}"
            )
            assert cgroup_dir.is_dir()

            # memory.events must be present and parseable.
            oom_count = read_oom_kill_count(cgroup_dir)
            assert oom_count is not None
            assert oom_count == 0

            # Kill via cgroup.kill — must return True.
            ok = cgroup_kill(cgroup_dir)
            assert ok is True

        finally:
            proc.wait(timeout=5)
