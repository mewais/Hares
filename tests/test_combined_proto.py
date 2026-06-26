"""MCP-protocol-level integration tests for the combined fs+shell server.

The combined mode is the value-prop of the unified Hares: ONE process,
ONE scope state, both tool families. A single restrict call narrows
fs path validation AND shell bwrap mounts together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._proto_helpers import (
    hares_session,
    parse_text_result,
    _accept_elicitation_callback,
    _decline_elicitation_callback,
)


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


# ── execute_command_high_memory tool (combined mode) ──────────────────────────

@pytest.mark.asyncio
async def test_combined_high_memory_tool_appears_with_prefix(tmp_path):
    """The high-memory tool must be included in the combined server's tool list."""
    async with hares_session(enable="fs+shell", scope_id="block_x", ceiling=tmp_path) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    assert "block_x_execute_command_high_memory" in names
    # All names must carry the prefix.
    assert all(n.startswith("block_x_") for n in names), names


@pytest.mark.asyncio
async def test_combined_high_memory_tool_appears_unprefixed(tmp_path):
    """High-memory tool present without scope_id in combined mode."""
    async with hares_session(enable="fs+shell", ceiling=tmp_path) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    assert "execute_command_high_memory" in names


@pytest.mark.asyncio
async def test_combined_high_memory_tool_schema_has_command(tmp_path):
    """The combined high-memory tool schema must include 'command'."""
    async with hares_session(enable="fs+shell", ceiling=tmp_path) as s:
        tools = await s.list_tools()
    tool_map = {t.name: t for t in tools.tools}
    hm_tool = tool_map["execute_command_high_memory"]
    assert "command" in hm_tool.inputSchema.get("properties", {}), (
        f"'command' missing from schema properties: {hm_tool.inputSchema}"
    )


@pytest.mark.asyncio
async def test_combined_high_memory_no_elicitation_support_fails_closed(tmp_path):
    """Without elicitation support the high-memory tool must fail closed."""
    async with hares_session(enable="fs+shell", ceiling=tmp_path) as s:
        result = await s.call_tool(
            "execute_command_high_memory",
            {"command": "echo SHOULD_NOT_RUN", "timeout": 10},
        )
        payload = parse_text_result(result)
    assert payload.get("killed_reason") == "rejected_by_policy", payload
    assert payload.get("exit_code") == -1, payload
    assert "SHOULD_NOT_RUN" not in (payload.get("stdout") or ""), payload


@pytest.mark.asyncio
async def test_combined_high_memory_elicitation_decline_fails_closed(tmp_path):
    """An explicit decline must fail closed in combined mode."""
    async with hares_session(
        enable="fs+shell", ceiling=tmp_path,
        elicitation_callback=_decline_elicitation_callback,
    ) as s:
        result = await s.call_tool(
            "execute_command_high_memory",
            {"command": "echo SHOULD_NOT_RUN", "timeout": 10},
        )
        payload = parse_text_result(result)
    assert payload.get("killed_reason") == "rejected_by_policy", payload
    assert payload.get("exit_code") == -1, payload
    assert "SHOULD_NOT_RUN" not in (payload.get("stdout") or ""), payload


@pytest.mark.asyncio
async def test_combined_high_memory_elicitation_accept_dispatches(tmp_path):
    """An accepted elicitation in combined mode must dispatch the command."""
    async with hares_session(
        enable="fs+shell", ceiling=tmp_path,
        elicitation_callback=_accept_elicitation_callback,
    ) as s:
        result = await s.call_tool(
            "execute_command_high_memory",
            {"command": "echo hi-from-combined-high-memory", "timeout": 10},
        )
        payload = parse_text_result(result)
    assert "hi-from-combined-high-memory" in (payload.get("stdout") or ""), payload


@pytest.mark.asyncio
async def test_combined_high_memory_no_bypass_via_arguments(tmp_path):
    """Security: no argument bypasses the high-memory elicitation gate."""
    async with hares_session(enable="fs+shell", ceiling=tmp_path) as s:
        result = await s.call_tool(
            "execute_command_high_memory",
            {
                "command": "echo BYPASSED",
                "mem_limit_mb": 512,
                "cpu_limit_sec": 30,
                "timeout": 10,
            },
        )
        payload = parse_text_result(result)
    assert payload.get("killed_reason") == "rejected_by_policy", payload
    assert "BYPASSED" not in (payload.get("stdout") or ""), payload


@pytest.mark.asyncio
async def test_combined_high_memory_read_only_still_registers_tool(tmp_path):
    """The high-memory tool must be present even in read-only combined mode."""
    async with hares_session(
        enable="fs+shell", scope_id="ro", ceiling=tmp_path, read_only=True,
    ) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    assert "ro_execute_command_high_memory" in names
