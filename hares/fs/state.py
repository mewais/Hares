"""Atomic state-file persistence for the active scope.

The active scope is the runtime-narrowed subset of the ceiling that
the instance's tool calls (and shell-spawned subprocesses, when
applicable) are allowed to write to. It can be set at runtime via
the ``restrict_paths`` tool and persists across server restarts
when ``--state-file`` is provided.

State file format (version: 2):

    {
      "version": 2,
      "scope_id": "src",          // or null when the instance is unscoped
      "ceiling": "/work/proj",    // resolved absolute
      "active_paths": [
        "/work/proj/lib/parser",
        "/work/proj/lib/lexer"
      ],
      "seq": 7,                   // monotonic counter, +1 per restrict_paths
      "last_restrict_at": "2026-05-05T10:30:00Z"
    }

Crash recovery: on startup, attempt load. On JSONDecodeError, schema
mismatch, scope_id mismatch, or ceiling mismatch, fall back to "no
active scope" (instance starts with active = the entire ceiling).
The fallback is logged at WARNING so operators notice; agents
should re-call ``restrict_paths`` to re-narrow.

Sequence numbering (0.2.1): ``seq`` is a monotonic counter that
increments by 1 on every successful ``restrict_paths`` call. It lets
external auditors (e.g. Bunyan's seal_bundle cross-check) detect
replay attacks: an attacker with state-file write access could
otherwise swap a tighter scope back to a stale, looser one between
the agent's last restrict_paths and the auditor's get_active_paths
read. With seq, the auditor tracks the latest seq it expected and
rejects any get_active_paths reply whose seq is BELOW that.
``restrict_paths`` also accepts an optional ``expected_seq`` arg —
if provided, the call refuses with ScopeSeqMismatch unless the
current seq matches. Compare-and-swap semantics for race-free
narrowing.

Backward compat: state files written by 0.2.0 (version=1, no seq
field) are detected by the version check and the instance starts
with an empty scope + seq=0. Operators upgrading should expect a
single warn-and-rebuild on first start under 0.2.1; agents
re-call restrict_paths and the new seq begins at 1.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


STATE_VERSION = 2


class ScopeSeqMismatch(Exception):
    """Raised by ``ScopeStateStore.set`` when ``expected_seq`` is provided
    and doesn't match the current scope's seq. Indicates either a
    legitimate concurrent narrower (caller should re-read with
    get_active_paths and decide whether to retry) or a replay attempt
    against a stale view of the scope."""

    def __init__(self, *, expected: int, actual: int) -> None:
        super().__init__(
            f"expected_seq={expected} but current seq={actual} — "
            f"scope was narrowed by another caller since you last read it; "
            f"re-read via get_active_paths and decide whether to retry."
        )
        self.expected = expected
        self.actual = actual


@dataclass
class ActiveScope:
    """In-memory representation of the active scope.

    Loaded from state file on startup; saved on every restrict call.
    Mutable in the sense that ``ScopeStateStore.set`` constructs a
    fresh instance and assigns it; the contents of any one instance
    are stable for as long as it's referenced.
    """

    paths: list[Path] = field(default_factory=list)
    last_restrict_at: Optional[datetime] = None
    scope_id: Optional[str] = None
    ceiling: Optional[Path] = None
    seq: int = 0


class ScopeStateStore:
    """Owns the in-memory ActiveScope plus its persistence.

    Construction reads the state file (if any) and validates it
    against the instance's scope_id + ceiling. On mismatch, falls
    back to a fresh empty scope.

    Thread / async safety: this class assumes single-writer (only the
    MCP server's call_tool dispatcher mutates state). Reads are
    cheap dict snapshots and don't need locking. If a future caller
    multi-writes, add an asyncio.Lock around set().
    """

    def __init__(
        self,
        *,
        scope_id: Optional[str],
        ceiling: Path,
        state_file: Optional[Path] = None,
    ) -> None:
        self._scope_id = scope_id
        self._ceiling = ceiling.resolve(strict=False)
        self._state_file = state_file
        self._scope = self._load_or_default()

    @property
    def scope_id(self) -> Optional[str]:
        return self._scope_id

    @property
    def ceiling(self) -> Path:
        return self._ceiling

    def current(self) -> ActiveScope:
        return self._scope

    def set(
        self,
        paths: list[Path],
        *,
        expected_seq: Optional[int] = None,
    ) -> ActiveScope:
        """Replace the active scope. Persists immediately if a state
        file was configured. Returns the new ActiveScope.

        ``expected_seq`` (0.2.1): if provided, raise ScopeSeqMismatch
        unless the current scope's seq equals expected_seq. Lets
        callers do compare-and-swap narrowing — useful when multiple
        agents share a scope and you want "tighten only if no one
        else has changed it since I last read."
        """
        if expected_seq is not None and expected_seq != self._scope.seq:
            raise ScopeSeqMismatch(
                expected=expected_seq, actual=self._scope.seq,
            )
        new = ActiveScope(
            paths=[p.resolve(strict=False) for p in paths],
            last_restrict_at=datetime.now(timezone.utc),
            scope_id=self._scope_id,
            ceiling=self._ceiling,
            seq=self._scope.seq + 1,
        )
        self._scope = new
        if self._state_file is not None:
            self._save(new)
        return new

    # ── Internal: load + save ─────────────────────────────────────────

    def _load_or_default(self) -> ActiveScope:
        if self._state_file is None:
            return ActiveScope(
                scope_id=self._scope_id, ceiling=self._ceiling,
            )
        if not self._state_file.exists():
            return ActiveScope(
                scope_id=self._scope_id, ceiling=self._ceiling,
            )
        try:
            text = self._state_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "ScopeStateStore: read failed for %s (%s); starting "
                "with empty scope.", self._state_file, exc,
            )
            return ActiveScope(
                scope_id=self._scope_id, ceiling=self._ceiling,
            )
        if not text.strip():
            return ActiveScope(
                scope_id=self._scope_id, ceiling=self._ceiling,
            )
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "ScopeStateStore: state file %s is corrupt (%s); "
                "starting with empty scope. Re-call "
                "restrict_paths to re-narrow.",
                self._state_file, exc,
            )
            return ActiveScope(
                scope_id=self._scope_id, ceiling=self._ceiling,
            )
        # Schema sanity checks.
        if data.get("version") != STATE_VERSION:
            logger.warning(
                "ScopeStateStore: state file %s version mismatch "
                "(got %r, expected %d); starting with empty scope.",
                self._state_file, data.get("version"), STATE_VERSION,
            )
            return ActiveScope(
                scope_id=self._scope_id, ceiling=self._ceiling,
            )
        # Instance-identity sanity checks (defends against accidentally
        # reusing a state file across instances with different shapes).
        if data.get("scope_id") != self._scope_id:
            logger.warning(
                "ScopeStateStore: state file %s was written by an "
                "instance with scope_id=%r but this instance has "
                "scope_id=%r; starting with empty scope.",
                self._state_file, data.get("scope_id"), self._scope_id,
            )
            return ActiveScope(
                scope_id=self._scope_id, ceiling=self._ceiling,
            )
        try:
            stored_ceiling = Path(data["ceiling"]).resolve(strict=False)
        except Exception:
            stored_ceiling = None
        if stored_ceiling != self._ceiling:
            logger.warning(
                "ScopeStateStore: state file %s was written under "
                "ceiling=%r but this instance has ceiling=%r; "
                "starting with empty scope.",
                self._state_file, str(stored_ceiling), str(self._ceiling),
            )
            return ActiveScope(
                scope_id=self._scope_id, ceiling=self._ceiling,
            )
        # Parse paths + timestamp + seq.
        paths = [Path(p).resolve(strict=False) for p in data.get("active_paths", [])]
        ts_raw = data.get("last_restrict_at")
        last_restrict_at: Optional[datetime] = None
        if ts_raw:
            try:
                last_restrict_at = datetime.fromisoformat(ts_raw)
            except ValueError:
                last_restrict_at = None
        # 0.2.1: seq was added in STATE_VERSION 2. Defensive int-coerce
        # in case the file is hand-edited to a non-int value (treat as
        # 0; the next set() will increment to 1 and re-save).
        seq_raw = data.get("seq", 0)
        try:
            seq = int(seq_raw)
            if seq < 0:
                raise ValueError(f"negative seq {seq}")
        except (TypeError, ValueError) as exc:
            logger.warning(
                "ScopeStateStore: state file %s has malformed seq=%r "
                "(%s); resetting seq to 0. Replay-defense for this "
                "scope is degraded until the next restrict_paths call.",
                self._state_file, seq_raw, exc,
            )
            seq = 0
        logger.info(
            "ScopeStateStore: restored active scope from %s "
            "(scope_id=%r, %d paths, seq=%d)",
            self._state_file, self._scope_id, len(paths), seq,
        )
        return ActiveScope(
            paths=paths,
            last_restrict_at=last_restrict_at,
            scope_id=self._scope_id,
            ceiling=self._ceiling,
            seq=seq,
        )

    def _save(self, scope: ActiveScope) -> None:
        """Atomic write via tmp + rename, with fsync on the tmp."""
        assert self._state_file is not None
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        payload = {
            "version": STATE_VERSION,
            "scope_id": self._scope_id,
            "ceiling": str(self._ceiling),
            "active_paths": [str(p) for p in scope.paths],
            "seq": scope.seq,
            "last_restrict_at": (
                scope.last_restrict_at.isoformat()
                if scope.last_restrict_at else None
            ),
        }
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        # fsync on the tmp file so the rename atomicity actually wins
        # under power-loss scenarios. No-op if the platform doesn't
        # support file-descriptor fsync.
        try:
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass
        os.replace(tmp, self._state_file)
