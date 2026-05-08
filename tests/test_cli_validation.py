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


def test_missing_ceiling_fails(tmp_path, monkeypatch):
    # Strip env so default-from-env doesn't accidentally satisfy.
    rc, out = _run([], env_extra={"HARES_FS_CEILING": ""})
    assert rc != 0
    assert "--ceiling is required" in out


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
