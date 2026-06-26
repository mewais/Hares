"""MCP-protocol-level integration tests for the shell server.

Spawns ``hares-mcp --enable=shell ...`` as a subprocess and verifies
the externally-visible tool surface and execute_command behavior over
real JSON-RPC. bwrap is force-disabled in the test env so these tests
work in CI containers without bubblewrap.
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


# ── execute_command_high_memory tool ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shell_high_memory_tool_appears_in_list_tools_unprefixed(tmp_path):
    """The new high-memory tool must be registered in the unprefixed surface."""
    async with hares_session(enable="shell", ceiling=tmp_path) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    assert "execute_command_high_memory" in names


@pytest.mark.asyncio
async def test_shell_high_memory_tool_appears_in_list_tools_prefixed(tmp_path):
    """The high-memory tool is correctly prefixed when scope_id is set."""
    async with hares_session(enable="shell", scope_id="safe", ceiling=tmp_path) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    assert "safe_execute_command_high_memory" in names
    # All tools must carry the prefix.
    assert all(n.startswith("safe_") for n in names), names


@pytest.mark.asyncio
async def test_shell_high_memory_tool_schema_has_command_field(tmp_path):
    """The high-memory tool's inputSchema must include the 'command' field."""
    async with hares_session(enable="shell", ceiling=tmp_path) as s:
        tools = await s.list_tools()
    tool_map = {t.name: t for t in tools.tools}
    hm_tool = tool_map["execute_command_high_memory"]
    assert "command" in hm_tool.inputSchema.get("properties", {}), (
        f"'command' missing from schema properties: {hm_tool.inputSchema}"
    )
    assert hm_tool.inputSchema.get("required") == ["command"], (
        f"'command' must be required: {hm_tool.inputSchema}"
    )


@pytest.mark.asyncio
async def test_shell_high_memory_no_elicitation_support_fails_closed(tmp_path):
    """Calling execute_command_high_memory when the client has NO elicitation
    support must fail closed — the result must indicate rejection, never
    execute the command.  This is the core security property: there is no
    argument the caller can pass to bypass the approval gate."""
    # Default session: no elicitation_callback → MCP SDK returns ErrorData →
    # server's elicit_memory_approval catches the exception → returns False.
    async with hares_session(enable="shell", ceiling=tmp_path) as s:
        result = await s.call_tool(
            "execute_command_high_memory",
            {"command": "echo SHOULD_NOT_RUN", "timeout": 10},
        )
        payload = parse_text_result(result)
    assert payload.get("killed_reason") == "rejected_by_policy", payload
    assert payload.get("exit_code") == -1, payload
    # The command must NOT have executed — stdout must be empty/absent.
    assert "SHOULD_NOT_RUN" not in (payload.get("stdout") or ""), payload


@pytest.mark.asyncio
async def test_shell_high_memory_elicitation_decline_fails_closed(tmp_path):
    """Even when the client supports elicitation, a DECLINE must fail closed."""
    async with hares_session(
        enable="shell", ceiling=tmp_path,
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
async def test_shell_high_memory_elicitation_accept_dispatches(tmp_path):
    """When the user accepts the elicitation, the command must run."""
    async with hares_session(
        enable="shell", ceiling=tmp_path,
        elicitation_callback=_accept_elicitation_callback,
    ) as s:
        result = await s.call_tool(
            "execute_command_high_memory",
            {"command": "echo hi-from-high-memory", "timeout": 10},
        )
        payload = parse_text_result(result)
    assert "hi-from-high-memory" in (payload.get("stdout") or ""), payload


@pytest.mark.asyncio
async def test_shell_high_memory_no_bypass_via_arguments(tmp_path):
    """Security property: no argument combination must bypass the elicitation.

    Even if the caller passes every possible argument (mem_limit_mb,
    cpu_limit_sec, etc.) the approval gate must still fire.  Without an
    elicitation callback the default client refuses → rejected_by_policy."""
    async with hares_session(enable="shell", ceiling=tmp_path) as s:
        result = await s.call_tool(
            "execute_command_high_memory",
            {
                "command": "echo BYPASSED",
                "mem_limit_mb": 512,
                "cpu_limit_sec": 30,
                "timeout": 10,
                "weight": 1,
            },
        )
        payload = parse_text_result(result)
    assert payload.get("killed_reason") == "rejected_by_policy", payload
    assert "BYPASSED" not in (payload.get("stdout") or ""), payload
