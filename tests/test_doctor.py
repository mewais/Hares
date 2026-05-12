"""Tests for hares.doctor — environment diagnostic.

Each individual check function is tested in isolation by manipulating
env vars / monkeypatching shutil.which. Whole-render and CLI-dispatch
tests cover the wiring.
"""

from __future__ import annotations

import os
import sys
import subprocess

import pytest

from hares import doctor


# HARES_* env vars are wiped before each test by tests/conftest.py.


# ── check_python_version ───────────────────────────────────────────────────

def test_python_version_passes_on_supported_runtime():
    r = doctor.check_python_version()
    assert r.level == "ok"
    assert "3.10" in r.title or "3.11" in r.title or "3.12" in r.title or "3.13" in r.title


# ── check_bwrap ────────────────────────────────────────────────────────────

def test_bwrap_missing_with_sandbox_disabled_warns(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_DISABLED", "1")
    monkeypatch.setattr("shutil.which", lambda _: None)
    r = doctor.check_bwrap()
    assert r.level == "warn"
    assert "not found" in r.title.lower()


def test_bwrap_missing_without_sandbox_disabled_errors(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    r = doctor.check_bwrap()
    assert r.level == "err"
    assert r.hint is not None
    assert "bubblewrap" in r.hint.lower() or "install" in r.hint.lower()


def test_bwrap_present_returns_ok(monkeypatch, tmp_path):
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\necho 'bubblewrap 0.6.2'\n")
    fake_bwrap.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda name: str(fake_bwrap))
    # Stub subprocess.run so we don't depend on the host's bwrap.
    monkeypatch.setattr(
        doctor.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=a[0], returncode=0, stdout="bubblewrap 0.6.2\n", stderr="",
        ),
    )
    r = doctor.check_bwrap()
    assert r.level == "ok"
    assert "0.6.2" in r.title


def test_bwrap_honors_custom_binary_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_SANDBOX_BWRAP_BIN", "/opt/custom/bwrap")
    captured: dict = {}
    def fake_which(name):
        captured["arg"] = name
        return None
    monkeypatch.setattr("shutil.which", fake_which)
    doctor.check_bwrap()
    assert captured["arg"] == "/opt/custom/bwrap"


# ── check_slot_files ───────────────────────────────────────────────────────

def test_slot_files_no_coord_dir_is_ok(monkeypatch):
    monkeypatch.delenv("HARES_COORDINATION_DIR", raising=False)
    r = doctor.check_slot_files()
    assert r.level == "ok"
    assert "inactive" in r.title.lower()


def test_slot_files_coord_dir_missing(monkeypatch, tmp_path):
    missing = tmp_path / "nonexistent"
    monkeypatch.setenv("HARES_COORDINATION_DIR", str(missing))
    monkeypatch.setenv("HARES_MAX_CONCURRENT", "2")
    r = doctor.check_slot_files()
    assert r.level == "warn"
    assert "does not exist" in r.title.lower()


def test_slot_files_all_free(monkeypatch, tmp_path):
    from hares.coordination import CrossProcessCoordinator
    coord_dir = tmp_path / "coord"
    CrossProcessCoordinator(coord_dir=coord_dir, max_concurrent=2)
    monkeypatch.setenv("HARES_COORDINATION_DIR", str(coord_dir))
    monkeypatch.setenv("HARES_MAX_CONCURRENT", "2")
    r = doctor.check_slot_files()
    assert r.level == "ok"
    assert "2/2" in r.title


# ── check_fs_ceiling ───────────────────────────────────────────────────────

def test_fs_ceiling_unset_passes_with_pwd_note():
    """Since 0.5, unset HARES_FS_CEILING is fine — the loader defaults
    to $PWD. The doctor surfaces this as an OK with the PWD path
    visible so the operator sees the implicit choice."""
    r = doctor.check_fs_ceiling()
    assert r.level == "ok"
    assert "$PWD" in r.title
    assert os.getcwd() in r.title


def test_fs_ceiling_existing_writable_dir_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path))
    r = doctor.check_fs_ceiling()
    assert r.level == "ok"
    assert str(tmp_path) in r.title


def test_fs_ceiling_nonexistent_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path / "does-not-exist"))
    r = doctor.check_fs_ceiling()
    assert r.level == "err"
    assert "does not exist" in r.title


def test_fs_ceiling_under_git_errors(monkeypatch, tmp_path):
    bad = tmp_path / ".git" / "objects"
    bad.mkdir(parents=True)
    monkeypatch.setenv("HARES_FS_CEILING", str(bad))
    r = doctor.check_fs_ceiling()
    assert r.level == "err"
    assert ".git" in r.title


