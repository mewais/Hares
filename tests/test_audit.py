"""Tests for hares.audit — Auditor + audited decorator."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
from pathlib import Path

import pytest

from hares.audit import (
    Auditor,
    audited,
    load_auditor,
    _redact_value,
    _summarize_mcp_result,
    _truncate,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("HARES_AUDIT_"):
            monkeypatch.delenv(key, raising=False)


class _FakeTextContent:
    """Mimics mcp.types.TextContent for tests."""
    def __init__(self, text: str, type_: str = "text"):
        self.text = text
        self.type = type_


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── _truncate ──────────────────────────────────────────────────────────────

def test_truncate_under_limit_returns_unchanged():
    assert _truncate("hello", 10) == "hello"


def test_truncate_over_limit_includes_marker():
    s = "a" * 100
    out = _truncate(s, 10)
    assert out.startswith("a" * 10)
    assert "90 more chars" in out


# ── _redact_value ──────────────────────────────────────────────────────────

def test_redact_dict_redacts_named_field():
    val = {"command": "ls", "env": {"SECRET": "abc", "FOO": "bar"}}
    out = _redact_value(val, frozenset({"env"}), 500)
    assert out["command"] == "ls"
    assert out["env"] == {"SECRET": "<redacted>", "FOO": "<redacted>"}


def test_redact_dict_no_redact_when_field_not_listed():
    val = {"env": {"X": "y"}}
    out = _redact_value(val, frozenset(), 500)
    assert out["env"] == {"X": "y"}


def test_redact_truncates_long_strings():
    val = {"body": "x" * 1000}
    out = _redact_value(val, frozenset(), 100)
    assert "more chars elided" in out["body"]


def test_redact_caps_long_lists():
    val = {"items": list(range(200))}
    out = _redact_value(val, frozenset(), 500)
    assert len(out["items"]) == 50


def test_redact_handles_non_dict_redact_target():
    """If 'env' field is a string instead of a dict, redact wholesale."""
    val = {"env": "SECRET=abc"}
    out = _redact_value(val, frozenset({"env"}), 500)
    assert out["env"] == "<redacted>"


# ── _summarize_mcp_result ──────────────────────────────────────────────────

def test_summarize_empty_result():
    assert _summarize_mcp_result([], 500) == {"empty": True}


def test_summarize_json_text_content():
    item = _FakeTextContent(json.dumps({"exit_code": 0, "stdout": "hi"}))
    out = _summarize_mcp_result([item], 500)
    assert out["items"][0]["type"] == "json"
    assert out["items"][0]["value"]["exit_code"] == 0


def test_summarize_non_json_text_falls_back_to_preview():
    item = _FakeTextContent("not json {[")
    out = _summarize_mcp_result([item], 500)
    assert out["items"][0]["type"] == "text"
    assert "preview" in out["items"][0]


def test_summarize_truncates_long_json_values():
    item = _FakeTextContent(json.dumps({"output": "x" * 1000}))
    out = _summarize_mcp_result([item], 100)
    assert "more chars elided" in out["items"][0]["value"]["output"]


def test_summarize_caps_at_three_items_and_records_total():
    items = [_FakeTextContent(f"item {i}") for i in range(5)]
    out = _summarize_mcp_result(items, 500)
    assert len(out["items"]) == 3
    assert out["n_total"] == 5


# ── Auditor.log → file ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_log_writes_jsonl_to_file(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    a = Auditor(dest=str(log_path))
    await a.log(
        tool="execute_command", scope_id="hares",
        input_args={"command": "ls"}, duration_ms=12.3,
    )
    a.close()
    entries = _read_jsonl(log_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["tool"] == "execute_command"
    assert e["scope_id"] == "hares"
    assert e["input"] == {"command": "ls"}
    assert e["duration_ms"] == 12.3
    assert "ts" in e


@pytest.mark.asyncio
async def test_log_redacts_env_by_default(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    a = Auditor(dest=str(log_path))
    await a.log(
        tool="execute_command", scope_id=None,
        input_args={"command": "x", "env": {"SECRET": "abc"}},
        duration_ms=1.0,
    )
    a.close()
    entries = _read_jsonl(log_path)
    assert entries[0]["input"]["env"] == {"SECRET": "<redacted>"}


@pytest.mark.asyncio
async def test_log_with_explicit_no_redact_keeps_env(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    a = Auditor(dest=str(log_path), redact_fields=frozenset())
    await a.log(
        tool="execute_command", scope_id=None,
        input_args={"command": "x", "env": {"K": "v"}},
        duration_ms=1.0,
    )
    a.close()
    entries = _read_jsonl(log_path)
    assert entries[0]["input"]["env"] == {"K": "v"}


@pytest.mark.asyncio
async def test_log_includes_error_field_when_provided(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    a = Auditor(dest=str(log_path))
    await a.log(
        tool="x", scope_id=None, input_args={},
        duration_ms=1.0, error="ValueError('boom')",
    )
    a.close()
    entries = _read_jsonl(log_path)
    assert entries[0]["error"] == "ValueError('boom')"
    assert "result" not in entries[0]


@pytest.mark.asyncio
async def test_log_summarizes_result(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    a = Auditor(dest=str(log_path))
    item = _FakeTextContent(json.dumps({"exit_code": 0}))
    await a.log(
        tool="execute_command", scope_id=None,
        input_args={"command": "x"}, duration_ms=1.0,
        result=[item],
    )
    a.close()
    entries = _read_jsonl(log_path)
    assert entries[0]["result"]["items"][0]["value"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_log_appends_multiple_entries(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    a = Auditor(dest=str(log_path))
    for i in range(5):
        await a.log(tool="t", scope_id=None,
                    input_args={"i": i}, duration_ms=1.0)
    a.close()
    entries = _read_jsonl(log_path)
    assert [e["input"]["i"] for e in entries] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_log_creates_parent_dir(tmp_path):
    log_path = tmp_path / "deep" / "nested" / "audit.jsonl"
    a = Auditor(dest=str(log_path))
    await a.log(tool="t", scope_id=None, input_args={}, duration_ms=1.0)
    a.close()
    assert log_path.exists()


# ── HMAC signing ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_log_with_hmac_includes_signature(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    secret = b"super-secret-32-bytes-of-entropy"
    a = Auditor(dest=str(log_path), hmac_secret=secret)
    await a.log(tool="t", scope_id="s",
                input_args={"x": 1}, duration_ms=2.0)
    a.close()
    entry = _read_jsonl(log_path)[0]
    assert "hmac" in entry
    assert len(entry["hmac"]) == 64  # sha256 hex


@pytest.mark.asyncio
async def test_hmac_is_verifiable_with_same_secret(tmp_path):
    """Verifier reproduces the canonical bytes + computes the same HMAC."""
    log_path = tmp_path / "audit.jsonl"
    secret = b"a" * 32
    a = Auditor(dest=str(log_path), hmac_secret=secret)
    await a.log(tool="t", scope_id="s",
                input_args={"x": 1}, duration_ms=2.0)
    a.close()
    entry = _read_jsonl(log_path)[0]
    sig = entry.pop("hmac")
    canonical = json.dumps(
        entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    assert sig == expected


@pytest.mark.asyncio
async def test_hmac_changes_when_field_modified(tmp_path):
    """A tampered entry's recomputed HMAC won't match the stored one."""
    log_path = tmp_path / "audit.jsonl"
    secret = b"x" * 32
    a = Auditor(dest=str(log_path), hmac_secret=secret)
    await a.log(tool="t", scope_id="s",
                input_args={"cmd": "ls"}, duration_ms=2.0)
    a.close()
    entry = _read_jsonl(log_path)[0]
    sig = entry["hmac"]
    # Modify the input.
    entry["input"]["cmd"] = "rm -rf /"
    entry_no_sig = {k: v for k, v in entry.items() if k != "hmac"}
    canonical = json.dumps(
        entry_no_sig, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    recomputed = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    assert recomputed != sig


# ── disabled state ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disabled_auditor_is_noop():
    a = Auditor(dest=None)
    assert a.disabled
    # Should not raise, should not need to write anything.
    await a.log(tool="t", scope_id=None, input_args={}, duration_ms=1.0)


# ── load_auditor (env-driven) ──────────────────────────────────────────────

def test_load_auditor_returns_none_when_unset(monkeypatch):
    _clean_env(monkeypatch)
    assert load_auditor() is None


def test_load_auditor_with_file_dest(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    p = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HARES_AUDIT_LOG", str(p))
    a = load_auditor()
    assert a is not None
    assert a.dest == str(p)
    assert a.hmac_secret is None


def test_load_auditor_with_stderr(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_AUDIT_LOG", "stderr")
    a = load_auditor()
    assert a.dest == "stderr"


def test_load_auditor_with_hmac_secret(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    monkeypatch.setenv("HARES_AUDIT_HMAC_SECRET", "abc" * 12)
    a = load_auditor()
    assert a.hmac_secret == ("abc" * 12).encode()


def test_load_auditor_custom_redact_fields(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    monkeypatch.setenv("HARES_AUDIT_REDACT_FIELDS", "env,api_key,token")
    a = load_auditor()
    assert a.redact_fields == frozenset({"env", "api_key", "token"})


def test_load_auditor_empty_redact_means_no_redaction(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    monkeypatch.setenv("HARES_AUDIT_REDACT_FIELDS", "")
    a = load_auditor()
    assert a.redact_fields == frozenset()


def test_load_auditor_invalid_max_chars_falls_back(monkeypatch, tmp_path, caplog):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HARES_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    monkeypatch.setenv("HARES_AUDIT_MAX_VALUE_CHARS", "not-an-int")
    a = load_auditor()
    assert a.max_value_chars == 500


# ── audited decorator ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audited_logs_successful_call(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    auditor = Auditor(dest=str(log_path))

    @audited(auditor, scope_id="hares")
    async def handler(name: str, args: dict):
        return [_FakeTextContent(json.dumps({"ok": True}))]

    result = await handler("execute_command", {"command": "ls"})
    auditor.close()
    assert result[0].text == json.dumps({"ok": True})
    entries = _read_jsonl(log_path)
    assert len(entries) == 1
    assert entries[0]["tool"] == "execute_command"
    assert entries[0]["scope_id"] == "hares"
    assert entries[0]["result"]["items"][0]["value"]["ok"] is True


@pytest.mark.asyncio
async def test_audited_logs_then_reraises_on_exception(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    auditor = Auditor(dest=str(log_path))

    @audited(auditor, scope_id="s")
    async def handler(name, args):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await handler("t", {})
    auditor.close()
    entries = _read_jsonl(log_path)
    assert len(entries) == 1
    assert "ValueError" in entries[0]["error"]
    assert "result" not in entries[0]


@pytest.mark.asyncio
async def test_audited_with_none_auditor_is_passthrough():
    @audited(None, scope_id="s")
    async def handler(name, args):
        return [_FakeTextContent("hi")]
    result = await handler("t", {})
    assert result[0].text == "hi"


@pytest.mark.asyncio
async def test_audited_with_disabled_auditor_is_passthrough():
    auditor = Auditor(dest=None)

    @audited(auditor, scope_id="s")
    async def handler(name, args):
        return [_FakeTextContent("hi")]
    result = await handler("t", {})
    assert result[0].text == "hi"


@pytest.mark.asyncio
async def test_audited_records_duration(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    auditor = Auditor(dest=str(log_path))

    @audited(auditor, scope_id=None)
    async def handler(name, args):
        await asyncio.sleep(0.02)
        return [_FakeTextContent("ok")]

    await handler("t", {})
    auditor.close()
    e = _read_jsonl(log_path)[0]
    assert e["duration_ms"] >= 15  # ~20ms minus float jitter


@pytest.mark.asyncio
async def test_audited_concurrent_calls_dont_interleave(tmp_path):
    """Multiple concurrent calls must produce well-formed JSONL — no
    half-written lines from interleaved writes."""
    log_path = tmp_path / "audit.jsonl"
    auditor = Auditor(dest=str(log_path))

    @audited(auditor, scope_id=None)
    async def handler(name, args):
        await asyncio.sleep(0.001)
        return [_FakeTextContent(json.dumps({"i": args["i"]}))]

    await asyncio.gather(*[handler("t", {"i": i}) for i in range(20)])
    auditor.close()
    entries = _read_jsonl(log_path)
    assert len(entries) == 20
    # Order isn't guaranteed but every i should appear exactly once.
    seen = sorted(e["input"]["i"] for e in entries)
    assert seen == list(range(20))


# ── integration: stderr destination ────────────────────────────────────────

@pytest.mark.asyncio
async def test_log_to_stderr(capsys):
    a = Auditor(dest="stderr")
    await a.log(tool="t", scope_id=None, input_args={"x": 1}, duration_ms=1.0)
    captured = capsys.readouterr()
    assert "tool" in captured.err
    assert json.loads(captured.err.splitlines()[-1])["tool"] == "t"
