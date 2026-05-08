"""Structured audit log for tool calls.

Opt-in via env. When ``HARES_AUDIT_LOG`` is set to a file path or
``stderr``/``stdout``, every tool call is captured as a JSON line:

    {"ts": "...", "tool": "execute_command", "scope_id": "src",
     "input": {...redacted...}, "result": {...summarized...},
     "duration_ms": 412.3}

Two ergonomic concerns drive the redaction defaults:

  - ``env`` field on shell/cluster job specs frequently carries
    secrets (API keys, tokens). Default-redacted: keys are kept,
    values replaced with ``"<redacted>"``. Operators with non-secret
    env can opt out via ``HARES_AUDIT_REDACT_FIELDS=`` (empty).
  - File contents (``write_file`` / ``edit_file`` body, command
    stdout/stderr) are unbounded. Default-truncated to 500 chars
    per string field plus an ``"…<N more chars elided>"`` marker so
    the total line stays grep-friendly.

Optional HMAC signing via ``HARES_AUDIT_HMAC_SECRET`` lets a
downstream verifier confirm individual entries weren't modified after
the fact. Per-entry HMAC only — chained signing (each entry includes
the prior entry's HMAC, defending against deletion) is a future
extension.

Concurrency: an asyncio.Lock guards the file write to keep entries
atomic. POSIX O_APPEND would suffice for short lines but truncated
file content can exceed PIPE_BUF, so the lock is the conservative
choice.

Output destinations:
  - file path (anything else): opened in append mode, lazily
  - "stderr"                : sys.stderr
  - "stdout"                : sys.stdout (rare; stdout is the MCP
                              protocol channel for stdio-mode servers,
                              so this is generally a footgun)
  - unset / empty           : disabled, no-op
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TextIO

logger = logging.getLogger(__name__)

# Hard cap on per-string truncation. Fields longer than this become a
# preview + length marker. Keeps lines shell-friendly.
_DEFAULT_MAX_VALUE_CHARS = 500

# Default fields to redact across all tool inputs.
_DEFAULT_REDACT_FIELDS = frozenset({"env"})


# ── Helpers ──────────────────────────────────────────────────────────────────

def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return f"{s[:n]}…<{len(s) - n} more chars elided>"


def _redact_value(value: Any, redact_fields: frozenset[str], max_chars: int) -> Any:
    """Recursively walk a value, redacting/truncating per the rules."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in redact_fields:
                if isinstance(v, dict):
                    out[k] = {kk: "<redacted>" for kk in v}
                else:
                    out[k] = "<redacted>"
            else:
                out[k] = _redact_value(v, redact_fields, max_chars)
        return out
    if isinstance(value, list):
        # Cap list length too, to avoid one giant array hijacking a line.
        return [_redact_value(x, redact_fields, max_chars) for x in value[:50]]
    if isinstance(value, str):
        return _truncate(value, max_chars)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Fallback: stringify and truncate.
    return _truncate(repr(value), max_chars)


def _summarize_mcp_result(result: Any, max_chars: int) -> Any:
    """MCP tools return list[TextContent]. Most handlers in this codebase
    JSON-encode their dict result as the only TextContent. Try to parse
    that back so the audit entry is structured; fall back to a preview.
    """
    if not result:
        return {"empty": True}
    if not isinstance(result, list):
        return {"non_list_result": _truncate(repr(result), max_chars)}
    items: list[Any] = []
    for item in result[:3]:
        text = getattr(item, "text", None)
        if text is None:
            items.append({"type": "non_text", "preview": _truncate(repr(item), max_chars)})
            continue
        try:
            parsed = json.loads(text)
            items.append({
                "type": "json",
                "value": _redact_value(parsed, frozenset(), max_chars),
            })
        except (json.JSONDecodeError, ValueError):
            items.append({
                "type": "text",
                "len": len(text),
                "preview": _truncate(text, max_chars),
            })
    out: dict[str, Any] = {"items": items}
    if len(result) > 3:
        out["n_total"] = len(result)
    return out


# ── Auditor ──────────────────────────────────────────────────────────────────

