"""MCP-protocol-level integration tests for the fs server.

Spawns ``hares-mcp --enable=fs ...`` as a subprocess, talks to it
via the official mcp client (real JSON-RPC over stdio), and asserts
the externally-visible behavior. Complements the in-process unit
tests in test_fs_operations.py and test_restrict_tools.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._proto_helpers import hares_session, parse_text_result


@pytest.mark.asyncio
async def test_fs_lists_read_and_write_tools(tmp_path):
    async with hares_session(enable="fs", ceiling=tmp_path) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    # Reads
    assert "read_file" in names
    assert "list_directory" in names
    # Writes (no --read-only, so present)
    assert "write_file" in names
    assert "edit_file" in names
    # Restrict tools always present
    assert "restrict_paths" in names
    assert "get_active_paths" in names


@pytest.mark.asyncio
async def test_fs_read_only_suppresses_writes(tmp_path):
    async with hares_session(enable="fs", ceiling=tmp_path, read_only=True) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    assert "read_file" in names           # reads still there
    assert "write_file" not in names      # writes suppressed
    assert "edit_file" not in names
    assert "create_directory" not in names
    assert "move_file" not in names
    # Restrict still registered (architect can still narrow reads' visibility... wait, reads use ceiling not scope; still registered for symmetry)
    assert "restrict_paths" in names


@pytest.mark.asyncio
async def test_fs_scope_id_prefixes_all_tools(tmp_path):
    async with hares_session(enable="fs", scope_id="src", ceiling=tmp_path) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    # Every name MUST start with src_; nothing unprefixed leaks.
    assert all(n.startswith("src_") for n in names), names
    assert "src_read_file" in names
    assert "src_write_file" in names
    assert "src_restrict_paths" in names


@pytest.mark.asyncio
async def test_fs_write_then_read_round_trip(tmp_path):
    async with hares_session(enable="fs", ceiling=tmp_path) as s:
        # Restrict to tmp_path itself so write_file is allowed.
        await s.call_tool("restrict_paths", {"paths": [str(tmp_path)]})
        target = tmp_path / "hello.txt"
        wrote = await s.call_tool(
            "write_file", {"path": str(target), "content": "hi there"}
        )
        wrote_payload = parse_text_result(wrote)
        assert "bytes_written" in wrote_payload or "path" in wrote_payload
        assert target.exists()
        assert target.read_text() == "hi there"
        # Read it back
        read = await s.call_tool("read_file", {"path": str(target)})
        read_payload = parse_text_result(read)
        # operations.py returns {"path": ..., "content": ...} for read_file
        assert read_payload["content"] == "hi there"


@pytest.mark.asyncio
async def test_fs_write_outside_active_scope_rejected(tmp_path):
    inside = tmp_path / "ok"; inside.mkdir()
    outside = tmp_path / "nope"; outside.mkdir()
    async with hares_session(enable="fs", ceiling=tmp_path) as s:
        await s.call_tool("restrict_paths", {"paths": [str(inside)]})
        # Writing under inside/ — fine.
        await s.call_tool(
            "write_file", {"path": str(inside / "f.txt"), "content": "yes"}
        )
        # Writing under outside/ — must error (MCP returns isError=True).
        bad = await s.call_tool(
            "write_file", {"path": str(outside / "f.txt"), "content": "no"}
        )
        assert bad.isError, f"expected error, got {bad}"


@pytest.mark.asyncio
async def test_fs_read_outside_ceiling_rejected(tmp_path):
    async with hares_session(enable="fs", ceiling=tmp_path) as s:
        # Try to read /etc/passwd — outside ceiling, must fail.
        bad = await s.call_tool("read_file", {"path": "/etc/passwd"})
        assert bad.isError


@pytest.mark.asyncio
async def test_fs_get_active_paths_round_trip(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    async with hares_session(enable="fs", ceiling=tmp_path) as s:
        await s.call_tool("restrict_paths", {"paths": [str(a), str(b)]})
        got = await s.call_tool("get_active_paths", {})
        payload = parse_text_result(got)
        assert {Path(p) for p in payload["active_paths"]} == {a.resolve(), b.resolve()}