# ── check_state_hmac ───────────────────────────────────────────────────────

def test_state_hmac_set_passes(monkeypatch):
    monkeypatch.setenv("HARES_STATE_HMAC_SECRET", "abc" * 12)
    assert doctor.check_state_hmac().level == "ok"


def test_state_hmac_unset_warns(monkeypatch):
    assert doctor.check_state_hmac().level == "warn"


# ── check_coordination_dir ─────────────────────────────────────────────────

def test_coordination_dir_unset_passes(monkeypatch):
    assert doctor.check_coordination_dir().level == "ok"


def test_coordination_dir_existing_writable_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_COORDINATION_DIR", str(tmp_path))
    assert doctor.check_coordination_dir().level == "ok"


def test_coordination_dir_nonexistent_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_COORDINATION_DIR", str(tmp_path / "missing"))
    assert doctor.check_coordination_dir().level == "warn"


# ── check_sandbox_disabled ─────────────────────────────────────────────────

def test_sandbox_enabled_passes(monkeypatch):
    assert doctor.check_sandbox_disabled().level == "ok"


def test_sandbox_disabled_warns(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_DISABLED", "1")
    r = doctor.check_sandbox_disabled()
    assert r.level == "warn"
    assert "bwrap" in r.title.lower()


def test_legacy_sandbox_mode_off_warns(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_MODE", "none")
    assert doctor.check_sandbox_disabled().level == "warn"


# ── check_lsf / check_slurm ────────────────────────────────────────────────

def test_lsf_all_missing_warns(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    r = doctor.check_lsf()
    assert r.level == "warn"
    assert "lsf" in r.title.lower()


def test_lsf_partially_installed_errors(monkeypatch):
    found = {"bsub": "/usr/bin/bsub", "bjobs": None, "bkill": "/usr/bin/bkill"}
    monkeypatch.setattr("shutil.which", lambda name: found.get(name))
    r = doctor.check_lsf()
    assert r.level == "err"
    assert "bjobs" in r.title


def test_slurm_all_present_passes(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    r = doctor.check_slurm()
    assert r.level == "ok"


def test_slurm_honors_custom_binary_env(monkeypatch):
    monkeypatch.setenv("HARES_SLURM_SBATCH_BIN", "/opt/slurm/bin/sbatch")
    queried: list[str] = []
    monkeypatch.setattr("shutil.which", lambda name: (queried.append(name), None)[1])
    doctor.check_slurm()
    assert "/opt/slurm/bin/sbatch" in queried


# ── render() and counts ────────────────────────────────────────────────────

def test_render_returns_text_and_counts(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path))
    text, counts = doctor.render(use_color=False)
    assert "Hares" in text
    assert "Sandbox" in text
    assert "Cluster" in text
    assert "Summary:" in text
    assert counts["ok"] >= 1
    assert sum(counts.values()) >= 5  # at least one per check


def test_render_color_mode_includes_ansi_codes(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path))
    text, _ = doctor.render(use_color=True)
    assert "\033[" in text


def test_render_no_color_excludes_ansi(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path))
    text, _ = doctor.render(use_color=False)
    assert "\033[" not in text


# ── run() exit-code wiring ─────────────────────────────────────────────────

def test_run_returns_zero_when_no_errors(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path))
    # Force bwrap "missing but sandbox-disabled" to avoid a hard err.
    monkeypatch.setenv("HARES_SANDBOX_DISABLED", "1")
    monkeypatch.setattr("shutil.which", lambda name: None)
    rc = doctor.run(["--no-color"])
    captured = capsys.readouterr()
    assert "Summary:" in captured.out
    assert rc == 0


def test_run_returns_one_when_errors(monkeypatch, capsys):
    # Force a real error: bwrap missing AND sandbox not disabled.
    monkeypatch.setattr("shutil.which", lambda name: None)
    rc = doctor.run(["--no-color"])
    capsys.readouterr()  # drain
    assert rc == 1


# ── CLI dispatch (subprocess) ──────────────────────────────────────────────

def test_cli_dispatches_doctor_subcommand():
    """`hares-mcp doctor --no-color` should run and exit cleanly."""
    env = os.environ.copy()
    proc = subprocess.run(
        ["hares-mcp", "doctor", "--no-color"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    # Exit code may be 0 or 1 depending on host; we only assert that it
    # ran the doctor (not the server) and produced a Summary line.
    out = proc.stdout + proc.stderr
    assert "Summary:" in out
    assert "Hares" in out


def test_cli_help_mentions_doctor():
    proc = subprocess.run(
        ["hares-mcp", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    assert "doctor" in (proc.stdout + proc.stderr).lower()
