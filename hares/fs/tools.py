"""Shared restrict-tool dispatchers used by both the fs and shell
servers.

The two tools registered:

* ``restrict_paths(paths: list[str])`` — narrows the active scope
  to ``paths``. Set-replace semantics (not accumulate). Each path
  is resolved under the ceiling, validated for traversal +
  system-dir safety, and ``mkdir -p``'d if it doesn't exist (mirrors
  npm filesystem-server behavior). Persists to the state file
  immediately.

* ``get_active_paths() -> {"active_paths": [...]}`` —
  introspect the current active scope. Useful for any agent or
  external auditor that wants to verify the scope it's operating
  under.

Both tools are ALWAYS registered on any Hares MCP instance with
``--enable=fs``, ``=shell``, or ``=fs+shell``. Whether any agent
gets to USE them is determined by the agent's tools allowlist
(client-side concern) — Hares is permissive at the registry level.

Tool names are optionally prefixed with ``<scope_id>_`` per the
unified naming convention.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from mcp.types import Tool

from ..grants import GrantStore
from ..path_safety import (
    PathSafetyError,
    resolve_under_ceiling,
)
from .state import (
    ScopeSeqMismatch,
    ScopeStateStore,
    STATE_VERSION,
    compute_state_hmac,
)

logger = logging.getLogger(__name__)


RESTRICT_PATHS_TOOL = "restrict_paths"
GET_ACTIVE_PATHS_TOOL = "get_active_paths"


def _prefixed(name: str, scope_id: Optional[str]) -> str:
    return f"{scope_id}_{name}" if scope_id else name


def restrict_tool_descriptors(scope_id: Optional[str]) -> list[Tool]:
    """Return the two MCP Tool descriptors with optional scope prefix."""
    return [
        Tool(
            name=_prefixed(RESTRICT_PATHS_TOOL, scope_id),
            description=(
                "Narrow the instance's active scope to the given paths "
                "(within --ceiling). Set-replace semantics: each call "
                "REPLACES the active scope, doesn't accumulate. For fs "
                "instances: subsequent write_file/edit_file/etc. calls "
                "are bounded to under one of these paths. For shell "
                "instances: bwrap re-spawns with these paths as the RW "
                "mount list (kernel-enforced). Each path is resolved "
                "under the ceiling, validated for traversal/system-dir "
                "safety, and mkdir-p'd if missing. Persists to the "
                "state file (when --state-file is set) so the scope "
                "survives a server restart."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Paths to set as the new active scope. Each "
                            "may be absolute or relative to the ceiling. "
                            "Empty list = no writes/runs allowed."
                        ),
                    },
                    "expected_seq": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "0.2.1: optional compare-and-swap. If "
                            "provided, the call refuses unless the "
                            "current scope's seq equals expected_seq. "
                            "Lets a caller race-safely tighten only if "
                            "no other agent has changed the scope since "
                            "the caller's last get_active_paths read."
                        ),
                    },
                },
                "required": ["paths"],
            },
        ),
        Tool(
            name=_prefixed(GET_ACTIVE_PATHS_TOOL, scope_id),
            description=(
                "Report what is currently writable: 'active_paths' is the "
                "current active scope (the paths writable now; empty means "
                "the whole ceiling, unless restrict_paths has narrowed it), "
                "and 'grants' lists any runtime request_path_access grants "
                "in effect (path, mode, lifetime) — outside-ceiling access a "
                "human approved, in-memory only and never persisted. Call "
                "this to check the writable surface before a write, or to "
                "see which grants are still live. "
                "The reply also carries a monotonic 'seq' and an 'hmac' over "
                "the canonical (version, scope_id, ceiling, seq, "
                "sorted_paths) payload — these are for OUT-OF-PROCESS "
                "verifiers, not the calling agent: a supervisor sharing "
                "HARES_STATE_HMAC_SECRET enforces reply.seq == expected_next "
                "(not >=, which an attacker could fast-forward past) and "
                "checks the HMAC to detect a forged or replayed scope. The "
                "'grants' list is in-memory only and is NOT covered by the "
                "seq/HMAC."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


def build_restrict_tool_handlers(
    *,
    scope_id: Optional[str],
    ceiling: Path,
    state_file: Optional[Path],
    scope_state: Optional[ScopeStateStore] = None,
    on_change: Optional[Callable[[], None]] = None,
    grant_store: Optional[GrantStore] = None,
) -> tuple[ScopeStateStore, dict[str, Callable[[dict], Awaitable[dict]]]]:
    """Construct the per-instance state store + bound async handlers
    for the two restrict tools.

    Args:
      scope_id: Optional tool-name prefix.
      ceiling: Instance's outer bound.
      state_file: Optional persistence path.
      scope_state: If provided, reuse this ScopeStateStore (server.py
        builds one and passes it). Otherwise construct a fresh one.
      on_change: Optional callable invoked after each successful
        ``restrict_paths`` call. Used by the shell server to notify
        the Runner that the active scope changed (re-spawn bwrap with
        new mounts on next execute_command).
      grant_store: Optional GrantStore whose active runtime
        path-access grants (see ``request_path_access`` /
        :mod:`hares.grant_tools`) are surfaced in ``get_active_paths``
        output under the ``"grants"`` key. None → an empty list is
        reported (no grants feature wired up for this instance).

    Returns:
      (scope_state, handlers) where handlers is name → async-callable.
    """
    if scope_state is None:
        scope_state = ScopeStateStore(
            scope_id=scope_id, ceiling=ceiling, state_file=state_file,
        )
    name_restrict = _prefixed(RESTRICT_PATHS_TOOL, scope_id)
    name_get = _prefixed(GET_ACTIVE_PATHS_TOOL, scope_id)

    async def _restrict(args: dict) -> dict:
        raw_paths = args.get("paths", [])
        if not isinstance(raw_paths, list):
            raise ValueError(
                f"{RESTRICT_PATHS_TOOL}: 'paths' must be a list of strings"
            )
        # 0.2.1: optional CAS arg. Validate before doing any disk work
        # (mkdir-p) so a mismatch fails fast without side effects.
        expected_seq_raw = args.get("expected_seq")
        if expected_seq_raw is not None and not isinstance(expected_seq_raw, int):
            raise ValueError(
                f"{RESTRICT_PATHS_TOOL}: 'expected_seq' must be an integer "
                f"or omitted (got {type(expected_seq_raw).__name__})"
            )
        if isinstance(expected_seq_raw, int) and expected_seq_raw < 0:
            raise ValueError(
                f"{RESTRICT_PATHS_TOOL}: 'expected_seq' must be >= 0 "
                f"(got {expected_seq_raw})"
            )
        resolved: list[Path] = []
        for p in raw_paths:
            if not isinstance(p, str) or not p:
                raise ValueError(
                    f"{RESTRICT_PATHS_TOOL}: each path must be a non-empty string"
                )
            target = resolve_under_ceiling(p, ceiling)
            # mkdir -p on the target (mirrors npm filesystem-server's
            # mkdir-on-write behavior; means callers can declare
            # paths that don't yet exist on disk).
            target.mkdir(parents=True, exist_ok=True)
            resolved.append(target)
        try:
            new_scope = scope_state.set(
                resolved, expected_seq=expected_seq_raw,
            )
        except ScopeSeqMismatch as exc:
            # Surface a structured error instead of a 500 — consumers
            # check this exact shape to decide whether to retry or
            # escalate.
            logger.warning(
                "%s: CAS mismatch (expected_seq=%d, current=%d) — "
                "scope was narrowed by another caller",
                name_restrict, exc.expected, exc.actual,
            )
            return {
                "error": "scope_seq_mismatch",
                "expected_seq": exc.expected,
                "current_seq": exc.actual,
                "message": str(exc),
            }
        logger.info(
            "Active scope updated for %s: %d paths, seq=%d",
            name_restrict, len(resolved), new_scope.seq,
        )
        if on_change is not None:
            try:
                on_change()
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "on_change callback raised (%s); active scope is "
                    "set but downstream notification failed.", exc,
                )
        sorted_path_strs = sorted(str(p) for p in new_scope.paths)
        sig = compute_state_hmac(
            version=STATE_VERSION,
            scope_id=scope_state.scope_id,
            ceiling=str(scope_state.ceiling),
            seq=new_scope.seq,
            sorted_paths=sorted_path_strs,
        )
        return {
            "active_paths": sorted_path_strs,
            "seq": new_scope.seq,
            # 0.2.2: HMAC over the canonical (version, scope_id,
            # ceiling, seq, sorted_paths) payload. Out-of-process
            # verifiers compute the same canonical form with the
            # shared HARES_STATE_HMAC_SECRET to detect a tampered
            # state file before trusting the reply.
            "hmac": sig,
            "version": STATE_VERSION,
        }

    async def _get_active(args: dict) -> dict:
        cur = scope_state.current()
        sorted_path_strs = sorted(str(p) for p in cur.paths)
        sig = compute_state_hmac(
            version=STATE_VERSION,
            scope_id=scope_state.scope_id,
            ceiling=str(scope_state.ceiling),
            seq=cur.seq,
            sorted_paths=sorted_path_strs,
        )
        return {
            "active_paths": sorted_path_strs,
            "seq": cur.seq,
            "hmac": sig,
            "version": STATE_VERSION,
            # Runtime request_path_access grants — in-memory only, NOT
            # part of the seq/HMAC-covered payload above (grants are
            # never persisted and don't survive a restart, so signing
            # them the same way would be misleading).
            "grants": grant_store.list_active() if grant_store is not None else [],
        }

    return scope_state, {
        name_restrict: _restrict,
        name_get: _get_active,
    }
