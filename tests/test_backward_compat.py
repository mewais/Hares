"""Backward-compat: bare ``hares-mcp`` (no flags) preserves 0.1 behavior.

The 0.1 release shipped a single-tool shell server: bare invocation
exposes ``execute_command`` (unprefixed), no restrict tools (no
ceiling), and uses an in-process semaphore (no coordinator). This
test pins that contract so future refactors don't accidentally break
existing 0.1 callers.

We force ``HARES_SANDBOX_DISABLED=1`` because the 0.2 default flipped
to bwrap-required; that's the only intentional break vs 0.1.
"""

from __future__ import annotations

import os

import pytest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.asyncio
async def test_bare_invocation_exposes_execute_command_and_restrict_unprefixed(tmp_path):
    """Bare `hares-mcp` with HARES_FS_CEILING env (the migration path)
    exposes the 0.1 tool surface (`execute_command`) plus the always-on
    restrict tools, all unprefixed. The 0.1 single-tool registry is
    preserved in the sense that no scope_id-prefixed names appear; the
    new restrict tools are intentionally additive.
    """
    env = os.environ.copy()
    env["HARES_SANDBOX_DISABLED"] = "1"
    env["HARES_FS_CEILING"] = str(tmp_path)
    params = StdioServerParameters(command="hares-mcp", args=[], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
    # All names unprefixed (no scope_id_ prefix anywhere).
    assert "execute_command" in names
    assert "restrict_paths" in names
    assert "get_active_paths" in names
    # Defensive: no fs tools leaked into the default shell-only mode.
    assert "read_file" not in names
    assert "write_file" not in names


@pytest.mark.asyncio
async def test_bare_invocation_no_ceiling_no_env_defaults_to_pwd(tmp_path):
    """Pinned-behavior cousin of
    test_cli_validation::test_missing_ceiling_defaults_to_pwd.

    Behavior changed in 0.5: the bare invocation no longer fails on a
    missing ceiling — it defaults to $PWD with an INFO log. Pinned
    here too because the change is user-visible and we want the proto
    layer to surface it as well as the CLI layer.

    We pass an empty stdin so the server exits without blocking on the
    MCP handshake, then assert on the startup log."""
    import subprocess
    env = os.environ.copy()
    env["HARES_SANDBOX_DISABLED"] = "1"
    env.pop("HARES_FS_CEILING", None)
    proc = subprocess.Popen(
        ["hares-mcp"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path), env=env,
    )
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=2)
    err = proc.stderr.read().decode() if proc.stderr else ""
    assert "--ceiling is required" not in err  # the old contract is gone
    assert "defaulting to $PWD" in err
    assert str(tmp_path) in err
