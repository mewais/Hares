"""MCP protocol-level tests for the Hares cluster server (LSF + SLURM).

Spawns ``hares-mcp --enable={lsf,slurm}`` as a subprocess and verifies
the externally-visible tool surface via real JSON-RPC. Neither bsub
nor sbatch is required — these tests exercise tool listing, schema
correctness, the tool-routing layer, and the ``X_not_found`` error
paths. Tool calls that need the scheduler (e.g. submit) will fail at
exec time which is the assertion target for those tests.
"""

from __future__ import annotations

import pytest

from ._proto_helpers import hares_session, parse_text_result


# ── Tool-list shape (parametrized over schedulers) ───────────────────────────

@pytest.mark.parametrize("scheduler", ["lsf", "slurm"])
@pytest.mark.asyncio
async def test_lists_five_tools_no_scope(tmp_path, scheduler):
    async with hares_session(enable=scheduler) as s:
        resp = await s.list_tools()
        names = {t.name for t in resp.tools}
    assert names == {
        f"{scheduler}_execute_blocking",
        f"{scheduler}_submit",
        f"{scheduler}_wait",
        f"{scheduler}_cancel",
        f"{scheduler}_jobs",
    }


@pytest.mark.parametrize("scheduler", ["lsf", "slurm"])
@pytest.mark.asyncio
async def test_scope_id_prefixes_all_tools(tmp_path, scheduler):
    async with hares_session(enable=scheduler, scope_id="cluster") as s:
        resp = await s.list_tools()
        names = {t.name for t in resp.tools}
    assert names == {
        f"cluster_{scheduler}_execute_blocking",
        f"cluster_{scheduler}_submit",
        f"cluster_{scheduler}_wait",
        f"cluster_{scheduler}_cancel",
        f"cluster_{scheduler}_jobs",
    }


@pytest.mark.parametrize("scheduler", ["lsf", "slurm"])
@pytest.mark.asyncio
async def test_no_ceiling_required(tmp_path, scheduler):
    """Cluster modes start successfully without --ceiling."""
    async with hares_session(enable=scheduler) as s:
        resp = await s.list_tools()
    assert len(resp.tools) == 5


@pytest.mark.parametrize("scheduler", ["lsf", "slurm"])
@pytest.mark.asyncio
async def test_tool_schemas(tmp_path, scheduler):
    async with hares_session(enable=scheduler) as s:
        resp = await s.list_tools()
        tools = {t.name: t for t in resp.tools}

    blocking_schema = tools[f"{scheduler}_execute_blocking"].inputSchema
    assert "command" in blocking_schema.get("required", [])

    submit_schema = tools[f"{scheduler}_submit"].inputSchema
    assert "jobs" in submit_schema.get("required", [])
    assert submit_schema["properties"]["jobs"]["type"] == "array"

    wait_schema = tools[f"{scheduler}_wait"].inputSchema
    assert "job_ids" in wait_schema.get("required", [])

    cancel_schema = tools[f"{scheduler}_cancel"].inputSchema
    assert "job_ids" in cancel_schema.get("required", [])

    jobs_schema = tools[f"{scheduler}_jobs"].inputSchema
    assert jobs_schema.get("required", []) == []


@pytest.mark.parametrize("scheduler", ["lsf", "slurm"])
@pytest.mark.asyncio
async def test_blocking_description_has_security_note(tmp_path, scheduler):
    async with hares_session(enable=scheduler) as s:
        resp = await s.list_tools()
        tools = {t.name: t for t in resp.tools}
    desc = tools[f"{scheduler}_execute_blocking"].description
    assert "bwrap" in desc or "SECURITY" in desc


# ── jobs (no scheduler needed) ───────────────────────────────────────────────

@pytest.mark.parametrize("scheduler", ["lsf", "slurm"])
@pytest.mark.asyncio
async def test_jobs_empty_at_start(tmp_path, scheduler):
    async with hares_session(enable=scheduler) as s:
        result = await s.call_tool(f"{scheduler}_jobs", {})
        payload = parse_text_result(result)
    assert payload == []


# ── wait unknown job (no scheduler needed) ───────────────────────────────────

@pytest.mark.parametrize("scheduler", ["lsf", "slurm"])
@pytest.mark.asyncio
async def test_wait_unknown_job_returns_error(tmp_path, scheduler):
    async with hares_session(enable=scheduler) as s:
        result = await s.call_tool(
            f"{scheduler}_wait",
            {"job_ids": ["99999"], "timeout_sec": 1},
        )
        payload = parse_text_result(result)
    assert payload["99999"]["status"] == "ERROR"
    assert "not known" in payload["99999"]["error"]


# ── submit with missing scheduler binary returns structured error ────────────

@pytest.mark.asyncio
async def test_lsf_submit_bsub_not_found_returns_error_shape(tmp_path):
    async with hares_session(
        enable="lsf",
        extra_env={"HARES_LSF_BSUB_BIN": "/nonexistent/bsub"},
    ) as s:
        result = await s.call_tool("lsf_submit", {
            "jobs": [{"command": "echo hello"}],
        })
        payload = parse_text_result(result)
    assert isinstance(payload, list)
    assert len(payload) == 1
    entry = payload[0]
    assert entry.get("job_id") is None
    assert "error" in entry


@pytest.mark.asyncio
async def test_slurm_submit_sbatch_not_found_returns_error_shape(tmp_path):
    async with hares_session(
        enable="slurm",
        extra_env={"HARES_SLURM_SBATCH_BIN": "/nonexistent/sbatch"},
    ) as s:
        result = await s.call_tool("slurm_submit", {
            "jobs": [{"command": "echo hello"}],
        })
        payload = parse_text_result(result)
    assert isinstance(payload, list)
    assert len(payload) == 1
    entry = payload[0]
    assert entry.get("job_id") is None
    assert "error" in entry


# ── cancel with missing scheduler binary returns failure ─────────────────────

@pytest.mark.asyncio
async def test_lsf_cancel_bkill_not_found_returns_failure(tmp_path):
    async with hares_session(
        enable="lsf",
        extra_env={"HARES_LSF_BKILL_BIN": "/nonexistent/bkill"},
    ) as s:
        result = await s.call_tool("lsf_cancel", {"job_ids": ["12345"]})
        payload = parse_text_result(result)
    assert payload["12345"]["cancelled"] is False


@pytest.mark.asyncio
async def test_slurm_cancel_scancel_not_found_returns_failure(tmp_path):
    async with hares_session(
        enable="slurm",
        extra_env={"HARES_SLURM_SCANCEL_BIN": "/nonexistent/scancel"},
    ) as s:
        result = await s.call_tool("slurm_cancel", {"job_ids": ["12345"]})
        payload = parse_text_result(result)
    assert payload["12345"]["cancelled"] is False


# ── CLI: --enable= flags accepted ────────────────────────────────────────────

def test_cli_parse_enable_lsf():
    from hares.cli import _parse_args
    args = _parse_args(["--enable=lsf"])
    assert args.enable == "lsf"


def test_cli_parse_enable_slurm():
    from hares.cli import _parse_args
    args = _parse_args(["--enable=slurm"])
    assert args.enable == "slurm"


def test_cli_lsf_ceiling_optional():
    """Cluster modes do not require --ceiling (unlike shell/fs modes)."""
    from hares.cli import _parse_args
    args = _parse_args(["--enable=lsf"])
    assert args.ceiling is None


def test_cli_slurm_ceiling_optional():
    from hares.cli import _parse_args
    args = _parse_args(["--enable=slurm"])
    assert args.ceiling is None
