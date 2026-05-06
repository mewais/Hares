"""Tests for hares.fs.state — atomic state-file persistence + crash recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hares.fs.state import ActiveScope, ScopeStateStore, STATE_VERSION


def test_no_state_file_means_empty_scope(tmp_path):
    store = ScopeStateStore(scope_id="src", ceiling=tmp_path, state_file=None)
    assert store.current().paths == []
    assert store.current().scope_id == "src"


def test_set_and_persist_round_trip(tmp_path):
    state_file = tmp_path / "state.json"
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    p1 = tmp_path / "lib" / "parser"
    p1.mkdir(parents=True)
    store.set([p1])
    # File on disk reflects the set.
    saved = json.loads(state_file.read_text())
    assert saved["version"] == STATE_VERSION
    assert saved["scope_id"] == "src"
    assert str(p1.resolve()) in saved["active_paths"]


def test_reload_restores_active_scope(tmp_path):
    state_file = tmp_path / "state.json"
    p1 = tmp_path / "x"
    p1.mkdir()
    store1 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store1.set([p1])
    # Fresh store sees the persisted scope.
    store2 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert [str(p) for p in store2.current().paths] == [str(p1.resolve())]


def test_corrupt_state_file_falls_back_to_empty(tmp_path, caplog):
    state_file = tmp_path / "state.json"
    state_file.write_text("{ this is { invalid }}")
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store.current().paths == []
    assert any("corrupt" in r.message for r in caplog.records)


def test_version_mismatch_falls_back_to_empty(tmp_path, caplog):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "version": 999,
        "scope_id": "src",
        "ceiling": str(tmp_path),
        "active_paths": ["/whatever"],
    }))
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store.current().paths == []
    assert any("version mismatch" in r.message for r in caplog.records)


def test_scope_id_mismatch_falls_back_to_empty(tmp_path, caplog):
    state_file = tmp_path / "state.json"
    p1 = tmp_path / "x"
    p1.mkdir()
    # Write state under scope_id="src"
    store_src = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store_src.set([p1])
    # Reload as scope_id="unit_tests" — mismatch, fall back.
    store_other = ScopeStateStore(
        scope_id="unit_tests", ceiling=tmp_path, state_file=state_file,
    )
    assert store_other.current().paths == []
    assert any("scope_id" in r.message for r in caplog.records)


def test_ceiling_mismatch_falls_back_to_empty(tmp_path, caplog):
    state_file = tmp_path / "state.json"
    sub = tmp_path / "sub"
    sub.mkdir()
    p1 = sub / "x"
    p1.mkdir()
    store_a = ScopeStateStore(
        scope_id="src", ceiling=sub, state_file=state_file,
    )
    store_a.set([p1])
    # Reload with a different ceiling — mismatch.
    store_b = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store_b.current().paths == []
    assert any("ceiling" in r.message for r in caplog.records)


def test_set_replace_semantics(tmp_path):
    state_file = tmp_path / "state.json"
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    c = tmp_path / "c"; c.mkdir()
    store.set([a, b])
    assert {str(p) for p in store.current().paths} == {str(a.resolve()), str(b.resolve())}
    # New set REPLACES, doesn't accumulate.
    store.set([c])
    assert {str(p) for p in store.current().paths} == {str(c.resolve())}


def test_atomic_write_via_tmp(tmp_path):
    """Write goes through .tmp + rename — no partial file visible at
    the destination during a write."""
    state_file = tmp_path / "state.json"
    p1 = tmp_path / "x"; p1.mkdir()
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store.set([p1])
    # No .tmp left over after a successful write.
    assert not (tmp_path / "state.json.tmp").exists()
    # Final file is well-formed JSON.
    json.loads(state_file.read_text())
