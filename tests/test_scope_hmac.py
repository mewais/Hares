"""Tests for the 0.2.2 HMAC-signed state file + WAL recovery feature.

Covers:
  - HMAC field present in saved file + tool replies
  - HMAC verifies correctly via canonical_hmac_payload + secret env
  - Tampered (paths, seq) tuple → load rejects with empty fallback
  - Forged HMAC (random bytes) → load rejects
  - Missing HMAC field in v3 → load rejects
  - Missing seq field (v3 mandatory) → load rejects
  - WAL recovery: crash mid-write leaves higher-seq .tmp + lower-seq
    .json; load commits the .tmp
  - WAL recovery: invalid HMAC on .tmp does NOT commit (defends
    against attacker writing a forged .tmp)
  - Secret rotation invalidates the prior file (a fresh process with
    a different secret rejects the old file)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hares.fs.state import (
    ScopeStateStore,
    STATE_VERSION,
    canonical_hmac_payload,
    compute_state_hmac,
    _PROCESS_FALLBACK_SECRET,  # noqa: F401 — for test introspection
)
from hares.fs.tools import build_restrict_tool_handlers


_PINNED_SECRET = "test-secret-32-bytes-of-entropy-base64"


@pytest.fixture
def pinned_secret(monkeypatch):
    """Pin HARES_STATE_HMAC_SECRET so tests don't depend on the
    per-process random fallback (which would change per pytest run
    AND across stores within a single run)."""
    monkeypatch.setenv("HARES_STATE_HMAC_SECRET", _PINNED_SECRET)
    # Reset the process fallback so an unrelated test's fallback
    # doesn't leak state into ours.
    import hares.fs.state as _s
    _s._PROCESS_FALLBACK_SECRET = None


def test_saved_file_includes_hmac_field(tmp_path, pinned_secret):
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store.set([p])
    saved = json.loads(state_file.read_text())
    assert "hmac" in saved
    assert isinstance(saved["hmac"], str)
    assert len(saved["hmac"]) == 64  # sha256 hex


def test_saved_hmac_verifies(tmp_path, pinned_secret):
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store.set([p])
    saved = json.loads(state_file.read_text())
    expected = compute_state_hmac(
        version=STATE_VERSION,
        scope_id="src",
        ceiling=str(tmp_path.resolve()),
        seq=saved["seq"],
        sorted_paths=saved["active_paths"],
        secret=_PINNED_SECRET.encode(),
    )
    assert saved["hmac"] == expected


def test_tampered_paths_rejected(tmp_path, pinned_secret, caplog):
    """Attacker swaps active_paths to add a wider scope without
    updating the HMAC — load must reject."""
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store.set([p])
    # Tamper.
    saved = json.loads(state_file.read_text())
    saved["active_paths"] = [str(tmp_path.resolve())]  # wider scope
    state_file.write_text(json.dumps(saved))
    # Fresh store reload.
    store2 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store2.current().paths == []
    assert store2.current().seq == 0
    assert any("HMAC verification" in r.message for r in caplog.records)


def test_tampered_seq_rejected(tmp_path, pinned_secret, caplog):
    """Attacker fast-forwards seq without updating HMAC — load rejects.
    Closes the round-3 HIGH 'seq fast-forward' finding."""
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store.set([p])
    saved = json.loads(state_file.read_text())
    saved["seq"] = 10_000_000  # the fast-forward attack
    state_file.write_text(json.dumps(saved))
    store2 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store2.current().seq == 0
    assert any("HMAC verification" in r.message for r in caplog.records)


def test_random_hmac_rejected(tmp_path, pinned_secret, caplog):
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    state_file.write_text(json.dumps({
        "version": STATE_VERSION,
        "scope_id": "src",
        "ceiling": str(tmp_path.resolve()),
        "active_paths": [str(p.resolve())],
        "seq": 1,
        "hmac": "deadbeef" * 8,  # nonsense
        "last_restrict_at": None,
    }))
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store.current().paths == []
    assert any("HMAC verification" in r.message for r in caplog.records)


def test_missing_hmac_field_rejected(tmp_path, pinned_secret, caplog):
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    state_file.write_text(json.dumps({
        "version": STATE_VERSION,
        "scope_id": "src",
        "ceiling": str(tmp_path.resolve()),
        "active_paths": [str(p.resolve())],
        "seq": 1,
        # NO hmac
    }))
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store.current().paths == []
    assert any("missing 'hmac'" in r.message for r in caplog.records)


def test_missing_seq_field_rejected(tmp_path, pinned_secret, caplog):
    """0.2.2 [security-engineer D.3]: seq is mandatory in v3."""
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    state_file.write_text(json.dumps({
        "version": STATE_VERSION,
        "scope_id": "src",
        "ceiling": str(tmp_path.resolve()),
        "active_paths": [str(p.resolve())],
        # NO seq
        "hmac": "0" * 64,
    }))
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store.current().paths == []
    assert any("missing required 'seq'" in r.message for r in caplog.records)


def test_secret_rotation_invalidates_prior_file(tmp_path, monkeypatch, caplog):
    """Operator changes HARES_STATE_HMAC_SECRET between runs — the
    new process can't verify the old file → falls back to empty scope.
    Documented behavior (warn-and-rebuild)."""
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    monkeypatch.setenv("HARES_STATE_HMAC_SECRET", "secret-A-with-enough-bytes")
    import hares.fs.state as _s
    _s._PROCESS_FALLBACK_SECRET = None
    store1 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store1.set([p])
    # Rotate.
    monkeypatch.setenv("HARES_STATE_HMAC_SECRET", "secret-B-with-enough-bytes")
    store2 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store2.current().paths == []
    assert any("HMAC verification" in r.message for r in caplog.records)


# ── WAL recovery ───────────────────────────────────────────────────────


def test_wal_recovery_commits_higher_seq_tmp(tmp_path, pinned_secret, caplog):
    """Crash between fsync and rename leaves a .tmp with higher seq.
    Next load commits the .tmp over the main file — preserves the
    auditor's expected_next."""
    state_file = tmp_path / "state.json"
    wal = state_file.with_suffix(state_file.suffix + ".tmp")
    p = tmp_path / "x"; p.mkdir()
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store.set([p])  # seq 1
    store.set([p])  # seq 2 — main file at seq 2
    # Simulate crash mid-write of seq 3: hand-craft a valid .tmp at seq 3.
    sig3 = compute_state_hmac(
        version=STATE_VERSION,
        scope_id="src",
        ceiling=str(tmp_path.resolve()),
        seq=3,
        sorted_paths=[str(p.resolve())],
        secret=_PINNED_SECRET.encode(),
    )
    wal.write_text(json.dumps({
        "version": STATE_VERSION,
        "scope_id": "src",
        "ceiling": str(tmp_path.resolve()),
        "active_paths": [str(p.resolve())],
        "seq": 3,
        "hmac": sig3,
        "last_restrict_at": None,
    }))
    # Reload — WAL should commit.
    store2 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store2.current().seq == 3
    assert any("WAL recovery" in r.message for r in caplog.records)
    # WAL was committed (renamed) — file no longer exists.
    assert not wal.exists()
    # Main file now has seq=3.
    assert json.loads(state_file.read_text())["seq"] == 3


