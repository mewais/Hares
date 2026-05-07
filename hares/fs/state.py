"""Atomic state-file persistence for the active scope.

The active scope is the runtime-narrowed subset of the ceiling that
the instance's tool calls (and shell-spawned subprocesses, when
applicable) are allowed to write to. It can be set at runtime via
the ``restrict_paths`` tool and persists across server restarts
when ``--state-file`` is provided.

State file format (version: 3):

    {
      "version": 3,
      "scope_id": "src",          // or null when the instance is unscoped
      "ceiling": "/work/proj",    // resolved absolute
      "active_paths": [
        "/work/proj/lib/parser",
        "/work/proj/lib/lexer"
      ],
      "seq": 7,                   // monotonic counter, +1 per restrict_paths
      "hmac": "<hex>",            // 0.2.2: HMAC over (version|scope_id|
                                  //         ceiling|seq|sorted_paths)
      "last_restrict_at": "2026-05-05T10:30:00Z"
    }

Crash recovery: on startup, attempt load. On JSONDecodeError, schema
mismatch, scope_id mismatch, ceiling mismatch, OR HMAC mismatch
(0.2.2), fall back to "no active scope" with seq=0. The fallback
is logged at WARNING so operators notice; agents re-call
``restrict_paths`` to re-narrow. The first successful restrict_paths
post-fallback gets seq=1.

WAL recovery (0.2.2): writes go through ``state.json.tmp`` first
(fsync'd), then ``os.replace`` to ``state.json``. If the process
crashes between fsync and rename, the next startup checks for a
``.tmp`` file with a HIGHER, HMAC-valid seq and prefers it
(committing the rename). Closes the round-3 false-positive-replay
window where a crash mid-write rolled the seq back below what the
external auditor last observed.

Sequence numbering (0.2.1) + HMAC signing (0.2.2):
``seq`` is a monotonic counter that increments by 1 on every
successful ``restrict_paths`` call. It lets external auditors
(e.g. Bunyan's seal_bundle cross-check) detect replay attacks: an
attacker with state-file write access could otherwise swap a
tighter scope back to a stale, looser one between the agent's last
restrict_paths and the auditor's get_active_paths read. With seq,
the auditor enforces ``seq == expected_next`` (NOT ``seq >=
expected``, which the round-3 reviewer flagged: an attacker writing
seq=10000000 fast-forwards past any expected value). The HMAC
protects against an attacker who has state-file write access from
forging a (seq, paths) pair: without the secret they can't compute
a valid HMAC, so the next load fails verification and falls back
to seq=0 + empty scope (visible to the auditor as a discontinuity).

The HMAC secret is operator-pinned via ``HARES_STATE_HMAC_SECRET``
(env var, expected to be 32+ bytes of base64/hex). When unset, the
process generates a random secret at startup; this defends the
in-process case but BREAKS verification across process restarts
(the new process has a fresh secret + can't verify the old file →
falls back to empty scope on load). Operators running multi-process
or restart-tolerant deployments MUST pin the secret. External
auditors (Bunyan) read the same env var to verify HMACs they
receive over the MCP protocol.

``restrict_paths`` also accepts an optional ``expected_seq`` arg —
if provided, the call refuses with ScopeSeqMismatch unless the
current seq matches. Compare-and-swap semantics for race-free
narrowing across multiple agents.

Backward compat: state files written by 0.2.0 (v1) or 0.2.1 (v2)
trigger the existing version-mismatch warn-and-rebuild path on
first start under 0.2.2. No data loss because the active scope is
advisory only — the ceiling + bwrap mounts are the kernel-enforced
bound. Agents re-call restrict_paths and the new seq begins at 1.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


STATE_VERSION = 3
_HMAC_SECRET_ENV = "HARES_STATE_HMAC_SECRET"

# Per-process random fallback for the HMAC secret. Set on first call
# to ``_state_hmac_secret`` when the env var is unset; never persisted,
# never logged. Operators running multi-process or restart-tolerant
# deployments MUST pin via env (otherwise the next process has a fresh
# secret and rejects the prior process's state file at load time).
_PROCESS_FALLBACK_SECRET: bytes | None = None


def _state_hmac_secret() -> bytes:
    """Return the HMAC key used to sign + verify state-file payloads.

    Prefers ``HARES_STATE_HMAC_SECRET`` env (operator-pinned, restart-
    safe; the only way for verification to outlive a process restart
    or to be checkable by an external auditor like Bunyan).

    Falls back to a process-local random secret with a one-time
    WARNING. The fallback defends the in-process case (an attacker
    with state-file write can't forge a valid HMAC because they
    don't know the secret) but breaks cross-process verification.
    """
    pinned = (os.environ.get(_HMAC_SECRET_ENV) or "").strip()
    if pinned:
        return pinned.encode("utf-8")
    global _PROCESS_FALLBACK_SECRET
    if _PROCESS_FALLBACK_SECRET is None:
        _PROCESS_FALLBACK_SECRET = _secrets.token_bytes(32)
        logger.warning(
            "HARES_STATE_HMAC_SECRET not set; using a process-local "
            "random key for state-file HMAC. Cross-process verification "
            "(including Bunyan's seal_bundle replay-defense check) "
            "will FAIL until the operator pins this env var to a "
            "high-entropy value (32+ bytes of base64 / hex). For "
            "single-process dev runs this fallback is fine.",
        )
    return _PROCESS_FALLBACK_SECRET


def canonical_hmac_payload(
    *,
    version: int,
    scope_id: Optional[str],
    ceiling: str,
    seq: int,
    sorted_paths: list[str],
) -> bytes:
    """Build the canonical bytes the HMAC signs over. Exposed so external
    verifiers (Bunyan) can compute the same canonical form. Order +
    separators are LOCKED IN — any change is a state-version bump."""
    # NUL-separated to defeat field-injection attacks (no path can
    # contain NUL on POSIX). version + seq are utf-8 decimal.
    parts = [
        str(version).encode("utf-8"),
        (scope_id or "").encode("utf-8"),
        ceiling.encode("utf-8"),
        str(seq).encode("utf-8"),
        b"\x00".join(p.encode("utf-8") for p in sorted_paths),
    ]
    return b"\x00\x01\x00".join(parts)


def compute_state_hmac(
    *,
    version: int,
    scope_id: Optional[str],
    ceiling: str,
    seq: int,
    sorted_paths: list[str],
    secret: bytes | None = None,
) -> str:
    """Return the hex-digest HMAC for the given canonical payload.
    Uses ``_state_hmac_secret()`` when ``secret`` is not provided —
    callers in Hares do that; external verifiers pass an explicit
    secret read from the same env var."""
    if secret is None:
        secret = _state_hmac_secret()
    payload = canonical_hmac_payload(
        version=version,
        scope_id=scope_id,
        ceiling=ceiling,
        seq=seq,
        sorted_paths=sorted_paths,
    )
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


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

    def _wal_path(self) -> Path:
        """0.2.2: WAL companion file. ``_save`` writes here first
        (fsync) then ``os.replace`` to ``_state_file``. On startup
        ``_load_or_default`` checks the WAL for a higher-seq,
        HMAC-valid payload and prefers it over the main file —
        recovers from crash-mid-write that would otherwise roll the
        seq back below what the external auditor last observed."""
        assert self._state_file is not None
        return self._state_file.with_suffix(self._state_file.suffix + ".tmp")

    def _load_or_default(self) -> ActiveScope:
        if self._state_file is None:
            return ActiveScope(
                scope_id=self._scope_id, ceiling=self._ceiling,
            )
        # 0.2.2: WAL recovery. If the .tmp companion exists with a
        # higher-seq, HMAC-valid payload, prefer it (the prior
        # process crashed between fsync and rename). Commit the
        # rename here so subsequent loads see the canonical file.
        wal = self._wal_path()
        main_scope = self._try_load_one(self._state_file)
        wal_scope = self._try_load_one(wal) if wal.exists() else None
        if wal_scope is not None and (
            main_scope is None or wal_scope.seq > main_scope.seq
        ):
            logger.warning(
                "ScopeStateStore: WAL recovery — committing "
                "state.json.tmp (seq=%d) over state.json (seq=%s). "
                "Prior process crashed between fsync and rename; "
                "external auditor's expected_next is preserved.",
                wal_scope.seq,
                main_scope.seq if main_scope else "absent",
            )
            try:
                os.replace(wal, self._state_file)
            except OSError as exc:
                logger.warning(
                    "ScopeStateStore: WAL commit-rename failed "
                    "(%s); keeping in-memory recovered state but "
                    "subsequent restart may regress.", exc,
                )
            return wal_scope
        # Clean up stale WAL (lower seq than main, or invalid).
        if wal.exists():
            try:
                wal.unlink()
            except OSError:
                pass
        if main_scope is not None:
            return main_scope
        return ActiveScope(
            scope_id=self._scope_id, ceiling=self._ceiling,
        )

    def _try_load_one(self, path: Path) -> Optional[ActiveScope]:
        """Return an ActiveScope if ``path`` parses, validates,
        AND verifies HMAC; None on any failure (with a logged
        warning so operators see the cause)."""
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "ScopeStateStore: read failed for %s (%s); starting "
                "with empty scope.", path, exc,
            )
            return None
        if not text.strip():
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "ScopeStateStore: state file %s is corrupt (%s); "
                "starting with empty scope. Re-call "
                "restrict_paths to re-narrow.",
                path, exc,
            )
            return None
        # Schema sanity checks.
        if data.get("version") != STATE_VERSION:
            logger.warning(
                "ScopeStateStore: state file %s version mismatch "
                "(got %r, expected %d); starting with empty scope.",
                path, data.get("version"), STATE_VERSION,
            )
            return None
        # Instance-identity sanity checks (defends against accidentally
        # reusing a state file across instances with different shapes).
        if data.get("scope_id") != self._scope_id:
            logger.warning(
                "ScopeStateStore: state file %s was written by an "
                "instance with scope_id=%r but this instance has "
                "scope_id=%r; starting with empty scope.",
                path, data.get("scope_id"), self._scope_id,
            )
            return None
        try:
            stored_ceiling = Path(data["ceiling"]).resolve(strict=False)
        except Exception:
            stored_ceiling = None
        if stored_ceiling != self._ceiling:
            logger.warning(
                "ScopeStateStore: state file %s was written under "
                "ceiling=%r but this instance has ceiling=%r; "
                "starting with empty scope.",
                path, str(stored_ceiling), str(self._ceiling),
            )
            return None
        # Parse paths + timestamp + seq.
        paths = [Path(p).resolve(strict=False) for p in data.get("active_paths", [])]
        ts_raw = data.get("last_restrict_at")
        last_restrict_at: Optional[datetime] = None
        if ts_raw:
            try:
                last_restrict_at = datetime.fromisoformat(ts_raw)
            except ValueError:
                last_restrict_at = None
        # 0.2.2 [security-engineer D.3]: seq is MANDATORY in v3. Pre-fix
        # data.get("seq", 0) defaulted to 0 when missing — an attacker
        # hand-editing the state file to omit the field (or set it to
        # null) silently degraded replay-defense to seq=0. v3 rejects
        # missing seq instead; the caller falls back to empty scope.
        if "seq" not in data:
            logger.warning(
                "ScopeStateStore: state file %s missing required "
                "'seq' field; rejecting (replay-defense compromised). "
                "Starting with empty scope.", path,
            )
            return None
        seq_raw = data["seq"]
        try:
            seq = int(seq_raw)
            if seq < 0:
                raise ValueError(f"negative seq {seq}")
        except (TypeError, ValueError) as exc:
            logger.warning(
                "ScopeStateStore: state file %s has malformed seq=%r "
                "(%s); rejecting. Starting with empty scope.",
                path, seq_raw, exc,
            )
            return None
        # 0.2.2 — verify HMAC over the canonical (version, scope_id,
        # ceiling, seq, sorted_paths) payload. Mismatch (forged file
        # OR cross-process secret rotation OR file written by an
        # earlier process whose random fallback secret is gone) →
        # reject.
        claimed_hmac = data.get("hmac")
        if not isinstance(claimed_hmac, str) or not claimed_hmac:
            logger.warning(
                "ScopeStateStore: state file %s missing 'hmac' field; "
                "rejecting (signing required since v3 / 0.2.2). "
                "Starting with empty scope.", path,
            )
            return None
        sorted_path_strs = sorted(str(p) for p in paths)
        expected_hmac = compute_state_hmac(
            version=STATE_VERSION,
            scope_id=self._scope_id,
            ceiling=str(self._ceiling),
            seq=seq,
            sorted_paths=sorted_path_strs,
        )
        if not hmac.compare_digest(claimed_hmac, expected_hmac):
            logger.warning(
                "ScopeStateStore: state file %s HMAC verification "
                "FAILED. Either the file was tampered, the operator "
                "rotated HARES_STATE_HMAC_SECRET between writes, OR "
                "this is a previous process's file and the random "
                "fallback secret is gone. Starting with empty scope; "
                "agents must re-call restrict_paths.", path,
            )
            return None
        logger.info(
            "ScopeStateStore: restored active scope from %s "
            "(scope_id=%r, %d paths, seq=%d)",
            path, self._scope_id, len(paths), seq,
        )
        return ActiveScope(
            paths=paths,
            last_restrict_at=last_restrict_at,
            scope_id=self._scope_id,
            ceiling=self._ceiling,
            seq=seq,
        )

    def _save(self, scope: ActiveScope) -> None:
        """Atomic write via tmp + rename, with fsync on the tmp.

        0.2.2: writes include the HMAC over the canonical payload so
        readers (including external auditors with the same secret)
        can detect tampering. The .tmp file doubles as a WAL — on
        crash between fsync and rename, _load_or_default's WAL
        recovery prefers the higher-seq .tmp on next startup,
        avoiding a false-positive replay alert at the auditor.
        """
        assert self._state_file is not None
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._wal_path()
        sorted_path_strs = sorted(str(p) for p in scope.paths)
        sig = compute_state_hmac(
            version=STATE_VERSION,
            scope_id=self._scope_id,
            ceiling=str(self._ceiling),
            seq=scope.seq,
            sorted_paths=sorted_path_strs,
        )
        payload = {
            "version": STATE_VERSION,
            "scope_id": self._scope_id,
            "ceiling": str(self._ceiling),
            "active_paths": sorted_path_strs,
            "seq": scope.seq,
            "hmac": sig,
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
        # Round-4 [HIGH security-engineer M.1]: fsync the parent
        # directory after the rename so the directory-entry update
        # itself is durable. On ext4(data=ordered) and XFS, the
        # rename's directory-entry update can otherwise reach disk
        # AFTER the inode data; power loss in that window can leave
        # the parent dir pointing at the OLD inode while both files
        # exist as durable, rolling seq back below what the auditor
        # last observed → false-positive REPLAY alert (or, worse,
        # silent scope widening). POSIX-compliant + cheap. No-op
        # when fsync isn't supported (Windows, exotic FSes).
        try:
            parent_fd = os.open(
                str(self._state_file.parent),
                os.O_RDONLY | os.O_DIRECTORY,
            )
        except OSError:
            return  # platform doesn't support O_DIRECTORY
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
        finally:
            os.close(parent_fd)
