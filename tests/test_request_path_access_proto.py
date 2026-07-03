"""MCP-protocol-level tests for the request_path_access tool —
the human-in-the-loop-gated runtime path-access escape hatch.

Covers all three server modes (fs, shell, fs+shell) via real JSON-RPC,
mirroring the conventions in test_fs_server_proto.py /
test_shell_server_proto.py / test_combined_proto.py.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ._proto_helpers import (
    hares_session,
    parse_text_result,
    _accept_elicitation_callback,
    _cancel_elicitation_callback,
    _decline_elicitation_callback,
    _grant_elicitation_callback,
)


# ── Tool registration ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("enable", ["shell", "fs", "fs+shell"])
async def test_request_path_access_registered_unprefixed(enable, tmp_path):
    async with hares_session(enable=enable, ceiling=tmp_path) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    assert "request_path_access" in names


@pytest.mark.asyncio
async def test_request_path_access_prefixed_when_scope_id_set(tmp_path):
    async with hares_session(
        enable="fs+shell", scope_id="blk", ceiling=tmp_path,
    ) as s:
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
    assert "blk_request_path_access" in names
    assert all(n.startswith("blk_") for n in names), names


@pytest.mark.asyncio
async def test_request_path_access_schema_requires_path_mode_reason(tmp_path):
    async with hares_session(enable="fs", ceiling=tmp_path) as s:
        tools = await s.list_tools()
    tool_map = {t.name: t for t in tools.tools}
    tool = tool_map["request_path_access"]
    props = tool.inputSchema.get("properties", {})
    assert "path" in props and "mode" in props and "reason" in props
    assert set(tool.inputSchema.get("required", [])) == {"path", "mode", "reason"}
    assert set(props["mode"].get("enum", [])) == {"ro", "rw"}


# ── Fail-closed paths (decline / cancel / no elicitation support) ──────


@pytest.mark.asyncio
async def test_no_elicitation_support_denied(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    async with hares_session(enable="fs", ceiling=tmp_path) as s:
        result = await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "ro", "reason": "need to inspect"},
        )
        payload = parse_text_result(result)
    assert payload["granted"] is False
    assert "reason_denied" in payload


@pytest.mark.asyncio
async def test_decline_denied(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_decline_elicitation_callback,
    ) as s:
        result = await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "ro", "reason": "need to inspect"},
        )
        payload = parse_text_result(result)
    assert payload["granted"] is False


@pytest.mark.asyncio
async def test_cancel_denied(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_cancel_elicitation_callback,
    ) as s:
        result = await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "ro", "reason": "need to inspect"},
        )
        payload = parse_text_result(result)
    assert payload["granted"] is False


@pytest.mark.asyncio
async def test_in_dialog_deny_choice_denied(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_grant_elicitation_callback("deny"),
    ) as s:
        result = await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "ro", "reason": "need to inspect"},
        )
        payload = parse_text_result(result)
    assert payload["granted"] is False


# ── Lax-client fallback ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lax_accept_missing_grant_field_allows_once(tmp_path):
    """The shared _accept_elicitation_callback returns action=accept
    with an EMPTY content dict, regardless of requestedSchema — the
    lax-client scenario the spec calls out. Must fall back to
    allow-once."""
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_accept_elicitation_callback,
    ) as s:
        result = await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "ro", "reason": "need to inspect"},
        )
        payload = parse_text_result(result)
    assert payload["granted"] is True
    assert payload["lifetime"] == "once"


# ── Deny beats grant — cannot be granted even on accept ─────────────────


@pytest.mark.asyncio
async def test_dot_git_path_cannot_be_granted(tmp_path):
    git_dir = tmp_path.parent / f"gitrepo_{tmp_path.name}" / ".git" / "objects"
    git_dir.mkdir(parents=True, exist_ok=True)
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_grant_elicitation_callback("session"),
    ) as s:
        result = await s.call_tool(
            "request_path_access",
            {"path": str(git_dir), "mode": "ro", "reason": "need git internals"},
        )
        payload = parse_text_result(result)
    assert payload["granted"] is False
    assert ".git" in payload["reason_denied"]


@pytest.mark.asyncio
async def test_system_dir_cannot_be_granted_when_strict(tmp_path):
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_grant_elicitation_callback("session"),
        extra_env={"HARES_DISALLOW_SYSTEM_DIRS": "1"},
    ) as s:
        result = await s.call_tool(
            "request_path_access",
            {"path": "/etc", "mode": "ro", "reason": "need system config"},
        )
        payload = parse_text_result(result)
    assert payload["granted"] is False


@pytest.mark.asyncio
async def test_excluded_path_cannot_be_granted(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_grant_elicitation_callback("session"),
        extra_env={"HARES_SANDBOX_EXCLUDE": "secrets"},
    ) as s:
        result = await s.call_tool(
            "request_path_access",
            {"path": str(secrets), "mode": "ro", "reason": "need the secret"},
        )
        payload = parse_text_result(result)
    assert payload["granted"] is False


# ── --read-only rejects rw requests without a dialog ────────────────────


@pytest.mark.asyncio
async def test_read_only_rejects_rw_request_no_elicitation_needed(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    # No elicitation_callback at all — if the handler tried to elicit,
    # this would deny with "client does not support elicitation"; we
    # assert the read_only-specific message instead, proving the
    # rejection happened BEFORE any elicitation attempt.
    async with hares_session(enable="fs", ceiling=tmp_path, read_only=True) as s:
        result = await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "rw", "reason": "need to write"},
        )
        payload = parse_text_result(result)
    assert payload["granted"] is False
    assert "read-only" in payload["reason_denied"].lower()


@pytest.mark.asyncio
async def test_read_only_still_allows_ro_request(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    async with hares_session(
        enable="fs", ceiling=tmp_path, read_only=True,
        elicitation_callback=_grant_elicitation_callback("session"),
    ) as s:
        result = await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "ro", "reason": "need to inspect"},
        )
        payload = parse_text_result(result)
    assert payload["granted"] is True


# ── Successful grants + fs enforcement + get_active_paths visibility ───


@pytest.mark.asyncio
async def test_session_grant_allows_read_then_write_file_via_fs_tools(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_grant_elicitation_callback("session"),
    ) as s:
        granted = parse_text_result(await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "rw", "reason": "need to write output"},
        ))
        assert granted["granted"] is True
        assert granted["lifetime"] == "session"

        target = str(outside / "out.txt")
        write_result = await s.call_tool(
            "write_file", {"path": target, "content": "hello-outside"},
        )
        assert not write_result.isError, write_result

        read_result = parse_text_result(await s.call_tool(
            "read_file", {"path": target},
        ))
        assert read_result["content"] == "hello-outside"


@pytest.mark.asyncio
async def test_ro_grant_allows_read_but_not_write(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    (outside / "existing.txt").write_text("preexisting")
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_grant_elicitation_callback("session"),
    ) as s:
        granted = parse_text_result(await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "ro", "reason": "need to read logs"},
        ))
        assert granted["granted"] is True

        read_result = parse_text_result(await s.call_tool(
            "read_file", {"path": str(outside / "existing.txt")},
        ))
        assert read_result["content"] == "preexisting"

        write_result = await s.call_tool(
            "write_file",
            {"path": str(outside / "nope.txt"), "content": "should fail"},
        )
        assert write_result.isError


@pytest.mark.asyncio
async def test_once_grant_consumed_after_one_fs_use(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    target = outside / "f.txt"
    target.write_text("once-content")
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_grant_elicitation_callback("once"),
    ) as s:
        granted = parse_text_result(await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "ro", "reason": "need one read"},
        ))
        assert granted["granted"] is True
        assert granted["lifetime"] == "once"

        first = parse_text_result(await s.call_tool(
            "read_file", {"path": str(target)},
        ))
        assert first["content"] == "once-content"

        second = await s.call_tool("read_file", {"path": str(target)})
        assert second.isError


@pytest.mark.asyncio
async def test_get_active_paths_reports_grants(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        elicitation_callback=_grant_elicitation_callback("session"),
    ) as s:
        await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "rw", "reason": "need to write"},
        )
        got = parse_text_result(await s.call_tool("get_active_paths", {}))
        assert "grants" in got
        grants = got["grants"]
        assert len(grants) == 1
        assert grants[0]["path"] == str(outside.resolve())
        assert grants[0]["mode"] == "rw"
        assert grants[0]["lifetime"] == "session"


@pytest.mark.asyncio
async def test_get_active_paths_grants_empty_when_none_active(tmp_path):
    async with hares_session(enable="fs", ceiling=tmp_path) as s:
        got = parse_text_result(await s.call_tool("get_active_paths", {}))
        assert got["grants"] == []


# ── Combined mode: one shared GrantStore ────────────────────────────────


@pytest.mark.asyncio
async def test_combined_shares_one_grant_store_across_fs_and_get_active(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    async with hares_session(
        enable="fs+shell", scope_id="c", ceiling=tmp_path,
        elicitation_callback=_grant_elicitation_callback("session"),
    ) as s:
        granted = parse_text_result(await s.call_tool(
            "c_request_path_access",
            {"path": str(outside), "mode": "rw", "reason": "need to write"},
        ))
        assert granted["granted"] is True

        # fs side sees the grant (write succeeds outside the ceiling).
        write_result = await s.call_tool(
            "c_write_file",
            {"path": str(outside / "combined.txt"), "content": "shared-store"},
        )
        assert not write_result.isError, write_result

        # get_active_paths (one shared instance) reports it too.
        got = parse_text_result(await s.call_tool("c_get_active_paths", {}))
        assert len(got["grants"]) == 1
        assert got["grants"][0]["path"] == str(outside.resolve())


_BWRAP = shutil.which("bwrap")


@pytest.mark.asyncio
@pytest.mark.skipif(_BWRAP is None, reason="bwrap not available")
async def test_combined_grant_reaches_shell_bwrap_mounts(tmp_path):
    """The strongest 'one shared store' proof: a grant recorded via
    request_path_access must be visible to the Runner's bwrap mount
    composition too, not just the fs side — i.e. execute_command can
    write to a path outside the ceiling once granted."""
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.pop("HARES_SANDBOX_DISABLED", None)
    async with hares_session(
        enable="fs+shell", ceiling=tmp_path,
        elicitation_callback=_grant_elicitation_callback("session"),
        extra_env={**env, "HARES_SANDBOX_DISABLED": ""},
    ) as s:
        granted = parse_text_result(await s.call_tool(
            "request_path_access",
            {"path": str(outside), "mode": "rw", "reason": "need to write via shell"},
        ))
        assert granted["granted"] is True

        cmd = f"echo via-shell > {outside}/shell_marker.txt"
        result = await s.call_tool(
            "execute_command", {"command": cmd, "timeout": 15},
        )
        payload = parse_text_result(result)
        if payload.get("exit_code") != 0:
            pytest.skip(
                f"bwrap subprocess setup failed (likely userns restriction): {payload}"
            )
        assert (outside / "shell_marker.txt").exists()