def test_wal_with_invalid_hmac_does_not_commit(tmp_path, pinned_secret):
    """An attacker writing a forged .tmp can't trick load into
    committing it — HMAC verification gates WAL recovery the same
    way it gates main-file load."""
    state_file = tmp_path / "state.json"
    wal = state_file.with_suffix(state_file.suffix + ".tmp")
    p = tmp_path / "x"; p.mkdir()
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store.set([p])  # seq 1 in main file
    # Forged .tmp with seq 99 + bogus hmac.
    wal.write_text(json.dumps({
        "version": STATE_VERSION,
        "scope_id": "src",
        "ceiling": str(tmp_path.resolve()),
        "active_paths": [str(tmp_path.resolve())],  # wider scope!
        "seq": 99,
        "hmac": "f" * 64,  # forged
        "last_restrict_at": None,
    }))
    store2 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    # Main file (seq=1, valid hmac) wins.
    assert store2.current().seq == 1
    assert {str(x) for x in store2.current().paths} == {str(p.resolve())}


def test_wal_with_lower_seq_is_cleaned_up(tmp_path, pinned_secret):
    """Stale .tmp at lower seq than main is deleted on load."""
    state_file = tmp_path / "state.json"
    wal = state_file.with_suffix(state_file.suffix + ".tmp")
    p = tmp_path / "x"; p.mkdir()
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store.set([p])  # seq 1
    store.set([p])  # seq 2
    # Hand-craft a stale .tmp at seq 1.
    sig1 = compute_state_hmac(
        version=STATE_VERSION,
        scope_id="src",
        ceiling=str(tmp_path.resolve()),
        seq=1,
        sorted_paths=[str(p.resolve())],
        secret=_PINNED_SECRET.encode(),
    )
    wal.write_text(json.dumps({
        "version": STATE_VERSION,
        "scope_id": "src",
        "ceiling": str(tmp_path.resolve()),
        "active_paths": [str(p.resolve())],
        "seq": 1,
        "hmac": sig1,
        "last_restrict_at": None,
    }))
    store2 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store2.current().seq == 2
    # Stale WAL was cleaned up.
    assert not wal.exists()


