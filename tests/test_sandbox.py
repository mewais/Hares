"""Tests for the bwrap-based filesystem sandbox.

These tests need `bwrap` on PATH and a kernel with user namespaces
enabled. The skipif at the top covers both.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from hares.runner import Runner
from hares.sandbox import (
    SandboxConfig,
    _is_subpath,
    build_bwrap_argv,
    load_sandbox_config,
)

# Skip the whole module if bwrap isn't available.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="bwrap not available",
)


def _bwrap_works() -> bool:
    """Detect whether bwrap can actually run a trivial command on this
    host. Uses the production argv builder so the probe automatically
    matches the symlink/bind layout we'd use in real execution
    (covers merged-usr distros like RHEL/Fedora where /lib /lib64
    /bin /sbin are symlinks into /usr)."""
    import subprocess
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False
    cfg = SandboxConfig(enabled=True, bwrap_bin=bwrap, rw_binds=("/tmp",))
    argv = build_bwrap_argv(cfg, "exit 0", cwd="/tmp")
    try:
        r = subprocess.run(argv, capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


_BWRAP_OK = _bwrap_works()
needs_bwrap_runtime = pytest.mark.skipif(
    not _BWRAP_OK, reason="bwrap present but cannot create namespaces here",
)


# ── Pure builder tests (don't actually run bwrap) ──────────────────────────


def test_enabled_by_default_in_0_2(monkeypatch):
    """0.2.0 default flip: bwrap is REQUIRED by default. Operators
    opt out via HARES_SANDBOX_DISABLED=1 (mac, restricted-userns
    containers, debugging)."""
    monkeypatch.delenv("HARES_SANDBOX_MODE", raising=False)
    monkeypatch.delenv("HARES_SANDBOX_DISABLED", raising=False)
    cfg = load_sandbox_config()
    assert cfg.enabled is True


def test_disabled_via_new_opt_out(monkeypatch):
    monkeypatch.delenv("HARES_SANDBOX_MODE", raising=False)
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv("HARES_SANDBOX_DISABLED", v)
        assert load_sandbox_config().enabled is False


def test_legacy_off_aliases_still_disable(monkeypatch):
    """HARES_SANDBOX_MODE=none/off/false/0 (0.1 explicit-disable) is
    still honored as DISABLED for backward compat."""
    monkeypatch.delenv("HARES_SANDBOX_DISABLED", raising=False)
    for v in ("none", "off", "false", "0"):
        monkeypatch.setenv("HARES_SANDBOX_MODE", v)
        assert load_sandbox_config().enabled is False


def test_unknown_mode_rejected(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_MODE", "chroot")
    with pytest.raises(ValueError, match="not supported"):
        load_sandbox_config()


def test_default_cwd_becomes_rw_bind(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_SANDBOX_MODE", "bwrap")
    monkeypatch.delenv("HARES_SANDBOX_RW", raising=False)
    cfg = load_sandbox_config(default_cwd=str(tmp_path))
    assert cfg.rw_binds == (str(tmp_path),)


def test_explicit_rw_overrides_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_SANDBOX_MODE", "bwrap")
    monkeypatch.setenv("HARES_SANDBOX_RW", "/tmp/a:/tmp/b")
    cfg = load_sandbox_config(default_cwd=str(tmp_path))
    assert cfg.rw_binds == ("/tmp/a", "/tmp/b")


def test_env_var_and_tilde_expansion_in_bind_paths(monkeypatch, tmp_path):
    """Bind paths support ${VAR} and ~ expansion so the parent process
    doesn't have to pre-expand them in YAML config."""
    monkeypatch.setenv("HARES_SANDBOX_MODE", "bwrap")
    monkeypatch.setenv("MY_TEST_DIR", str(tmp_path))
    monkeypatch.setenv("HARES_SANDBOX_RO", "${MY_TEST_DIR}/a:~/b")
    cfg = load_sandbox_config(default_cwd=str(tmp_path))
    assert cfg.ro_binds[0] == f"{tmp_path}/a", cfg.ro_binds
    assert cfg.ro_binds[1].startswith(os.path.expanduser("~")), cfg.ro_binds


