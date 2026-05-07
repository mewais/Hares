"""MCP protocol-level tests for the Hares LSF server.

Spawns ``hares-mcp --enable=lsf`` as a subprocess and verifies the
externally-visible tool surface via real JSON-RPC. No real LSF
scheduler is required — these tests only exercise tool listing, schema
correctness, and the tool-routing layer. Jobs submitted during tests
will fail (bsub not found) which is fine; we assert on the error shape.
"""

from __future__ import annotations

import pytest

from ._proto_helpers import hares_session, parse_text_result


# ── list_tools ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lsf_lists_five_tools_no_scope(tmp_path):
    async with hares_session(enable="lsf") as s:
        resp = await s.list_tools()
        names = {t.name for t in resp.tools}
    assert names == {
        "lsf_execute_blocking",
        "lsf_submit",
        "lsf_wait",
        "lsf_cancel",
        "lsf_jobs",
    }


@pytest.mark.asyncio
async def test_lsf_scope_id_prefixes_all_tools(tmp_path):
    async with hares_session(enable="lsf", scope_id="cluster") as s:
        resp = await s.list_tools()
        names = {t.name for t in resp.tools}
    assert names == {
        "cluster_lsf_execute_blocking",
        "cluster_lsf_submit",
        "cluster_lsf_wait",
        "cluster_lsf_cancel",
        "cluster_lsf_jobs",
    }


@pytest.mark.asyncio
async def test_lsf_tools_no_ceiling_required(tmp_path):
    """LSF mode starts successfully without --ceiling."""
    async with hares_session(enable="lsf") as s:
        resp = await s.list_tools()
    assert len(resp.tools) == 5


@pytest.mark.asyncio
async def test_lsf_tool_schemas(tmp_path):
    async with hares_session(enable="lsf") as s:
        resp = await s.list_tools()
        tools = {t.name: t for t in resp.tools}

    blocking_schema = tools["lsf_execute_blocking"].inputSchema
    assert "command" in blocking_schema.get("required", [])

    submit_schema = tools["lsf_submit"].inputSchema
    assert "jobs" in submit_schema.get("required", [])
    assert submit_schema["properties"]["jobs"]["type"] == "array"

    wait_schema = tools["lsf_wait"].inputSchema
    assert "job_ids" in wait_schema.get("required", [])

    cancel_schema = tools["lsf_cancel"].inputSchema
    assert "job_ids" in cancel_schema.get("required", [])

    jobs_schema = tools["lsf_jobs"].inputSchema
    assert jobs_schema.get("required", []) == []


@pytest.mark.asyncio
async def test_lsf_blocking_description_has_security_note(tmp_path):
    async with hares_session(enable="lsf") as s:
        resp = await s.list_tools()
        tools = {t.name: t for t in resp.tools}
    desc = tools["lsf_execute_blocking"].description
    assert "bwrap" in desc or "SECURITY" in desc


# ── lsf_jobs (no scheduler needed) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_lsf_jobs_empty_at_start(tmp_path):
    async with hares_session(enable="lsf") as s:
        result = await s.call_tool("lsf_jobs", {})
        payload = parse_text_result(result)
    assert payload == []


# ── lsf_wait unknown job (no scheduler needed) ────────────────────────────────

@pytest.mark.asyncio
async def test_lsf_wait_unknown_job_returns_error(tmp_path):
    async with hares_session(enable="lsf") as s:
        result = await s.call_tool("lsf_wait", {"job_ids": ["99999"], "timeout_sec": 1})
        payload = parse_text_result(result)
    assert payload["99999"]["status"] == "ERROR"
    assert "not known" in payload["99999"]["error"]


# ── lsf_submit with missing bsub returns error shape ─────────────────────────

@pytest.mark.asyncio
async def test_lsf_submit_bsub_not_found_returns_error_shape(tmp_path):
    """When bsub is not on PATH, submit returns an error entry per job.
    Tests the end-to-end error path without a real scheduler."""
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
    # bsub not found → error entry with no job_id.
    entry = payload[0]
    assert entry.get("job_id") is None
    assert "error" in entry


# ── lsf_cancel unknown job (no scheduler needed) ──────────────────────────────

@pytest.mark.asyncio
async def test_lsf_cancel_bkill_not_found_returns_failure(tmp_path):
    async with hares_session(
        enable="lsf",
        extra_env={"HARES_LSF_BKILL_BIN": "/nonexistent/bkill"},
    ) as s:
        result = await s.call_tool("lsf_cancel", {"job_ids": ["12345"]})
        payload = parse_text_result(result)
    assert payload["12345"]["cancelled"] is False


# ── CLI: --enable=lsf flag ───────────────────────────────────────────────────

def test_cli_parse_enable_lsf():
    from hares.cli import _parse_args
    args = _parse_args(["--enable=lsf"])
    assert args.enable == "lsf"


def test_cli_lsf_ceiling_optional():
    """LSF mode does not require --ceiling (unlike shell/fs modes)."""
    from hares.cli import _parse_args
    args = _parse_args(["--enable=lsf"])
    assert args.ceiling is None
