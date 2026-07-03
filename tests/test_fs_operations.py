"""Tests for hares.fs.operations — read/write/etc. with active-scope
+ ceiling validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from hares.fs.operations import (
    ScopeViolationError,
    create_directory,
    directory_tree,
    edit_file,
    list_directory,
    move_file,
    read_file,
    search_files,
    write_file,
)
from hares.fs.state import ActiveScope
from hares.grants import GrantStore
from hares.path_safety import PathDeniedError, PathSafetyError


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


# ── In-ceiling blacklist: EXCLUDE (hide for read + write) ──────────────


@pytest.mark.asyncio
async def test_excluded_file_read_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_SANDBOX_EXCLUDE", "secrets")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "key").write_text("TOPSECRET")
    with pytest.raises(PathDeniedError, match="excluded"):
        await read_file({"path": "secrets/key"}, ceiling=tmp_path, scope=_scope())


@pytest.mark.asyncio
async def test_excluded_dir_pruned_from_list_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_SANDBOX_EXCLUDE", "secrets")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "a.txt").write_text("")
    result = await list_directory({"path": "."}, ceiling=tmp_path, scope=_scope())
    names = {e["name"] for e in result["entries"]}
    assert names == {"src", "a.txt"}  # secrets hidden


@pytest.mark.asyncio
async def test_excluded_dir_pruned_from_directory_tree(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_SANDBOX_EXCLUDE", "secrets")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "k").write_text("")
    (tmp_path / "src").mkdir()
    result = await directory_tree({"path": "."}, ceiling=tmp_path, scope=_scope())
    child_names = {c["name"] for c in result["tree"]["children"]}
    assert "secrets" not in child_names
    assert "src" in child_names


@pytest.mark.asyncio
async def test_excluded_dir_pruned_from_search(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_SANDBOX_EXCLUDE", "secrets")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "leak.py").write_text("")
    (tmp_path / "keep.py").write_text("")
    result = await search_files(
        {"path": ".", "pattern": "*.py"}, ceiling=tmp_path, scope=_scope(),
    )
    names = {Path(p).name for p in result["matches"]}
    assert names == {"keep.py"}  # leak.py not surfaced


@pytest.mark.asyncio
async def test_excluded_write_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_SANDBOX_EXCLUDE", "secrets")
    (tmp_path / "secrets").mkdir()
    with pytest.raises(PathDeniedError, match="excluded"):
        await write_file(
            {"path": "secrets/new", "content": "x"},
            ceiling=tmp_path, scope=_scope(),
        )


# ── In-ceiling blacklist: PROTECT (read-only) ──────────────────────────


@pytest.mark.asyncio
async def test_protected_read_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_SANDBOX_PROTECT", "vendor")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "lib.py").write_text("orig")
    result = await read_file(
        {"path": "vendor/lib.py"}, ceiling=tmp_path, scope=_scope(),
    )
    assert result["content"] == "orig"


@pytest.mark.asyncio
async def test_protected_write_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("HARES_SANDBOX_PROTECT", "vendor")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    with pytest.raises(PathDeniedError, match="protected"):
        await write_file(
            {"path": "vendor/lib.py", "content": "mutated"},
            ceiling=tmp_path, scope=_scope(),
        )


@pytest.mark.asyncio
async def test_protected_write_rejected_even_inside_active_scope(monkeypatch, tmp_path):
    """Precedence: deny beats allow — protect blocks the write even when
    the active scope explicitly includes the protected path."""
    monkeypatch.setenv("HARES_SANDBOX_PROTECT", "vendor")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    with pytest.raises(PathDeniedError, match="protected"):
        await write_file(
            {"path": "vendor/lib.py", "content": "mutated"},
            ceiling=tmp_path, scope=_scope(vendor),
        )


@pytest.mark.asyncio
async def test_exclude_beats_protect_on_read(monkeypatch, tmp_path):
    """A path in BOTH lists is hidden (exclude wins) — even reads fail."""
    monkeypatch.setenv("HARES_SANDBOX_EXCLUDE", "both")
    monkeypatch.setenv("HARES_SANDBOX_PROTECT", "both")
    both = tmp_path / "both"
    both.mkdir()
    (both / "f").write_text("x")
    with pytest.raises(PathDeniedError, match="excluded"):
        await read_file({"path": "both/f"}, ceiling=tmp_path, scope=_scope())


# ── request_path_access grants — fs enforcement ─────────────────────────
#
# These exercise the grant fallback inside hares.fs.operations._resolve
# directly (unit-level, no MCP round-trip — the request_path_access
# tool handler itself is covered by test_request_path_access_proto.py).


@pytest.mark.asyncio
async def test_outside_ceiling_read_rejected_without_grant(tmp_path):
    ceiling = tmp_path / "proj"; ceiling.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "f.txt").write_text("secret")
    with pytest.raises(PathSafetyError, match="not under ceiling"):
        await read_file(
            {"path": str(outside / "f.txt")}, ceiling=ceiling, scope=_scope(),
        )


@pytest.mark.asyncio
async def test_ro_grant_allows_read_outside_ceiling(tmp_path):
    ceiling = tmp_path / "proj"; ceiling.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "f.txt").write_text("hello")
    grants = GrantStore()
    grants.add(outside, "ro", "session")
    result = await read_file(
        {"path": str(outside / "f.txt")}, ceiling=ceiling, scope=_scope(),
        grants=grants,
    )
    assert result["content"] == "hello"


@pytest.mark.asyncio
async def test_ro_grant_does_not_allow_write_outside_ceiling(tmp_path):
    ceiling = tmp_path / "proj"; ceiling.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    grants = GrantStore()
    grants.add(outside, "ro", "session")
    with pytest.raises(PathSafetyError, match="not under ceiling"):
        await write_file(
            {"path": str(outside / "f.txt"), "content": "nope"},
            ceiling=ceiling, scope=_scope(), grants=grants,
        )


@pytest.mark.asyncio
async def test_rw_grant_allows_both_read_and_write_outside_ceiling(tmp_path):
    ceiling = tmp_path / "proj"; ceiling.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    grants = GrantStore()
    grants.add(outside, "rw", "session")
    await write_file(
        {"path": str(outside / "f.txt"), "content": "written"},
        ceiling=ceiling, scope=_scope(), grants=grants,
    )
    result = await read_file(
        {"path": str(outside / "f.txt")}, ceiling=ceiling, scope=_scope(),
        grants=grants,
    )
    assert result["content"] == "written"


@pytest.mark.asyncio
async def test_directory_grant_covers_nested_file(tmp_path):
    ceiling = tmp_path / "proj"; ceiling.mkdir()
    outside = tmp_path / "outside"
    nested_dir = outside / "a" / "b"
    nested_dir.mkdir(parents=True)
    (nested_dir / "deep.txt").write_text("deep-content")
    grants = GrantStore()
    grants.add(outside, "ro", "session")
    result = await read_file(
        {"path": str(nested_dir / "deep.txt")}, ceiling=ceiling, scope=_scope(),
        grants=grants,
    )
    assert result["content"] == "deep-content"


@pytest.mark.asyncio
async def test_once_grant_consumed_after_one_read(tmp_path):
    ceiling = tmp_path / "proj"; ceiling.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    target = outside / "f.txt"
    target.write_text("once-only")
    grants = GrantStore()
    grants.add(outside, "ro", "once")
    # First read succeeds and consumes the grant.
    result = await read_file(
        {"path": str(target)}, ceiling=ceiling, scope=_scope(), grants=grants,
    )
    assert result["content"] == "once-only"
    assert grants.list_active() == []
    # Second read is now rejected — the grant is gone.
    with pytest.raises(PathSafetyError, match="not under ceiling"):
        await read_file(
            {"path": str(target)}, ceiling=ceiling, scope=_scope(), grants=grants,
        )


@pytest.mark.asyncio
async def test_session_grant_persists_across_multiple_reads(tmp_path):
    ceiling = tmp_path / "proj"; ceiling.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    target = outside / "f.txt"
    target.write_text("session-content")
    grants = GrantStore()
    grants.add(outside, "ro", "session")
    for _ in range(3):
        result = await read_file(
            {"path": str(target)}, ceiling=ceiling, scope=_scope(), grants=grants,
        )
        assert result["content"] == "session-content"
    assert len(grants.list_active()) == 1


@pytest.mark.asyncio
async def test_grant_cannot_open_excluded_path(monkeypatch, tmp_path):
    """Deny beats grant, always — even a covering grant does not
    authorize a path that HARES_SANDBOX_EXCLUDE denies. This exercises
    the defense-in-depth re-check inside _resolve's grant branch (the
    grant target itself, an absolute path outside the ceiling, was
    never validated against this ceiling's excludelist before — this
    proves the use-time re-validation, not just grant-creation time)."""
    ceiling = tmp_path / "proj"; ceiling.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "f.txt").write_text("secret")
    monkeypatch.setenv("HARES_DISALLOW_SYSTEM_DIRS", "1")
    monkeypatch.setenv("HARES_EXTRA_SYSTEM_DIRS", str(outside))
    grants = GrantStore()
    grants.add(outside, "ro", "session")
    with pytest.raises(PathSafetyError, match="system directory"):
        await read_file(
            {"path": str(outside / "f.txt")}, ceiling=ceiling, scope=_scope(),
            grants=grants,
        )


@pytest.mark.asyncio
async def test_move_file_dst_outside_ceiling_requires_rw_grant(tmp_path):
    ceiling = tmp_path / "proj"; ceiling.mkdir()
    src = ceiling / "src.txt"
    src.write_text("payload")
    outside = tmp_path / "outside"; outside.mkdir()
    dst = outside / "dst.txt"
    grants = GrantStore()
    grants.add(outside, "rw", "session")
    await move_file(
        {"source": "src.txt", "destination": str(dst)},
        ceiling=ceiling, scope=_scope(ceiling), grants=grants,
    )
    assert dst.read_text() == "payload"
    assert not src.exists()
