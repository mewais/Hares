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


# ── Helpers ─────────────────────────────────────────────────────────────────

def _clean_env(monkeypatch):
    """Strip any HARES_* env vars that could leak from the host."""
    for key in list(os.environ):
        if key.startswith("HARES_"):
            monkeypatch.delenv(key, raising=False)


# ── check_python_version ───────────────────────────────────────────────────

def test_python_version_passes_on_supported_runtime():
    r = doctor.check_python_version()
    assert r.level == "ok"
    assert "3.10" in r.title or "3.11" in r.title or "3.12" in r.title or "3.13" in r.title


# ── check_bwrap ────────────────────────────────────────────────────────────

def test_bwrap_missing_with_sandbox_disabled_warns(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_SANDBOX_DISABLED", "1")
    monkeypatch.setattr("shutil.which", lambda _: None)
    r = doctor.check_bwrap()
    assert r.level == "warn"
    assert "not found" in r.title.lower()


def test_bwrap_missing_without_sandbox_disabled_errors(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda _: None)
    r = doctor.check_bwrap()
    assert r.level == "err"
    assert r.hint is not None
    assert "bubblewrap" in r.hint.lower() or "install" in r.hint.lower()


def test_bwrap_present_returns_ok(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
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
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_SANDBOX_BWRAP_BIN", "/opt/custom/bwrap")
    captured: dict = {}
    def fake_which(name):
        captured["arg"] = name
        return None
    monkeypatch.setattr("shutil.which", fake_which)
    doctor.check_bwrap()
    assert captured["arg"] == "/opt/custom/bwrap"


# ── check_posix_ipc ────────────────────────────────────────────────────────

def test_posix_ipc_check_matches_actual_install_state(monkeypatch):
    """The check's verdict should match whether posix_ipc actually imports
    in the current interpreter — assert that contract regardless of
    whether the test env has it."""
    _clean_env(monkeypatch)
    try:
        import posix_ipc  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    r = doctor.check_posix_ipc()
    assert r.level == ("ok" if installed else "warn")


def test_posix_ipc_missing_with_coordination_dir_errors(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_COORDINATION_DIR", "/tmp/coord")
    # Force ImportError by hiding the module.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *a, **kw):
        if name == "posix_ipc":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)
    r = doctor.check_posix_ipc()
    assert r.level == "err"
    assert "coordination" in r.hint.lower() or "pip install" in r.hint.lower()


def test_posix_ipc_missing_without_coordination_dir_warns(monkeypatch):
    _clean_env(monkeypatch)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *a, **kw):
        if name == "posix_ipc":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)
    r = doctor.check_posix_ipc()
    assert r.level == "warn"


# ── check_fs_ceiling ───────────────────────────────────────────────────────

def test_fs_ceiling_unset_warns(monkeypatch):
    _clean_env(monkeypatch)
    r = doctor.check_fs_ceiling()
    assert r.level == "warn"
    assert "not set" in r.title.lower()


def test_fs_ceiling_existing_writable_dir_passes(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path))
    r = doctor.check_fs_ceiling()
    assert r.level == "ok"
    assert str(tmp_path) in r.title


def test_fs_ceiling_nonexistent_errors(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path / "does-not-exist"))
    r = doctor.check_fs_ceiling()
    assert r.level == "err"
    assert "does not exist" in r.title


def test_fs_ceiling_under_git_errors(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    bad = tmp_path / ".git" / "objects"
    bad.mkdir(parents=True)
    monkeypatch.setenv("HARES_FS_CEILING", str(bad))
    r = doctor.check_fs_ceiling()
    assert r.level == "err"
    assert ".git" in r.title


# ── check_state_hmac ───────────────────────────────────────────────────────

def test_state_hmac_set_passes(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_STATE_HMAC_SECRET", "abc" * 12)
    assert doctor.check_state_hmac().level == "ok"


def test_state_hmac_unset_warns(monkeypatch):
    _clean_env(monkeypatch)
    assert doctor.check_state_hmac().level == "warn"


# ── check_coordination_dir ─────────────────────────────────────────────────

def test_coordination_dir_unset_passes(monkeypatch):
    _clean_env(monkeypatch)
    assert doctor.check_coordination_dir().level == "ok"


def test_coordination_dir_existing_writable_passes(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_COORDINATION_DIR", str(tmp_path))
    assert doctor.check_coordination_dir().level == "ok"


def test_coordination_dir_nonexistent_warns(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_COORDINATION_DIR", str(tmp_path / "missing"))
    assert doctor.check_coordination_dir().level == "warn"


# ── check_sandbox_disabled ─────────────────────────────────────────────────

def test_sandbox_enabled_passes(monkeypatch):
    _clean_env(monkeypatch)
    assert doctor.check_sandbox_disabled().level == "ok"


def test_sandbox_disabled_warns(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_SANDBOX_DISABLED", "1")
    r = doctor.check_sandbox_disabled()
    assert r.level == "warn"
    assert "bwrap" in r.title.lower()


def test_legacy_sandbox_mode_off_warns(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_SANDBOX_MODE", "none")
    assert doctor.check_sandbox_disabled().level == "warn"


# ── check_lsf / check_slurm ────────────────────────────────────────────────

def test_lsf_all_missing_warns(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda _: None)
    r = doctor.check_lsf()
    assert r.level == "warn"
    assert "lsf" in r.title.lower()


def test_lsf_partially_installed_errors(monkeypatch):
    _clean_env(monkeypatch)
    found = {"bsub": "/usr/bin/bsub", "bjobs": None, "bkill": "/usr/bin/bkill"}
    monkeypatch.setattr("shutil.which", lambda name: found.get(name))
    r = doctor.check_lsf()
    assert r.level == "err"
    assert "bjobs" in r.title


def test_slurm_all_present_passes(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    r = doctor.check_slurm()
    assert r.level == "ok"


def test_slurm_honors_custom_binary_env(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_SLURM_SBATCH_BIN", "/opt/slurm/bin/sbatch")
    queried: list[str] = []
    monkeypatch.setattr("shutil.which", lambda name: (queried.append(name), None)[1])
    doctor.check_slurm()
    assert "/opt/slurm/bin/sbatch" in queried


# ── render() and counts ────────────────────────────────────────────────────

def test_render_returns_text_and_counts(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path))
    text, counts = doctor.render(use_color=False)
    assert "Hares" in text
    assert "Sandbox" in text
    assert "Cluster" in text
    assert "Summary:" in text
    assert counts["ok"] >= 1
    assert sum(counts.values()) >= 5  # at least one per check


def test_render_color_mode_includes_ansi_codes(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path))
    text, _ = doctor.render(use_color=True)
    assert "\033[" in text


def test_render_no_color_excludes_ansi(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path))
    text, _ = doctor.render(use_color=False)
    assert "\033[" not in text


# ── run() exit-code wiring ─────────────────────────────────────────────────

def test_run_returns_zero_when_no_errors(monkeypatch, tmp_path, capsys):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_FS_CEILING", str(tmp_path))
    # Force bwrap "missing but sandbox-disabled" to avoid a hard err.
    monkeypatch.setenv("HARES_SANDBOX_DISABLED", "1")
    monkeypatch.setattr("shutil.which", lambda name: None)
    rc = doctor.run(["--no-color"])
    captured = capsys.readouterr()
    assert "Summary:" in captured.out
    assert rc == 0


def test_run_returns_one_when_errors(monkeypatch, capsys):
    _clean_env(monkeypatch)
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
