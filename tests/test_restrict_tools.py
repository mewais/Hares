"""Tests for the shared restrict_paths / get_active_paths
tools. Lives in hares.fs.tools but is consumed by both fs and shell
servers; tests exercise the dispatcher directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hares.fs.state import ScopeStateStore
from hares.fs.tools import (
    build_restrict_tool_handlers,
    restrict_tool_descriptors,
)


def test_descriptors_unprefixed_when_no_scope_id():
    descs = restrict_tool_descriptors(scope_id=None)
    names = {d.name for d in descs}
    assert names == {"restrict_paths", "get_active_paths"}


def test_descriptors_prefixed_when_scope_id_set():
    descs = restrict_tool_descriptors(scope_id="src")
    names = {d.name for d in descs}
    assert names == {"src_restrict_paths", "src_get_active_paths"}


@pytest.mark.asyncio
async def test_restrict_paths_updates_active_scope(tmp_path):
    p1 = tmp_path / "lib" / "parser"  # doesn't exist yet — restrict mkdir-p's
    p2 = tmp_path / "lib" / "lexer"
    state, handlers = build_restrict_tool_handlers(
        scope_id="src", ceiling=tmp_path, state_file=None,
    )
    restrict = handlers["src_restrict_paths"]
    get_active = handlers["src_get_active_paths"]
    result = await restrict({"paths": [str(p1), str(p2)]})
    # mkdir-p side effect.
    assert p1.exists() and p2.exists()
    # Tool returns the resolved active paths.
    assert {Path(p) for p in result["active_paths"]} == {p1.resolve(), p2.resolve()}
    # get_active_paths echoes.
    g = await get_active({})
    assert {Path(p) for p in g["active_paths"]} == {p1.resolve(), p2.resolve()}


@pytest.mark.asyncio
async def test_restrict_paths_set_replace_semantics(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    c = tmp_path / "c"; c.mkdir()
    state, handlers = build_restrict_tool_handlers(
        scope_id=None, ceiling=tmp_path, state_file=None,
    )
    restrict = handlers["restrict_paths"]
    get_active = handlers["get_active_paths"]
    await restrict({"paths": [str(a), str(b)]})
    await restrict({"paths": [str(c)]})
    g = await get_active({})
    assert {Path(p) for p in g["active_paths"]} == {c.resolve()}


@pytest.mark.asyncio
async def test_restrict_paths_traversal_rejected(tmp_path):
    state, handlers = build_restrict_tool_handlers(
        scope_id=None, ceiling=tmp_path, state_file=None,
    )
    from hares.path_safety import PathSafetyError
    with pytest.raises(PathSafetyError, match="'..'"):
        await handlers["restrict_paths"]({"paths": ["../escape"]})


@pytest.mark.asyncio
async def test_restrict_paths_outside_ceiling_rejected(tmp_path):
    state, handlers = build_restrict_tool_handlers(
        scope_id=None, ceiling=tmp_path, state_file=None,
    )
    other = tmp_path.parent / "outside"
    from hares.path_safety import PathSafetyError
    with pytest.raises(PathSafetyError, match="not under ceiling"):
        await handlers["restrict_paths"]({"paths": [str(other)]})


@pytest.mark.asyncio
async def test_restrict_paths_persists_to_state_file(tmp_path):
    state_file = tmp_path / "state.json"
    a = tmp_path / "a"; a.mkdir()
    state, handlers = build_restrict_tool_handlers(
        scope_id="src", ceiling=tmp_path, state_file=state_file,
    )
    await handlers["src_restrict_paths"]({"paths": [str(a)]})
    assert state_file.exists()
    saved = json.loads(state_file.read_text())
    assert saved["scope_id"] == "src"
    assert str(a.resolve()) in saved["active_paths"]


@pytest.mark.asyncio
async def test_on_change_callback_fires(tmp_path):
    fired = []

    def cb():
        fired.append("called")

    state, handlers = build_restrict_tool_handlers(
        scope_id=None, ceiling=tmp_path, state_file=None,
        on_change=cb,
    )
    a = tmp_path / "a"; a.mkdir()
    await handlers["restrict_paths"]({"paths": [str(a)]})
    assert fired == ["called"]
