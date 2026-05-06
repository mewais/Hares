"""MCP-protocol-level integration tests for the combined fs+shell server.

The combined mode is the value-prop of the unified Hares: ONE process,
ONE scope state, both tool families. A single restrict call narrows
fs path validation AND shell bwrap mounts together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._proto_helpers import hares_session, parse_text_result


@pytest.mark.asyncio
async def test_combined_exposes_union_of_tools(tmp_path):
    async with hares_session(enable="fs+shell", scope_id="block_x", ceiling=tmp_path) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    # Every name prefixed.
    assert all(n.startswith("block_x_") for n in names), names
    # FS reads + writes
    assert "block_x_read_file" in names
    assert "block_x_write_file" in names
    # Shell
    assert "block_x_execute_command" in names
    # Restrict (single shared instance)
    assert "block_x_restrict_paths" in names
    assert "block_x_get_active_paths" in names


@pytest.mark.asyncio
async def test_combined_one_restrict_updates_both_layers(tmp_path):
    """The combined server's contract: a single restrict_paths
    call narrows BOTH the fs write validator AND the shell bwrap mount
    list. We assert the fs side directly (writes outside scope error)
    and verify shell shares the same get_active_paths view."""
    inside = tmp_path / "lib"; inside.mkdir()
    outside = tmp_path / "elsewhere"; outside.mkdir()
    async with hares_session(enable="fs+shell", scope_id="b", ceiling=tmp_path) as s:
        await s.call_tool(
            "b_restrict_paths", {"paths": [str(inside)]},
        )
        # FS write inside scope: ok
        ok = await s.call_tool(
            "b_write_file",
            {"path": str(inside / "f.txt"), "content": "hello"},
        )
        assert not ok.isError
        # FS write outside scope: must error
        bad = await s.call_tool(
            "b_write_file",
            {"path": str(outside / "f.txt"), "content": "nope"},
        )
        assert bad.isError
        # get_active_paths reflects the shared scope
        got = await s.call_tool("b_get_active_paths", {})
        payload = parse_text_result(got)
        assert {Path(p) for p in payload["active_paths"]} == {inside.resolve()}


@pytest.mark.asyncio
async def test_combined_read_only_suppresses_fs_writes_keeps_shell(tmp_path):
    async with hares_session(
        enable="fs+shell", scope_id="ro", ceiling=tmp_path, read_only=True,
    ) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    # Reads + shell + restrict survive; fs writes gone.
    assert "ro_read_file" in names
    assert "ro_execute_command" in names
    assert "ro_restrict_paths" in names
    assert "ro_write_file" not in names
    assert "ro_edit_file" not in names
