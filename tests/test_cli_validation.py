"""Tests for hares.cli — argparse + dispatch validation.

These tests invoke the CLI in a subprocess to verify human-facing
behavior (error messages, exit codes). They cover only validation
paths; actual server runs are exercised by MCP-protocol-level
integration tests (test_*_proto.py)."""

from __future__ import annotations

import os
import subprocess

import pytest


HARES_MCP = "hares-mcp"


def _run(args, env_extra=None) -> tuple[int, str]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [HARES_MCP, *args],
        capture_output=True, text=True, env=env, timeout=10,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def test_help_works():
    rc, out = _run(["--help"])
    assert rc == 0
    assert "Hares" in out
    assert "--enable" in out
    assert "--ceiling" in out


def test_version_works():
    from hares import __version__
    rc, out = _run(["--version"])
    assert rc == 0
    assert __version__ in out


def test_missing_ceiling_defaults_to_pwd(tmp_path):
    """Bare invocation with no --ceiling and no HARES_FS_CEILING used
    to fail fast. Since 0.5 it defaults to $PWD with an INFO log so the
    most common first-run error goes away. We close stdin immediately
    so the server exits without blocking on the MCP handshake."""
    env = os.environ.copy()
    env["HARES_FS_CEILING"] = ""  # explicitly unset
    proc = subprocess.Popen(
        [HARES_MCP],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),  # PWD becomes the ceiling
        env=env,
    )
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=2)
    err = proc.stderr.read().decode() if proc.stderr else ""
    # Must NOT have failed with the old "ceiling is required" error.
    assert "--ceiling is required" not in err
    # Must have logged the PWD default at INFO so operators see it.
    assert "defaulting to $PWD" in err
    assert str(tmp_path) in err


def test_pwd_default_rejected_under_git(tmp_path):
    """If $PWD is under a .git/ tree, the default is rejected with a
    clear message — better than silently picking a bad ceiling."""
    bad = tmp_path / ".git" / "objects"
    bad.mkdir(parents=True)
    env = os.environ.copy()
    env["HARES_FS_CEILING"] = ""
    proc = subprocess.run(
        [HARES_MCP],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        cwd=str(bad), env=env, timeout=5,
    )
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "refusing to default" in out
    assert ".git" in out


@pytest.mark.parametrize("bad", ["Foo", "1abc", "with-dash", "UPPER", "with space"])
def test_invalid_scope_id_rejected(bad, tmp_path):
    rc, out = _run(
        ["--scope-id", bad, "--enable=fs"],
        env_extra={"HARES_FS_CEILING": str(tmp_path)},
    )
    assert rc != 0
    assert "scope-id" in out and "invalid" in out


@pytest.mark.parametrize("good", ["src", "unit_tests", "verification", "block_x", "x1"])
def test_valid_scope_id_passes_validation(good, tmp_path):
    """Validation passes; the actual MCP server then tries to start +
    block on stdin. We close stdin immediately so the process exits.
    Just verify validation doesn't reject the scope-id at startup."""
    env = os.environ.copy()
    env["HARES_FS_CEILING"] = str(tmp_path)
    proc = subprocess.Popen(
        [HARES_MCP, "--scope-id", good, "--enable=fs"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=2)
    err = proc.stderr.read().decode() if proc.stderr else ""
    # Validation must NOT have rejected the scope-id.
    assert "invalid" not in err, f"good scope_id {good!r} was rejected: {err}"


def test_ceiling_under_git_rejected(tmp_path):
    bad = tmp_path / ".git" / "objects"
    bad.mkdir(parents=True)
    rc, out = _run(["--enable=fs"], env_extra={"HARES_FS_CEILING": str(bad)})
    assert rc != 0
    assert ".git" in out


# ── In-ceiling blacklist (HARES_SANDBOX_EXCLUDE / _PROTECT) ─────────────


def test_exclude_outside_ceiling_rejected(tmp_path):
    outside = tmp_path.parent / "outside"
    rc, out = _run(
        ["--enable=fs"],
        env_extra={
            "HARES_FS_CEILING": str(tmp_path),
            "HARES_SANDBOX_EXCLUDE": str(outside),
        },
    )
    assert rc != 0
    assert "HARES_SANDBOX_EXCLUDE" in out
    assert "under the ceiling" in out


def test_exclude_equal_to_ceiling_rejected(tmp_path):
    rc, out = _run(
        ["--enable=fs"],
        env_extra={
            "HARES_FS_CEILING": str(tmp_path),
            "HARES_SANDBOX_EXCLUDE": str(tmp_path),
        },
    )
    assert rc != 0
    assert "HARES_SANDBOX_EXCLUDE" in out


def test_protect_traversal_rejected(tmp_path):
    rc, out = _run(
        ["--enable=fs"],
        env_extra={
            "HARES_FS_CEILING": str(tmp_path),
            "HARES_SANDBOX_PROTECT": "../escape",
        },
    )
    assert rc != 0
    assert "HARES_SANDBOX_PROTECT" in out


def test_valid_exclude_protect_passes_validation(tmp_path):
    """Valid in-ceiling entries must not be rejected at startup."""
    env = os.environ.copy()
    env["HARES_FS_CEILING"] = str(tmp_path)
    env["HARES_SANDBOX_EXCLUDE"] = "secrets"
    env["HARES_SANDBOX_PROTECT"] = "vendor"
    proc = subprocess.Popen(
        [HARES_MCP, "--enable=fs"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=2)
    err = proc.stderr.read().decode() if proc.stderr else ""
    assert "must be STRICTLY under" not in err, err
