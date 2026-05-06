"""MCP-protocol-level integration tests for the shell server.

Spawns ``hares-mcp --enable=shell ...`` as a subprocess and verifies
the externally-visible tool surface and execute_command behavior over
real JSON-RPC. bwrap is force-disabled in the test env so these tests
work in CI containers without bubblewrap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._proto_helpers import hares_session, parse_text_result


@pytest.mark.asyncio
async def test_shell_lists_execute_command_unprefixed_no_scope(tmp_path):
    async with hares_session(enable="shell", ceiling=tmp_path) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    assert "execute_command" in names
    assert "restrict_paths" in names
    assert "get_active_paths" in names
    # No accidental fs tools leak into the shell-only surface.
    assert "read_file" not in names
    assert "write_file" not in names


@pytest.mark.asyncio
async def test_shell_scope_id_prefixes_all_tools(tmp_path):
    async with hares_session(enable="shell", scope_id="safe", ceiling=tmp_path) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    assert all(n.startswith("safe_") for n in names), names
    assert "safe_execute_command" in names
    assert "safe_restrict_paths" in names


@pytest.mark.asyncio
async def test_shell_execute_command_runs(tmp_path):
    async with hares_session(enable="shell", ceiling=tmp_path) as s:
        result = await s.call_tool(
            "execute_command",
            {"command": "echo hello-from-hares", "timeout": 10},
        )
        payload = parse_text_result(result)
        # Runner returns a dict including stdout
        assert "hello-from-hares" in (payload.get("stdout") or ""), payload


@pytest.mark.asyncio
async def test_shell_restrict_then_get_round_trip(tmp_path):
    a = tmp_path / "scope_a"; a.mkdir()
    async with hares_session(enable="shell", ceiling=tmp_path) as s:
        await s.call_tool("restrict_paths", {"paths": [str(a)]})
        got = await s.call_tool("get_active_paths", {})
        payload = parse_text_result(got)
        assert {Path(p) for p in payload["active_paths"]} == {a.resolve()}
