"""Tests for HARES_SANDBOX_DISABLED=1 — the 0.1-compat opt-out.

In 0.2, bwrap is REQUIRED by default. Operators on non-Linux (mac),
in restricted-userns containers, or debugging set
``HARES_SANDBOX_DISABLED=1`` to opt out. When opted out:

* Subprocesses run without bwrap (existing 0.1 behavior).
* Shell ``--read-only`` becomes BEST-EFFORT — the kernel doesn't
  enforce RO any more, just a logged warning surfaces.
* Active-scope narrowing still gates fs tool calls (in-process check
  at path-validation time), but shell-spawned processes can still
  reach the full filesystem.

These tests pin those behaviors so a future "tighten by default"
refactor doesn't silently drop the escape hatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._proto_helpers import hares_session, parse_text_result


@pytest.mark.asyncio
async def test_sandbox_disabled_shell_command_runs_without_bwrap(tmp_path):
    async with hares_session(
        enable="shell", ceiling=tmp_path,
        extra_env={"HARES_SANDBOX_DISABLED": "1"},
    ) as s:
        result = await s.call_tool(
            "execute_command",
            {"command": "echo unsandboxed && /bin/pwd", "timeout": 10},
        )
        payload = parse_text_result(result)
        assert payload.get("exit_code", 1) == 0, payload
        assert "unsandboxed" in (payload.get("stdout") or "")


@pytest.mark.asyncio
async def test_sandbox_disabled_fs_active_scope_still_enforced(tmp_path):
    """Active-scope is enforced at the Python validation layer for fs
    tools, independent of bwrap. So even with sandbox disabled, an
    out-of-scope write must still fail."""
    inside = tmp_path / "in"; inside.mkdir()
    outside = tmp_path / "out"; outside.mkdir()
    async with hares_session(
        enable="fs", ceiling=tmp_path,
        extra_env={"HARES_SANDBOX_DISABLED": "1"},
    ) as s:
        await s.call_tool("restrict_paths", {"paths": [str(inside)]})
        bad = await s.call_tool(
            "write_file",
            {"path": str(outside / "f.txt"), "content": "x"},
        )
        assert bad.isError
        assert not (outside / "f.txt").exists()
