"""Tests for hares.path_safety — system-dir validation, traversal
rejection, ceiling enforcement.

The module is dependency-free (only stdlib) so these tests can run
without spinning up any MCP server or runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hares.path_safety import (
    DEFAULT_SYSTEM_DIRS,
    PathSafetyError,
    get_system_dir_blocklist,
    resolve_under_ceiling,
    system_dirs_disallowed,
    validate_ceiling,
    validate_path_no_system_dir,
    validate_path_no_traversal,
)


# ── system_dirs_disallowed env parsing ─────────────────────────────────


def test_disallow_default_off(monkeypatch):
    monkeypatch.delenv("HARES_DISALLOW_SYSTEM_DIRS", raising=False)
    assert system_dirs_disallowed() is False


@pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE", "  Yes "])
def test_disallow_truthy_values(monkeypatch, v):
    monkeypatch.setenv("HARES_DISALLOW_SYSTEM_DIRS", v)
    assert system_dirs_disallowed() is True


@pytest.mark.parametrize("v", ["0", "false", "no", "off", ""])
def test_disallow_falsy_values(monkeypatch, v):
    monkeypatch.setenv("HARES_DISALLOW_SYSTEM_DIRS", v)
    assert system_dirs_disallowed() is False


# ── blocklist composition ─────────────────────────────────────────────


def test_default_blocklist_includes_obvious_system_dirs():
    bl = DEFAULT_SYSTEM_DIRS
    for expected in ("/etc", "/proc", "/sys", "/dev", "/bin", "/sbin", "/boot", "/root"):
        assert expected in bl


def test_default_blocklist_omits_policy_debatable_dirs():
    """/opt, /var, /run, /tmp, /home, /usr (parent) are intentionally
    NOT in the default blocklist — operators with legit needs would
    otherwise be blocked."""
    bl = DEFAULT_SYSTEM_DIRS
    for not_blocked in ("/opt", "/var", "/run", "/tmp", "/home", "/usr"):
        assert not_blocked not in bl


def test_extra_system_dirs_extends_blocklist(monkeypatch):
    monkeypatch.setenv("HARES_EXTRA_SYSTEM_DIRS", "/opt:/var/log")
    bl = get_system_dir_blocklist()
    assert "/opt" in bl
    assert "/var/log" in bl
    # Default entries still present.
    assert "/etc" in bl


def test_extra_system_dirs_empty_when_unset(monkeypatch):
    monkeypatch.delenv("HARES_EXTRA_SYSTEM_DIRS", raising=False)
    bl = get_system_dir_blocklist()
    assert bl == DEFAULT_SYSTEM_DIRS


# ── traversal validation ──────────────────────────────────────────────


@pytest.mark.parametrize("p", [
    "../etc/passwd",
    "foo/../etc",
    "foo/bar/../../etc",
    "..",
    "/foo/../etc",
])
def test_traversal_rejected(p):
    with pytest.raises(PathSafetyError, match="'..'"):
        validate_path_no_traversal(p)


@pytest.mark.parametrize("p", [
    "foo/bar",
    "/abs/path",
    "..hidden",         # double-dot in filename, NOT a traversal
    "foo/..bar/baz",    # double-dot inside a filename
    "",                 # empty — not a traversal
])
def test_non_traversal_accepted(p):
    validate_path_no_traversal(p)  # must not raise


# ── system-dir validation ─────────────────────────────────────────────


def test_no_check_when_strict_off(monkeypatch):
    monkeypatch.delenv("HARES_DISALLOW_SYSTEM_DIRS", raising=False)
    # Even a path explicitly under /etc is accepted.
    validate_path_no_system_dir(Path("/etc/passwd"))


def test_check_rejects_under_blocklist_when_strict_on(monkeypatch):
    monkeypatch.setenv("HARES_DISALLOW_SYSTEM_DIRS", "1")
    with pytest.raises(PathSafetyError, match="system directory"):
        validate_path_no_system_dir(Path("/etc/passwd"))
    with pytest.raises(PathSafetyError):
        validate_path_no_system_dir(Path("/etc"))  # exact match
    with pytest.raises(PathSafetyError):
        validate_path_no_system_dir(Path("/proc/self"))


def test_check_accepts_outside_blocklist_when_strict_on(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_DISALLOW_SYSTEM_DIRS", "1")
    monkeypatch.delenv("HARES_EXTRA_SYSTEM_DIRS", raising=False)
    validate_path_no_system_dir(tmp_path)
    validate_path_no_system_dir(Path("/opt/some/tool"))  # /opt NOT in default
    validate_path_no_system_dir(Path("/home/user/proj"))


def test_extra_system_dirs_takes_effect(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_DISALLOW_SYSTEM_DIRS", "1")
    monkeypatch.setenv("HARES_EXTRA_SYSTEM_DIRS", "/opt")
    with pytest.raises(PathSafetyError):
        validate_path_no_system_dir(Path("/opt/qemu"))


# ── ceiling validation ────────────────────────────────────────────────


def test_ceiling_under_git_always_rejected(monkeypatch, tmp_path):
    monkeypatch.delenv("HARES_DISALLOW_SYSTEM_DIRS", raising=False)
    bad = tmp_path / ".git" / "objects"
    bad.mkdir(parents=True)
    with pytest.raises(PathSafetyError, match=".git"):
        validate_ceiling(bad)


def test_ceiling_normal_path_accepted(monkeypatch, tmp_path):
    monkeypatch.delenv("HARES_DISALLOW_SYSTEM_DIRS", raising=False)
    validate_ceiling(tmp_path)  # must not raise


def test_ceiling_under_etc_rejected_when_strict(monkeypatch):
    monkeypatch.setenv("HARES_DISALLOW_SYSTEM_DIRS", "1")
    with pytest.raises(PathSafetyError, match="system directory"):
        validate_ceiling(Path("/etc"))


# ── resolve_under_ceiling ─────────────────────────────────────────────


def test_resolve_relative_path_under_ceiling(tmp_path):
    target = resolve_under_ceiling("foo/bar.txt", tmp_path)
    assert target == (tmp_path / "foo" / "bar.txt").resolve()


def test_resolve_absolute_path_under_ceiling(tmp_path):
    abs_path = str(tmp_path / "x")
    target = resolve_under_ceiling(abs_path, tmp_path)
    assert target == (tmp_path / "x").resolve()


def test_resolve_traversal_rejected(tmp_path):
    with pytest.raises(PathSafetyError, match="'..'"):
        resolve_under_ceiling("../escape", tmp_path)


def test_resolve_absolute_path_outside_ceiling_rejected(tmp_path):
    other = tmp_path.parent / "outside"
    with pytest.raises(PathSafetyError, match="not under ceiling"):
        resolve_under_ceiling(str(other), tmp_path)


def test_resolve_symlink_escape_rejected(tmp_path):
    """A symlink under the ceiling pointing OUTSIDE the ceiling must
    be rejected after realpath resolution."""
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "sneaky"
    link.symlink_to(outside)
    with pytest.raises(PathSafetyError, match="not under ceiling"):
        resolve_under_ceiling("sneaky", tmp_path)