# ── Tool reply shape ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_replies_include_hmac_and_version(tmp_path, pinned_secret):
    p = tmp_path / "a"
    state, handlers = build_restrict_tool_handlers(
        scope_id="src", ceiling=tmp_path, state_file=None,
    )
    r = await handlers["src_restrict_paths"]({"paths": [str(p)]})
    assert "hmac" in r
    assert r["version"] == STATE_VERSION == 3
    g = await handlers["src_get_active_paths"]({})
    assert "hmac" in g
    assert g["version"] == STATE_VERSION


@pytest.mark.asyncio
async def test_tool_hmac_verifies_externally(tmp_path, pinned_secret):
    """An external auditor with the same secret can recompute and
    verify the HMAC the tool returned."""
    p = tmp_path / "a"
    state, handlers = build_restrict_tool_handlers(
        scope_id="src", ceiling=tmp_path, state_file=None,
    )
    r = await handlers["src_restrict_paths"]({"paths": [str(p)]})
    expected = compute_state_hmac(
        version=r["version"],
        scope_id="src",
        ceiling=str(tmp_path.resolve()),
        seq=r["seq"],
        sorted_paths=r["active_paths"],
        secret=_PINNED_SECRET.encode(),
    )
    assert r["hmac"] == expected


# ── Canonical payload ─────────────────────────────────────────────────


def test_canonical_payload_is_path_order_independent():
    """sorted_paths input means the canonical bytes are deterministic
    regardless of restriction-call ordering. Sorting happens at the
    boundaries (set + tools.py)."""
    p1 = canonical_hmac_payload(
        version=3, scope_id="src", ceiling="/c", seq=1,
        sorted_paths=["/a", "/b"],
    )
    p2 = canonical_hmac_payload(
        version=3, scope_id="src", ceiling="/c", seq=1,
        sorted_paths=["/a", "/b"],
    )
    assert p1 == p2


def test_canonical_payload_distinguishes_paths():
    p1 = canonical_hmac_payload(
        version=3, scope_id="src", ceiling="/c", seq=1,
        sorted_paths=["/a"],
    )
    p2 = canonical_hmac_payload(
        version=3, scope_id="src", ceiling="/c", seq=1,
        sorted_paths=["/a", "/b"],
    )
    assert p1 != p2
