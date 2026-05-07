"""Round-4 reviewer findings:
  - sec-eng M.1 [HIGH]: parent-dir fsync after os.replace (durability).
  - sec-adv #3 [MEDIUM]: refuse to start if --state-file configured
    AND HARES_STATE_HMAC_SECRET unset.

The fsync test is structural: we can't reliably exercise power-loss
behavior in a unit test, so we assert the call shape (parent-dir
opened with O_DIRECTORY + fsync called). The CLI test exercises the
fail-closed path via subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hares.fs.state import ScopeStateStore


def test_save_calls_parent_dir_fsync(tmp_path, monkeypatch):
    """Round-4 [sec-eng M.1]: ``_save`` opens the parent dir with
    O_RDONLY|O_DIRECTORY and fsyncs it post-replace, so the rename's
    directory-entry update is durable across power loss."""
    monkeypatch.setenv("HARES_STATE_HMAC_SECRET", "test-secret-32-chars")
    import hares.fs.state as _s
    _s._PROCESS_FALLBACK_SECRET = None
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )

    real_open = os.open
    real_fsync = os.fsync
    parent_fd_fsynced: list[int] = []

    def tracking_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY and Path(str(path)) == tmp_path:
            parent_fd_fsynced.append(fd)
        return fd

    fsync_calls: list[int] = []

    def tracking_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    with patch("os.open", side_effect=tracking_open), \
         patch("os.fsync", side_effect=tracking_fsync):
        store.set([p])

    # The fd opened on the parent dir was fsynced.
    assert parent_fd_fsynced, (
        "_save did not open the parent dir with O_DIRECTORY"
    )
    assert any(fd in fsync_calls for fd in parent_fd_fsynced), (
        f"_save did not fsync the parent-dir fd "
        f"(opened {parent_fd_fsynced}, fsynced {fsync_calls})"
    )


def test_cli_refuses_state_file_without_hmac_secret(tmp_path, monkeypatch):
    """Round-4 [sec-adv #3]: hares-mcp refuses to start when
    --state-file is configured but HARES_STATE_HMAC_SECRET is unset.
    Exec via subprocess so the SystemExit + stderr surface like a
    real launch would."""
    state_file = tmp_path / "state.json"
    env = {
        # Drop HMAC secret if the host happens to have it set.
        k: v for k, v in os.environ.items()
        if k != "HARES_STATE_HMAC_SECRET"
    }
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin"))
    proc = subprocess.run(
        [
            sys.executable, "-m", "hares.cli",
            "--enable=fs",
            "--ceiling", str(tmp_path),
            "--state-file", str(state_file),
        ],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "HARES_STATE_HMAC_SECRET is unset" in combined, combined
    # Make sure we surface the operator-actionable hint.
    assert "high-entropy value" in combined or "32+ bytes" in combined, combined


def test_cli_starts_with_state_file_when_secret_pinned(tmp_path):
    """Sanity: pinning HARES_STATE_HMAC_SECRET allows --state-file
    startup. Use a dry-run-equivalent (--enable=fs without
    actually running) — we just need the validation to pass; the
    server stdio will block forever otherwise."""
    state_file = tmp_path / "state.json"
    env = dict(os.environ)
    env["HARES_STATE_HMAC_SECRET"] = "test-secret-32-bytes-of-entropy"
    # Spawn + immediately terminate. If validation passes, the
    # process gets to the MCP stdio loop (no exit). We send SIGTERM
    # after a brief moment.
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "hares.cli",
            "--enable=fs",
            "--ceiling", str(tmp_path),
            "--state-file", str(state_file),
        ],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        # Give the validation path a moment to run.
        try:
            stdout, stderr = proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            # Expected: process is in the MCP stdio loop, blocking.
            # That means validation passed.
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=5.0)
            combined = (stdout or b"").decode() + (stderr or b"").decode()
            # No "HMAC_SECRET is unset" in the output.
            assert "HARES_STATE_HMAC_SECRET is unset" not in combined, combined
            return
        # If we got here, the process exited inside the validation
        # window — should have been a clean exit (validation passed +
        # MCP stdio rejected an immediately-EOF stdin), not the
        # explicit refuse-to-start error.
        combined = (stdout or b"").decode() + (stderr or b"").decode()
        assert "HARES_STATE_HMAC_SECRET is unset" not in combined, combined
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
