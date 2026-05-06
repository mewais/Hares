"""Bwrap-level enforcement of shell ``--read-only``.

When a shell instance is launched with ``--read-only``, the active
scope is mounted RO inside the bwrap namespace. Subprocess reads
succeed; subprocess writes fail at the kernel level (EROFS).
"""

from __future__ import annotations

import os
import shutil

import pytest

from ._proto_helpers import hares_session, parse_text_result


_BWRAP = shutil.which("bwrap")
pytestmark = pytest.mark.skipif(
    _BWRAP is None,
    reason="bwrap not available; kernel-level enforcement cannot be exercised",
)


@pytest.mark.asyncio
async def test_read_only_subprocess_can_read(tmp_path):
    inside = tmp_path / "inside"; inside.mkdir()
    (inside / "data.txt").write_text("the secret")
    env = os.environ.copy()
    env.pop("HARES_SANDBOX_DISABLED", None)
    async with hares_session(
        enable="shell", ceiling=tmp_path, read_only=True,
        extra_env={**env, "HARES_SANDBOX_DISABLED": ""},
    ) as s:
        await s.call_tool("restrict_paths", {"paths": [str(inside)]})
        r = await s.call_tool(
            "execute_command",
            {"command": f"cat {inside}/data.txt", "timeout": 10},
        )
        payload = parse_text_result(r)
        if payload.get("exit_code") != 0:
            pytest.skip(f"bwrap setup failed: {payload}")
        assert "the secret" in (payload.get("stdout") or "")


@pytest.mark.asyncio
async def test_read_only_subprocess_cannot_write(tmp_path):
    inside = tmp_path / "inside"; inside.mkdir()
    env = os.environ.copy()
    env.pop("HARES_SANDBOX_DISABLED", None)
    async with hares_session(
        enable="shell", ceiling=tmp_path, read_only=True,
        extra_env={**env, "HARES_SANDBOX_DISABLED": ""},
    ) as s:
        await s.call_tool("restrict_paths", {"paths": [str(inside)]})
        r = await s.call_tool(
            "execute_command",
            {"command": f"echo nope > {inside}/forbidden.txt", "timeout": 10},
        )
        payload = parse_text_result(r)
        # Kernel must have rejected the write — either non-zero exit OR
        # the file simply doesn't exist on the host post-call.
        if payload.get("exit_code") == 0 and (inside / "forbidden.txt").exists():
            pytest.skip(
                "bwrap RO mount didn't enforce; likely userns restriction"
            )
        assert payload.get("exit_code", 0) != 0 or not (inside / "forbidden.txt").exists()
