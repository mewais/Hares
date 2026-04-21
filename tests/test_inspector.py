"""Tests for the pre-flight command inspector."""

from __future__ import annotations

import pytest

from hares.inspector import inspect_command, format_rewrite_notice


# ── pytest-xdist ────────────────────────────────────────────────────────────


def test_pytest_n_auto_rewritten_to_budget():
    r = inspect_command("pytest -n auto tests/", weight=1, max_concurrent=2)
    assert r.command == "pytest -n 1 tests/"
    assert len(r.rewrites) == 1
    assert r.rewrites[0].kind == "pytest_xdist"
    assert r.rewrites[0].original == "-n auto"


def test_pytest_n_too_many_clamped():
    r = inspect_command("pytest -n 8 tests/", weight=1, max_concurrent=2)
    assert r.command == "pytest -n 1 tests/"
    assert r.rewrites[0].kind == "pytest_xdist"


def test_pytest_n_within_budget_passes_through():
    r = inspect_command("pytest -n 1 tests/", weight=1, max_concurrent=2)
    assert r.command == "pytest -n 1 tests/"
    assert r.rewrites == []


def test_pytest_no_n_flag_passes_through():
    r = inspect_command("pytest tests/", weight=1, max_concurrent=2)
    assert r.command == "pytest tests/"
    assert r.rewrites == []


def test_python_m_pytest_recognised():
    r = inspect_command(
        "python -m pytest -n auto tests/", weight=1, max_concurrent=2,
    )
    assert "-n 1" in r.command
    assert len(r.rewrites) == 1


def test_uv_run_pytest_recognised():
    r = inspect_command(
        "uv run pytest -n auto tests/", weight=2, max_concurrent=2,
    )
    # weight=2 means budget=2 — "auto" is still > nothing, gets clamped
    assert "-n 2" in r.command
    assert len(r.rewrites) == 1


def test_pytest_attached_n_flag():
    r = inspect_command("pytest -nauto tests/", weight=1, max_concurrent=2)
    assert "-n1" in r.command
    assert len(r.rewrites) == 1


def test_pytest_numprocesses_long_flag():
    r = inspect_command(
        "pytest --numprocesses=auto tests/", weight=1, max_concurrent=2,
    )
    assert "--numprocesses=1" in r.command
    assert len(r.rewrites) == 1


def test_pytest_numprocesses_separate_token():
    r = inspect_command(
        "pytest --numprocesses auto tests/", weight=1, max_concurrent=2,
    )
    assert "--numprocesses 1" in r.command
    assert len(r.rewrites) == 1


def test_pytest_dot_test_alias():
    r = inspect_command("py.test -n auto tests/", weight=1, max_concurrent=2)
    assert "-n 1" in r.command


# ── make / ninja ────────────────────────────────────────────────────────────


def test_make_jN_too_many_clamped():
    r = inspect_command("make -j8", weight=1, max_concurrent=2)
    assert "-j1" in r.command
    assert r.rewrites[0].kind == "make_jobs"


def test_make_bare_j_clamped():
    r = inspect_command("make -j", weight=1, max_concurrent=2)
    # Bare -j becomes -j1 (budget)
    assert "-j1" in r.command
    assert r.rewrites[0].kind == "make_jobs"


def test_make_jobs_long_clamped():
    r = inspect_command("make --jobs=16 all", weight=1, max_concurrent=2)
    assert "--jobs=1" in r.command


def test_ninja_jN_clamped():
    r = inspect_command("ninja -j32", weight=1, max_concurrent=2)
    assert "-j1" in r.command
    assert r.rewrites[0].kind == "ninja_jobs"


def test_make_within_budget_passes():
    r = inspect_command("make -j1", weight=1, max_concurrent=2)
    assert r.rewrites == []


# ── cargo ───────────────────────────────────────────────────────────────────


def test_cargo_jobs_clamped():
    r = inspect_command(
        "cargo build --jobs 64", weight=1, max_concurrent=2,
    )
    assert "--jobs 1" in r.command
    assert r.rewrites[0].kind == "cargo_jobs"


def test_cargo_jobs_eq_clamped():
    r = inspect_command(
        "cargo test --jobs=8", weight=1, max_concurrent=2,
    )
    assert "--jobs=1" in r.command


# ── unrelated commands pass through ────────────────────────────────────────


def test_unrelated_command_passes_through():
    cmd = "echo hello && ls -la"
    r = inspect_command(cmd, weight=1, max_concurrent=2)
    assert r.rewrites == []


def test_unparseable_command_passes_through():
    # Unbalanced quote: shlex.split raises; we should return the
    # original string unchanged.
    cmd = "echo 'unbalanced"
    r = inspect_command(cmd, weight=1, max_concurrent=2)
    assert r.command == cmd
    assert r.rewrites == []


def test_weight_2_gives_budget_2():
    r = inspect_command(
        "pytest -n 8 tests/", weight=2, max_concurrent=2,
    )
    assert "-n 2" in r.command


def test_weight_capped_to_max_concurrent():
    # weight=99 caps to max_concurrent=2
    r = inspect_command(
        "pytest -n 8 tests/", weight=99, max_concurrent=2,
    )
    assert "-n 2" in r.command


# ── notice formatting ──────────────────────────────────────────────────────


def test_format_rewrite_notice_lists_each_edit():
    r = inspect_command("pytest -n auto tests/", weight=1, max_concurrent=2)
    notice = format_rewrite_notice(r.rewrites)
    assert "[hares: pre-flight rewrote command" in notice
    assert "'-n auto' → '-n 1'" in notice


def test_format_rewrite_notice_empty():
    notice = format_rewrite_notice([])
    # Should still have the header line, just no bullets
    assert "[hares:" in notice