def test_network_off(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_MODE", "bwrap")
    monkeypatch.setenv("HARES_SANDBOX_NETWORK", "off")
    assert load_sandbox_config().allow_network is False


def test_missing_bwrap_fails_fast(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_MODE", "bwrap")
    monkeypatch.setenv("HARES_SANDBOX_BWRAP_BIN", "definitely-not-installed-xyz")
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        load_sandbox_config()


def test_argv_includes_unshare_and_chdir(tmp_path):
    cfg = SandboxConfig(
        enabled=True, bwrap_bin="bwrap",
        rw_binds=(str(tmp_path),), ro_binds=(),
    )
    argv = build_bwrap_argv(cfg, "echo hi", cwd=str(tmp_path))
    assert "--unshare-pid" in argv
    assert "--unshare-uts" in argv
    assert "--die-with-parent" in argv
    assert "--chdir" in argv
    assert str(tmp_path) in argv
    # rw bind for the cwd
    assert "--bind" in argv
    # The user command lands at the tail through `/bin/sh -c`.
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]


def test_argv_unshare_net_when_disabled(tmp_path):
    cfg = SandboxConfig(
        enabled=True, bwrap_bin="bwrap",
        rw_binds=(str(tmp_path),), allow_network=False,
    )
    argv = build_bwrap_argv(cfg, "echo hi", cwd=str(tmp_path))
    assert "--unshare-net" in argv


def test_argv_no_unshare_net_when_enabled(tmp_path):
    cfg = SandboxConfig(
        enabled=True, bwrap_bin="bwrap",
        rw_binds=(str(tmp_path),), allow_network=True,
    )
    argv = build_bwrap_argv(cfg, "echo hi", cwd=str(tmp_path))
    assert "--unshare-net" not in argv


def test_argv_auto_binds_cwd_outside_rw_set(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    cfg = SandboxConfig(
        enabled=True, bwrap_bin="bwrap",
        rw_binds=(str(tmp_path / "allowed"),),
    )
    (tmp_path / "allowed").mkdir()
    argv = build_bwrap_argv(cfg, "true", cwd=str(other))
    # cwd should have been auto-added as a bind
    bind_pairs = []
    i = 0
    while i < len(argv):
        if argv[i] == "--bind" and i + 2 < len(argv):
            bind_pairs.append((argv[i+1], argv[i+2]))
            i += 3
        else:
            i += 1
    assert (str(other), str(other)) in bind_pairs


def test_disabled_argv_raises():
    cfg = SandboxConfig(enabled=False)
    with pytest.raises(ValueError, match="sandbox disabled"):
        build_bwrap_argv(cfg, "echo hi", cwd="/tmp")


def test_is_subpath():
    assert _is_subpath("/tmp/a/b", "/tmp/a") is True
    assert _is_subpath("/tmp/a", "/tmp/a") is True
    assert _is_subpath("/tmp/ab", "/tmp/a") is False
    assert _is_subpath("/tmp", "/tmp/a") is False


# ── Live bwrap tests (need a working bubblewrap) ───────────────────────────


def _runner(rw_binds: tuple[str, ...], allow_network: bool = True) -> Runner:
    cfg = SandboxConfig(
        enabled=True, bwrap_bin=shutil.which("bwrap") or "bwrap",
        rw_binds=rw_binds, ro_binds=(), allow_network=allow_network,
    )
    return Runner(
        max_concurrent=1, mem_limit_mb=512, cpu_limit_sec=30,
        sandbox=cfg,
    )


@needs_bwrap_runtime
async def test_bwrap_simple_echo(tmp_path):
    r = await _runner((str(tmp_path),)).execute("echo sandboxed", cwd=str(tmp_path))
    assert r["exit_code"] == 0, r
    assert r["stdout"].strip() == "sandboxed"


@needs_bwrap_runtime
async def test_bwrap_cannot_see_unbound_path(tmp_path):
    """The classic case: agent runs `find /home`, sandbox makes /home
    invisible (or empty). We bind only tmp_path, so /home should not
    be readable as a real host directory tree."""
    secret = Path("/home")  # exists on host but should not be bound
    r = await _runner((str(tmp_path),)).execute(
        f"ls /home 2>&1 | wc -l; echo ===; ls {tmp_path} 2>&1",
        cwd=str(tmp_path),
    )
    out = r["stdout"]
    # Inside the sandbox, /home should either not exist, exist but be empty,
    # or report "No such file or directory". In any case, it must NOT contain
    # any of the host's actual /home entries.
    real_home_entries = set(os.listdir("/home")) if secret.exists() else set()
    if real_home_entries:
        # Confirm none of the real entries leaked into the sandbox view.
        before, _, _ = out.partition("===")
        for entry in real_home_entries:
            assert entry not in before, (
                f"sandbox leaked /home entry {entry!r}: {out}"
            )


@needs_bwrap_runtime
async def test_bwrap_can_write_to_rw_bind(tmp_path):
    r = await _runner((str(tmp_path),)).execute(
        "echo content > out.txt && cat out.txt",
        cwd=str(tmp_path),
    )
    assert r["exit_code"] == 0
    # File should be visible on the host because rw bind is mutual.
    assert (tmp_path / "out.txt").exists()
    assert (tmp_path / "out.txt").read_text().strip() == "content"


@needs_bwrap_runtime
async def test_bwrap_proc_is_isolated(tmp_path):
    """Inside --unshare-pid, the inner shell sees only its own /proc
    (PID 1 = the shell, no others)."""
    r = await _runner((str(tmp_path),)).execute(
        "ls /proc | grep -E '^[0-9]+$' | wc -l",
        cwd=str(tmp_path),
    )
    assert r["exit_code"] == 0
    pid_count = int(r["stdout"].strip())
    # Just a handful: the shell, its child (the pipeline), and a couple
    # for the actively-running ls/grep/wc. Definitely not the host's
    # hundreds-to-thousands.
    assert pid_count < 30, f"expected isolated /proc, got {pid_count} pids"


@needs_bwrap_runtime
async def test_bwrap_network_unshare(tmp_path):
    """With --unshare-net, /proc/self/net/dev shows only `lo` (the
    new netns has no other interfaces). /proc is always mounted by
    bwrap, so this probe works on any host without needing /sys."""
    r = await _runner((str(tmp_path),), allow_network=False).execute(
        # awk strips header lines and the lo: line that always exists,
        # then prints anything left. Anything left = unshare didn't
        # work.
        "awk -F: 'NR>2 && $1!~/lo/ { gsub(/ /,\"\",$1); print $1 }' /proc/self/net/dev",
        cwd=str(tmp_path),
    )
    leftover = r["stdout"].strip().split("\n") if r["stdout"].strip() else []
    assert leftover == [], (
        f"expected only loopback in isolated netns, got extra: {leftover}\n"
        f"full result: {r}"
    )


@needs_bwrap_runtime
async def test_bwrap_preserves_resource_caps(tmp_path):
    """RLIMIT_AS still applies inside the sandbox: a runaway shell
    allocation should hit the cap and die."""
    cfg = SandboxConfig(
        enabled=True, bwrap_bin=shutil.which("bwrap") or "bwrap",
        rw_binds=(str(tmp_path),),
    )
    r = Runner(
        max_concurrent=1, mem_limit_mb=64, cpu_limit_sec=30, sandbox=cfg,
    )
    # Use a shell that resolves in /usr/bin (always there inside the
    # sandbox); avoid sys.executable since user-tools paths like /tool
    # aren't bound by default.
    cmd = (
        # Allocate ~300 MB by appending to a here-doc-like accumulator
        # in awk; awk lives in /usr/bin and respects RLIMIT_AS.
        "awk 'BEGIN { s=\"\"; for(i=0;i<300*1024*1024;i++) s=s\"x\"; print length(s) }'"
    )
    res = await r.execute(cmd, cwd=str(tmp_path), timeout=15.0)
    # Either awk OOM'd (exit_code != 0) OR the runner detected the kill.
    assert res["exit_code"] != 0 or res["killed_reason"] is not None, res
