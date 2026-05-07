"""Tests for the 0.2.1 sequence-numbered restrict_paths feature.

Covers:
  - seq starts at 0 for a fresh ScopeStateStore
  - set() increments seq by 1
  - seq survives state-file persistence round-trip
  - expected_seq mismatch raises ScopeSeqMismatch (in-process)
  - restrict_paths tool returns seq in the success reply
  - get_active_paths tool returns seq
  - restrict_paths tool with mismatched expected_seq returns the
    structured scope_seq_mismatch error
  - restrict_paths tool with matching expected_seq succeeds + bumps
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hares.fs.state import (
    ActiveScope,
    ScopeSeqMismatch,
    ScopeStateStore,
    STATE_VERSION,
)
from hares.fs.tools import build_restrict_tool_handlers


def test_fresh_store_has_seq_zero(tmp_path):
    store = ScopeStateStore(scope_id="src", ceiling=tmp_path, state_file=None)
    assert store.current().seq == 0


def test_set_increments_seq(tmp_path):
    store = ScopeStateStore(scope_id="src", ceiling=tmp_path, state_file=None)
    p1 = tmp_path / "a"; p1.mkdir()
    p2 = tmp_path / "b"; p2.mkdir()
    s1 = store.set([p1])
    assert s1.seq == 1
    s2 = store.set([p2])
    assert s2.seq == 2
    s3 = store.set([])
    assert s3.seq == 3


def test_seq_round_trips_via_state_file(tmp_path):
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    store1 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    store1.set([p])
    store1.set([p])  # seq now 2
    saved = json.loads(state_file.read_text())
    assert saved["version"] == STATE_VERSION == 3  # 0.2.2 bump
    assert saved["seq"] == 2
    assert "hmac" in saved  # 0.2.2: signed payload
    # Reload sees seq=2.
    store2 = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store2.current().seq == 2
    # Next set() continues the sequence.
    s3 = store2.set([p])
    assert s3.seq == 3


def test_expected_seq_mismatch_raises(tmp_path):
    store = ScopeStateStore(scope_id="src", ceiling=tmp_path, state_file=None)
    p = tmp_path / "a"; p.mkdir()
    store.set([p])  # seq -> 1
    with pytest.raises(ScopeSeqMismatch) as excinfo:
        store.set([p], expected_seq=99)
    assert excinfo.value.expected == 99
    assert excinfo.value.actual == 1
    # State unchanged on mismatch.
    assert store.current().seq == 1


def test_expected_seq_match_succeeds(tmp_path):
    store = ScopeStateStore(scope_id="src", ceiling=tmp_path, state_file=None)
    p = tmp_path / "a"; p.mkdir()
    store.set([p])  # seq -> 1
    s = store.set([p], expected_seq=1)
    assert s.seq == 2


def test_malformed_seq_in_state_file_resets_to_zero(tmp_path, caplog):
    """0.2.2: malformed seq is now a hard reject (was: reset seq to 0
    while keeping paths). v3 makes seq mandatory + signed; a hand-
    edited file with a non-int seq fails parsing and the load returns
    None → empty scope."""
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    state_file.write_text(json.dumps({
        "version": STATE_VERSION,
        "scope_id": "src",
        "ceiling": str(tmp_path.resolve()),
        "active_paths": [str(p.resolve())],
        "seq": "not-an-int",
        "hmac": "0" * 64,  # not actually verified — parse fails first
        "last_restrict_at": None,
    }))
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store.current().seq == 0
    assert store.current().paths == []
    assert any("malformed seq" in r.message for r in caplog.records)


def test_negative_seq_in_state_file_resets_to_zero(tmp_path, caplog):
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    state_file.write_text(json.dumps({
        "version": STATE_VERSION,
        "scope_id": "src",
        "ceiling": str(tmp_path.resolve()),
        "active_paths": [str(p.resolve())],
        "seq": -5,
        "hmac": "0" * 64,
        "last_restrict_at": None,
    }))
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store.current().seq == 0
    assert any("malformed seq" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_tool_restrict_paths_returns_seq(tmp_path):
    p = tmp_path / "a"
    state, handlers = build_restrict_tool_handlers(
        scope_id=None, ceiling=tmp_path, state_file=None,
    )
    result = await handlers["restrict_paths"]({"paths": [str(p)]})
    assert result["seq"] == 1
    result2 = await handlers["restrict_paths"]({"paths": [str(p)]})
    assert result2["seq"] == 2


@pytest.mark.asyncio
async def test_tool_get_active_paths_returns_seq(tmp_path):
    p = tmp_path / "a"
    state, handlers = build_restrict_tool_handlers(
        scope_id=None, ceiling=tmp_path, state_file=None,
    )
    g0 = await handlers["get_active_paths"]({})
    assert g0["seq"] == 0
    await handlers["restrict_paths"]({"paths": [str(p)]})
    g1 = await handlers["get_active_paths"]({})
    assert g1["seq"] == 1


@pytest.mark.asyncio
async def test_tool_expected_seq_mismatch_returns_structured_error(tmp_path):
    p = tmp_path / "a"
    state, handlers = build_restrict_tool_handlers(
        scope_id=None, ceiling=tmp_path, state_file=None,
    )
    await handlers["restrict_paths"]({"paths": [str(p)]})  # seq -> 1
    result = await handlers["restrict_paths"]({
        "paths": [str(p)],
        "expected_seq": 99,
    })
    assert result["error"] == "scope_seq_mismatch"
    assert result["expected_seq"] == 99
    assert result["current_seq"] == 1
    assert "active_paths" not in result
    # Scope unchanged.
    g = await handlers["get_active_paths"]({})
    assert g["seq"] == 1


@pytest.mark.asyncio
async def test_tool_expected_seq_match_succeeds_and_bumps(tmp_path):
    p = tmp_path / "a"
    state, handlers = build_restrict_tool_handlers(
        scope_id=None, ceiling=tmp_path, state_file=None,
    )
    await handlers["restrict_paths"]({"paths": [str(p)]})  # seq -> 1
    result = await handlers["restrict_paths"]({
        "paths": [str(p)],
        "expected_seq": 1,
    })
    assert result["seq"] == 2
    assert "error" not in result


@pytest.mark.asyncio
async def test_tool_expected_seq_must_be_int(tmp_path):
    p = tmp_path / "a"
    state, handlers = build_restrict_tool_handlers(
        scope_id=None, ceiling=tmp_path, state_file=None,
    )
    with pytest.raises(ValueError, match="expected_seq.*integer"):
        await handlers["restrict_paths"]({
            "paths": [str(p)],
            "expected_seq": "not-an-int",
        })


@pytest.mark.asyncio
async def test_tool_expected_seq_must_be_non_negative(tmp_path):
    p = tmp_path / "a"
    state, handlers = build_restrict_tool_handlers(
        scope_id=None, ceiling=tmp_path, state_file=None,
    )
    with pytest.raises(ValueError, match="expected_seq.*>= 0"):
        await handlers["restrict_paths"]({
            "paths": [str(p)],
            "expected_seq": -1,
        })


def test_v1_state_file_falls_back_via_version_mismatch(tmp_path, caplog):
    """0.2.0 state files (version=1) should trigger the existing
    version-mismatch warn-and-rebuild — backward-compat is via reset,
    not migration. Documented in the CHANGELOG."""
    state_file = tmp_path / "state.json"
    p = tmp_path / "x"; p.mkdir()
    state_file.write_text(json.dumps({
        "version": 1,  # 0.2.0 format
        "scope_id": "src",
        "ceiling": str(tmp_path.resolve()),
        "active_paths": [str(p.resolve())],
        "last_restrict_at": None,
    }))
    store = ScopeStateStore(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    assert store.current().paths == []
    assert store.current().seq == 0
    assert any("version mismatch" in r.message for r in caplog.records)
