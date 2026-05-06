"""Tests for hares.fs.operations — read/write/etc. with active-scope
+ ceiling validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from hares.fs.operations import (
    ScopeViolationError,
    create_directory,
    edit_file,
    list_directory,
    move_file,
    read_file,
    search_files,
    write_file,
)
from hares.fs.state import ActiveScope


def _scope(*paths: Path) -> ActiveScope:
    return ActiveScope(paths=list(paths))


# ── Reads bounded by ceiling alone (active scope ignored for reads) ────


@pytest.mark.asyncio
async def test_read_file_inside_ceiling(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello")
    # Empty active scope is fine for reads.
    result = await read_file({"path": "x.txt"}, ceiling=tmp_path, scope=_scope())
    assert result["content"] == "hello"


@pytest.mark.asyncio
async def test_read_outside_ceiling_rejected(tmp_path):
    other = tmp_path.parent / "outside.txt"
    other.write_text("nope")
    from hares.path_safety import PathSafetyError
    with pytest.raises(PathSafetyError, match="not under ceiling"):
        await read_file({"path": str(other)}, ceiling=tmp_path, scope=_scope())


@pytest.mark.asyncio
async def test_list_directory(tmp_path):
    (tmp_path / "a").write_text("")
    (tmp_path / "b").mkdir()
    result = await list_directory({"path": "."}, ceiling=tmp_path, scope=_scope())
    names = {e["name"] for e in result["entries"]}
    assert names == {"a", "b"}


@pytest.mark.asyncio
async def test_search_files_glob(tmp_path):
    (tmp_path / "foo.py").write_text("")
    (tmp_path / "bar.py").write_text("")
    (tmp_path / "baz.txt").write_text("")
    result = await search_files(
        {"path": ".", "pattern": "*.py"}, ceiling=tmp_path, scope=_scope(),
    )
    names = {Path(p).name for p in result["matches"]}
    assert names == {"foo.py", "bar.py"}


# ── Writes bounded by ceiling AND active scope ─────────────────────────


@pytest.mark.asyncio
async def test_write_inside_active_scope(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    await write_file(
        {"path": "sub/new.txt", "content": "data"},
        ceiling=tmp_path, scope=_scope(sub),
    )
    assert (sub / "new.txt").read_text() == "data"


@pytest.mark.asyncio
async def test_write_outside_active_scope_rejected(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    with pytest.raises(ScopeViolationError, match="outside the active scope"):
        await write_file(
            {"path": "elsewhere.txt", "content": "data"},
            ceiling=tmp_path, scope=_scope(sub),
        )


@pytest.mark.asyncio
async def test_write_with_empty_scope_uses_ceiling(tmp_path):
    """When the active scope is empty (no restrict ever called),
    writes are bounded by ceiling alone — not blocked entirely."""
    await write_file(
        {"path": "anywhere.txt", "content": "data"},
        ceiling=tmp_path, scope=_scope(),
    )
    assert (tmp_path / "anywhere.txt").read_text() == "data"


@pytest.mark.asyncio
async def test_write_traversal_rejected(tmp_path):
    sub = tmp_path / "sub"; sub.mkdir()
    from hares.path_safety import PathSafetyError
    with pytest.raises(PathSafetyError, match="'..'"):
        await write_file(
            {"path": "../escape.txt", "content": "x"},
            ceiling=tmp_path, scope=_scope(sub),
        )


@pytest.mark.asyncio
async def test_write_creates_parent_directories(tmp_path):
    sub = tmp_path / "sub"; sub.mkdir()
    await write_file(
        {"path": "sub/a/b/c/deep.txt", "content": "data"},
        ceiling=tmp_path, scope=_scope(sub),
    )
    assert (sub / "a" / "b" / "c" / "deep.txt").read_text() == "data"


@pytest.mark.asyncio
async def test_edit_file_single_match(tmp_path):
    sub = tmp_path / "sub"; sub.mkdir()
    f = sub / "x.txt"
    f.write_text("hello world")
    await edit_file(
        {"path": "sub/x.txt", "edits": [{"oldText": "world", "newText": "there"}]},
        ceiling=tmp_path, scope=_scope(sub),
    )
    assert f.read_text() == "hello there"


@pytest.mark.asyncio
async def test_edit_file_no_match_raises(tmp_path):
    sub = tmp_path / "sub"; sub.mkdir()
    f = sub / "x.txt"
    f.write_text("hello world")
    with pytest.raises(ValueError, match="not found"):
        await edit_file(
            {"path": "sub/x.txt", "edits": [{"oldText": "missing", "newText": "x"}]},
            ceiling=tmp_path, scope=_scope(sub),
        )


@pytest.mark.asyncio
async def test_edit_file_ambiguous_raises(tmp_path):
    sub = tmp_path / "sub"; sub.mkdir()
    f = sub / "x.txt"
    f.write_text("foo bar foo")
    with pytest.raises(ValueError, match="matches 2 times"):
        await edit_file(
            {"path": "sub/x.txt", "edits": [{"oldText": "foo", "newText": "X"}]},
            ceiling=tmp_path, scope=_scope(sub),
        )


@pytest.mark.asyncio
async def test_create_directory(tmp_path):
    sub = tmp_path / "sub"; sub.mkdir()
    await create_directory(
        {"path": "sub/new"},
        ceiling=tmp_path, scope=_scope(sub),
    )
    assert (sub / "new").is_dir()


@pytest.mark.asyncio
async def test_move_file_both_endpoints_in_scope(tmp_path):
    sub = tmp_path / "sub"; sub.mkdir()
    src = sub / "a.txt"
    src.write_text("data")
    await move_file(
        {"source": "sub/a.txt", "destination": "sub/b.txt"},
        ceiling=tmp_path, scope=_scope(sub),
    )
    assert not src.exists()
    assert (sub / "b.txt").read_text() == "data"


@pytest.mark.asyncio
async def test_move_file_destination_outside_scope_rejected(tmp_path):
    sub = tmp_path / "sub"; sub.mkdir()
    src = sub / "a.txt"
    src.write_text("data")
    with pytest.raises(ScopeViolationError, match="outside the active scope"):
        await move_file(
            {"source": "sub/a.txt", "destination": "elsewhere.txt"},
            ceiling=tmp_path, scope=_scope(sub),
        )