class Auditor:
    """Append-only structured logger for tool calls.

    Construct via ``load_auditor()`` to read env vars; or directly with
    explicit args for tests.

    Set ``dest=None`` for a disabled auditor that no-ops on every call;
    callers can apply the ``audited`` decorator unconditionally.
    """

    def __init__(
        self,
        dest: Optional[str],
        *,
        hmac_secret: Optional[bytes] = None,
        redact_fields: frozenset[str] = _DEFAULT_REDACT_FIELDS,
        max_value_chars: int = _DEFAULT_MAX_VALUE_CHARS,
    ) -> None:
        self.dest = dest
        self.hmac_secret = hmac_secret
        self.redact_fields = redact_fields
        self.max_value_chars = max_value_chars
        self._lock = asyncio.Lock()
        self._fp: Optional[TextIO] = None  # opened lazily on first write

    @property
    def disabled(self) -> bool:
        return not self.dest

    def _open(self) -> TextIO:
        """Resolve dest to an open file handle. Cached after first call."""
        if self._fp is not None:
            return self._fp
        if self.dest == "stderr":
            self._fp = sys.stderr
        elif self.dest == "stdout":
            self._fp = sys.stdout
        else:
            assert self.dest is not None
            path = Path(os.path.expanduser(os.path.expandvars(self.dest))).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Line-buffered append. The asyncio.Lock guards atomicity
            # across coroutines; line-buffering ensures each write is
            # immediately visible (important for tail -f).
            self._fp = path.open("a", buffering=1)
        return self._fp

    def _sign(self, entry: dict[str, Any]) -> str:
        """HMAC-SHA256 over canonical bytes (sorted keys, compact separators)."""
        assert self.hmac_secret is not None
        canonical = json.dumps(
            {k: v for k, v in entry.items() if k != "hmac"},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(self.hmac_secret, canonical, hashlib.sha256).hexdigest()

    def _build_entry(
        self,
        *,
        tool: str,
        scope_id: Optional[str],
        input_args: dict[str, Any],
        duration_ms: float,
        result: Any = None,
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "scope_id": scope_id,
            "input": _redact_value(
                input_args, self.redact_fields, self.max_value_chars,
            ),
            "duration_ms": round(duration_ms, 1),
        }
        if error is not None:
            entry["error"] = _truncate(error, self.max_value_chars)
        if result is not None:
            entry["result"] = _summarize_mcp_result(result, self.max_value_chars)
        if self.hmac_secret is not None:
            entry["hmac"] = self._sign(entry)
        return entry

    async def log(
        self,
        *,
        tool: str,
        scope_id: Optional[str],
        input_args: dict[str, Any],
        duration_ms: float,
        result: Any = None,
        error: Optional[str] = None,
    ) -> None:
        """Write one structured entry. No-op when disabled."""
        if self.disabled:
            return
        entry = self._build_entry(
            tool=tool, scope_id=scope_id, input_args=input_args,
            duration_ms=duration_ms, result=result, error=error,
        )
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        async with self._lock:
            try:
                fp = self._open()
                fp.write(line)
                fp.flush()
            except OSError as exc:
                # Failing the audit must NOT fail the tool call. Log to
                # stderr and carry on — operators see audit-disabled
                # behavior, which is more useful than a broken server.
                logger.error(
                    "audit log write failed (dest=%r): %s; tool=%s scope_id=%s",
                    self.dest, exc, tool, scope_id,
                )

    def close(self) -> None:
        """Close the file handle. Optional — process exit cleans up
        anyway. Useful for tests."""
        if self._fp is not None and self._fp not in (sys.stdout, sys.stderr):
            try:
                self._fp.close()
            except OSError:
                pass
        self._fp = None


# ── Loader from env ──────────────────────────────────────────────────────────

def load_auditor() -> Optional[Auditor]:
    """Build an Auditor from HARES_AUDIT_* env vars, or return None
    when ``HARES_AUDIT_LOG`` is unset (disabled).

    Env vars:
      HARES_AUDIT_LOG            file path or "stderr" / "stdout"
      HARES_AUDIT_HMAC_SECRET    32+ bytes for tamper-evident entries
      HARES_AUDIT_REDACT_FIELDS  comma-separated; defaults to "env".
                                 Pass empty string to redact nothing.
      HARES_AUDIT_MAX_VALUE_CHARS  per-string truncation (default 500)
    """
    dest = os.environ.get("HARES_AUDIT_LOG", "").strip()
    if not dest:
        return None
    secret_raw = os.environ.get("HARES_AUDIT_HMAC_SECRET", "").strip()
    secret = secret_raw.encode("utf-8") if secret_raw else None
    redact_raw = os.environ.get("HARES_AUDIT_REDACT_FIELDS")
    if redact_raw is None:
        redact = _DEFAULT_REDACT_FIELDS
    else:
        # Empty string ⇒ redact nothing. Comma-separated otherwise.
        redact = frozenset(
            x.strip() for x in redact_raw.split(",") if x.strip()
        )
    max_chars_raw = os.environ.get("HARES_AUDIT_MAX_VALUE_CHARS", "").strip()
    if max_chars_raw:
        try:
            max_chars = max(1, int(max_chars_raw))
        except ValueError:
            logger.warning(
                "HARES_AUDIT_MAX_VALUE_CHARS=%r is not an int; using default %d",
                max_chars_raw, _DEFAULT_MAX_VALUE_CHARS,
            )
            max_chars = _DEFAULT_MAX_VALUE_CHARS
    else:
        max_chars = _DEFAULT_MAX_VALUE_CHARS
    return Auditor(
        dest=dest,
        hmac_secret=secret,
        redact_fields=redact,
        max_value_chars=max_chars,
    )


# ── Decorator for MCP call_tool handlers ─────────────────────────────────────

# Type alias: a call_tool handler takes (tool_name, arguments_dict) and
# returns a list[TextContent] (or whatever the MCP server expects).
Handler = Callable[[str, dict[str, Any]], Awaitable[Any]]


def audited(
    auditor: Optional[Auditor],
    *,
    scope_id: Optional[str],
) -> Callable[[Handler], Handler]:
    """Decorator: wrap an MCP call_tool handler with audit logging.

    No-op when ``auditor`` is None or disabled — safe to apply
    unconditionally so server modules don't need an if-statement.

    Usage:

        @server.call_tool()
        @audited(auditor, scope_id=scope_id)
        async def _call_tool(name, arguments):
            ...
    """
    def decorator(handler: Handler) -> Handler:
        if auditor is None or auditor.disabled:
            return handler

        @functools.wraps(handler)
        async def wrapped(name: str, arguments: dict[str, Any]) -> Any:
            start = time.perf_counter()
            try:
                result = await handler(name, arguments)
            except BaseException as exc:
                duration_ms = (time.perf_counter() - start) * 1000
                # NB: log-then-reraise. Use a shielded log so cancellation
                # mid-log doesn't lose the entry.
                try:
                    await asyncio.shield(auditor.log(
                        tool=name, scope_id=scope_id,
                        input_args=arguments, duration_ms=duration_ms,
                        error=repr(exc),
                    ))
                except BaseException:
                    pass  # never let audit failure mask the original
                raise
            duration_ms = (time.perf_counter() - start) * 1000
            await auditor.log(
                tool=name, scope_id=scope_id,
                input_args=arguments, duration_ms=duration_ms,
                result=result,
            )
            return result

        return wrapped
    return decorator
