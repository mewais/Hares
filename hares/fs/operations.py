"""Filesystem operations exposed by the Hares fs MCP server.

Each operation is an async function that:

1. Validates the input path string (no ``..`` traversal).
2. Resolves the path under the instance's ceiling (rejects escape).
3. For WRITE ops: also validates the resolved target is under at
   least one active-scope path (the architect-decided narrowing).
4. For READ ops: bounded by ceiling alone (active scope is a WRITE
   narrowing — read access remains broad within ceiling).
5. Performs the requested file operation.
6. Returns a JSON-serializable dict result.

The fs operations cover the same surface as
``@modelcontextprotocol/server-filesystem`` so this MCP is a
drop-in replacement at the tool-call layer (modulo the optional
prefix when ``--scope-id`` is set).
"""

from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path
from typing import Any, Optional

from ..grants import GrantStore
from ..path_safety import (
    DenyLists,
    PathSafetyError,
    _is_subpath,
    get_exclude_list,
    is_denied,
    resolve_under_ceiling,
    validate_grant_target,
    validate_path_no_traversal,
    validate_path_not_protected,
)
from .state import ActiveScope

logger = logging.getLogger(__name__)


# ── In-ceiling blacklist plumbing ──────────────────────────────────────
#
# Every operation takes an optional pre-resolved ``deny`` (see
# :class:`hares.path_safety.DenyLists`). The servers resolve it ONCE
# when the ceiling is finalized (server build time) and pass it down
# explicitly, so the per-operation hot path never re-reads
# HARES_SANDBOX_EXCLUDE / HARES_SANDBOX_PROTECT from the environment.
# When ``deny`` is None (direct callers, tests), each helper falls back
# to the per-call env read — the previous behavior.
#
# Every operation ALSO takes an optional ``grants`` (a
# :class:`hares.grants.GrantStore`) — the runtime request_path_access
# escape hatch. It only ever comes into play for a path that resolves
# OUTSIDE the ceiling: normal in-ceiling resolution/validation is
# completely unaffected. See ``_resolve`` below.


def _resolve(
    path_str: str,
    ceiling: Path,
    deny: Optional[DenyLists],
    *,
    grants: Optional[GrantStore] = None,
    need_write: bool = False,
) -> Path:
    """``resolve_under_ceiling`` with the pre-resolved exclude list,
    PLUS a fallback to the runtime ``request_path_access`` GrantStore
    for paths that resolve OUTSIDE the ceiling.

    ``need_write`` selects which grant tier can authorize the path:
    write operations require a covering ``rw`` grant; read operations
    accept either ``ro`` or ``rw``. Deny (exclude/protect/system-dir/
    .git) is re-validated here via :func:`validate_grant_target` even
    though ``request_path_access`` already validated it at grant-
    creation time — defense in depth, per the feature's design.

    A covering "once" grant is consumed (see
    :meth:`hares.grants.GrantStore.consume_once`) the moment it
    authorizes this resolution — i.e. before the caller performs the
    actual read/write, not after the I/O succeeds. A "session" grant
    is left untouched.
    """
    validate_path_no_traversal(path_str)
    raw = Path(path_str)
    candidate = raw if raw.is_absolute() else ceiling / raw
    resolved = candidate.resolve(strict=False)

    if grants is None or _is_subpath(resolved, ceiling):
        # In-ceiling path (the normal case) OR no grant store wired up
        # for this instance — defer entirely to the ceiling-bound
        # resolver (unchanged behavior).
        return resolve_under_ceiling(
            path_str, ceiling,
            excludelist=deny.exclude if deny is not None else None,
        )

    # Outside the ceiling: only an active request_path_access grant
    # can authorize this.
    covered = (
        grants.covers_write(resolved) if need_write else grants.covers_read(resolved)
    )
    if not covered:
        raise PathSafetyError(
            f"Resolved path {str(resolved)!r} is not under ceiling "
            f"{str(ceiling)!r} and is not covered by any active "
            f"request_path_access grant ({'rw' if need_write else 'ro/rw'} "
            f"required). Refusing to operate outside the instance's "
            f"outer bound. Call request_path_access to ask a human to "
            f"widen access to this path."
        )
    validate_grant_target(
        resolved, mode="rw" if need_write else "ro", ceiling=ceiling, deny=deny,
    )
    grants.consume_once(resolved, need_write=need_write)
    return resolved


def _exclude_list(ceiling: Path, deny: Optional[DenyLists]) -> tuple[Path, ...]:
    """The effective HARES_SANDBOX_EXCLUDE list for listing filters."""
    return deny.exclude if deny is not None else get_exclude_list(ceiling)


def _is_hidden(path: Path, excluded: tuple[Path, ...]) -> bool:
    """True when ``path`` must be hidden from listings/search results
    because it is at-or-under a HARES_SANDBOX_EXCLUDE entry."""
    return is_denied(path.resolve(strict=False), excluded)


def _visible_children(directory: Path, excluded: tuple[Path, ...]) -> list[Path]:
    """Sorted immediate children of ``directory`` minus excluded ones —
    the shared filter for list_directory / list_directory_with_sizes /
    directory_tree."""
    return [
        child for child in sorted(directory.iterdir())
        if not _is_hidden(child, excluded)
    ]


class ScopeViolationError(ValueError):
    """Raised when a write target is under the ceiling but NOT under
    any active-scope path. Distinct from PathSafetyError so the JSON-
    RPC error code can differentiate "outside ceiling entirely" (a
    safety check) from "outside the architect-narrowed scope" (a
    policy decision)."""


def _ensure_under_active_scope(
    target: Path, scope: ActiveScope, ceiling: Path,
    deny: Optional[DenyLists] = None,
) -> None:
    """For write operations: assert target is under at least one of
    the active-scope paths. Reads bypass this check (bounded by
    ceiling alone). When the scope is empty (no restrict ever called),
    writes are bounded by CEILING alone — same as reads.

    Also enforces the write-only HARES_SANDBOX_PROTECT blacklist FIRST,
    so a protected path is rejected even when it falls within the
    active scope (deny beats allow). The exclude blacklist is already
    enforced upstream in resolve_under_ceiling for both reads and
    writes."""
    validate_path_not_protected(
        target, ceiling,
        protectlist=deny.protect if deny is not None else None,
    )
    if not _is_subpath(target, ceiling):
        # Target lies entirely outside the ceiling. The ONLY way
        # _resolve() would have returned such a target is that an
        # active request_path_access "rw" grant already authorized
        # it (see _resolve's grant fallback) — active-scope narrowing
        # is a ceiling-INTERNAL concept ("write authority within the
        # ceiling") and has nothing to say about paths outside it, so
        # it does not apply here. The protect check above still ran
        # (a no-op for genuinely outside-ceiling paths, since protect
        # entries are themselves always inside the ceiling).
        return
    if not scope.paths:
        return  # no narrowing in effect; ceiling alone bounds writes
    target_resolved = target.resolve(strict=False)
    for sp in scope.paths:
        sp_resolved = sp.resolve(strict=False)
        if target_resolved == sp_resolved:
            return
        if str(target_resolved).startswith(str(sp_resolved).rstrip("/") + "/"):
            return
    raise ScopeViolationError(
        f"Path {str(target)!r} is under ceiling but outside the active "
        f"scope. Active scope: {[str(p) for p in scope.paths]}. To "
        f"write here, call restrict_paths to widen the active scope."
    )


# ── Read operations ────────────────────────────────────────────────────


async def read_file(args: dict, *, ceiling: Path, scope: ActiveScope,
                    deny: Optional[DenyLists] = None,
                    grants: Optional[GrantStore] = None) -> dict:
    target = _resolve(args["path"], ceiling, deny, grants=grants)
    return {
        "path": str(target),
        "content": target.read_text(encoding="utf-8"),
    }


async def read_text_file(args: dict, *, ceiling: Path, scope: ActiveScope,
                         deny: Optional[DenyLists] = None,
                         grants: Optional[GrantStore] = None) -> dict:
    """Alias for ``read_file`` matching @modelcontextprotocol/server-
    filesystem's tool surface (which has both for legacy reasons)."""
    return await read_file(args, ceiling=ceiling, scope=scope, deny=deny, grants=grants)


async def read_multiple_files(args: dict, *, ceiling: Path, scope: ActiveScope,
                              deny: Optional[DenyLists] = None,
                              grants: Optional[GrantStore] = None) -> dict:
    paths = args.get("paths", [])
    out: dict[str, Any] = {}
    for p in paths:
        try:
            target = _resolve(p, ceiling, deny, grants=grants)
            out[p] = {"content": target.read_text(encoding="utf-8")}
        except (PathSafetyError, OSError) as exc:
            out[p] = {"error": str(exc)}
    return {"files": out}


async def list_directory(args: dict, *, ceiling: Path, scope: ActiveScope,
                         deny: Optional[DenyLists] = None,
                         grants: Optional[GrantStore] = None) -> dict:
    target = _resolve(args["path"], ceiling, deny, grants=grants)
    excluded = _exclude_list(ceiling, deny)
    entries = []
    for entry in _visible_children(target, excluded):
        kind = "directory" if entry.is_dir() else "file"
        entries.append({"name": entry.name, "type": kind})
    return {"path": str(target), "entries": entries}


async def list_directory_with_sizes(args: dict, *, ceiling: Path, scope: ActiveScope,
                                    deny: Optional[DenyLists] = None,
                                    grants: Optional[GrantStore] = None) -> dict:
    target = _resolve(args["path"], ceiling, deny, grants=grants)
    excluded = _exclude_list(ceiling, deny)
    entries = []
    for entry in _visible_children(target, excluded):
        kind = "directory" if entry.is_dir() else "file"
        size = entry.stat().st_size if entry.is_file() else None
        entries.append({"name": entry.name, "type": kind, "size": size})
    return {"path": str(target), "entries": entries}


async def directory_tree(args: dict, *, ceiling: Path, scope: ActiveScope,
                         deny: Optional[DenyLists] = None,
                         grants: Optional[GrantStore] = None) -> dict:
    target = _resolve(args["path"], ceiling, deny, grants=grants)
    excluded = _exclude_list(ceiling, deny)
    def _walk(p: Path) -> dict:
        node: dict[str, Any] = {"name": p.name, "type": "directory" if p.is_dir() else "file"}
        if p.is_dir():
            try:
                node["children"] = [
                    _walk(child) for child in _visible_children(p, excluded)
                ]
            except OSError:
                node["children"] = []
        return node
    return {"path": str(target), "tree": _walk(target)}


async def search_files(args: dict, *, ceiling: Path, scope: ActiveScope,
                       deny: Optional[DenyLists] = None,
                       grants: Optional[GrantStore] = None) -> dict:
    """Search for files matching ``pattern`` (glob) under ``path``.
    Mirrors @modelcontextprotocol/server-filesystem's search shape."""
    target = _resolve(args["path"], ceiling, deny, grants=grants)
    excluded = _exclude_list(ceiling, deny)
    pattern = args["pattern"]
    matches: list[str] = []
    for root, dirs, files in os.walk(target):
        # Prune excluded dirs in place so os.walk doesn't descend into
        # them (also keeps them out of the dirs match list below).
        dirs[:] = [d for d in dirs if not _is_hidden(Path(root, d), excluded)]
        for name in files + dirs:
            if _is_hidden(Path(root, name), excluded):
                continue
            if fnmatch.fnmatch(name, pattern):
                matches.append(os.path.join(root, name))
    return {"path": str(target), "pattern": pattern, "matches": matches}


async def get_file_info(args: dict, *, ceiling: Path, scope: ActiveScope,
                        deny: Optional[DenyLists] = None,
                        grants: Optional[GrantStore] = None) -> dict:
    target = _resolve(args["path"], ceiling, deny, grants=grants)
    st = target.stat()
    return {
        "path": str(target),
        "size": st.st_size,
        "mode": oct(st.st_mode),
        "type": "directory" if target.is_dir() else "file",
        "mtime": st.st_mtime,
        "ctime": st.st_ctime,
    }


# ── Write operations (suppressed by --read-only) ──────────────────────


async def write_file(args: dict, *, ceiling: Path, scope: ActiveScope,
                     deny: Optional[DenyLists] = None,
                     grants: Optional[GrantStore] = None) -> dict:
    target = _resolve(args["path"], ceiling, deny, grants=grants, need_write=True)
    _ensure_under_active_scope(target, scope, ceiling, deny)
    content = args["content"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes_written": len(content.encode("utf-8"))}


async def edit_file(args: dict, *, ceiling: Path, scope: ActiveScope,
                    deny: Optional[DenyLists] = None,
                    grants: Optional[GrantStore] = None) -> dict:
    """Apply a list of {oldText, newText} edits to a file. Each edit
    must match exactly once; ambiguity raises an error so the caller
    rewrites the input."""
    target = _resolve(args["path"], ceiling, deny, grants=grants, need_write=True)
    _ensure_under_active_scope(target, scope, ceiling, deny)
    edits = args.get("edits", [])
    text = target.read_text(encoding="utf-8")
    applied = 0
    for edit in edits:
        old = edit["oldText"]
        new = edit["newText"]
        count = text.count(old)
        if count == 0:
            raise ValueError(
                f"edit_file: oldText not found in {target!s}: {old[:80]!r}"
            )
        if count > 1:
            raise ValueError(
                f"edit_file: oldText matches {count} times in "
                f"{target!s}; provide a more specific snippet."
            )
        text = text.replace(old, new, 1)
        applied += 1
    target.write_text(text, encoding="utf-8")
    return {"path": str(target), "edits_applied": applied}


async def create_directory(args: dict, *, ceiling: Path, scope: ActiveScope,
                           deny: Optional[DenyLists] = None,
                           grants: Optional[GrantStore] = None) -> dict:
    target = _resolve(args["path"], ceiling, deny, grants=grants, need_write=True)
    _ensure_under_active_scope(target, scope, ceiling, deny)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": str(target)}


async def move_file(args: dict, *, ceiling: Path, scope: ActiveScope,
                    deny: Optional[DenyLists] = None,
                    grants: Optional[GrantStore] = None) -> dict:
    src = _resolve(args["source"], ceiling, deny, grants=grants, need_write=True)
    dst = _resolve(args["destination"], ceiling, deny, grants=grants, need_write=True)
    # Both endpoints must be in the active scope (mv counts as a write
    # on both sides — source is unlinked, destination is created).
    _ensure_under_active_scope(src, scope, ceiling, deny)
    _ensure_under_active_scope(dst, scope, ceiling, deny)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    return {"source": str(src), "destination": str(dst)}


# ── Public dispatch tables ────────────────────────────────────────────

# Mapping: tool base-name → (handler, input schema, description, kind).
# The server.py layer applies the per-instance prefix and the
# read-only suppression of write tools.

READ_OPS: dict[str, dict] = {
    "read_file": {
        "handler": read_file,
        "description": (
            "Read a single file's contents. The path must resolve under the "
            "instance's root directory (the ceiling); paths outside it are "
            "refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "read_text_file": {
        "handler": read_text_file,
        "description": (
            "Read a single file's contents as text — an alias of read_file "
            "(mirrors the npm filesystem-server tool surface)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "read_multiple_files": {
        "handler": read_multiple_files,
        "description": (
            "Read several files in one call, returning each path's contents "
            "keyed by path. A failure on one path is reported in that path's "
            "entry instead of aborting the batch. Each path must resolve under "
            "the ceiling."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["paths"],
        },
    },
    "list_directory": {
        "handler": list_directory,
        "description": (
            "List the immediate entries of a directory, each with its name "
            "and type (file or directory). Does not recurse — use "
            "directory_tree for a full subtree. The path must resolve under "
            "the instance's root directory (the ceiling); paths outside it "
            "are refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "list_directory_with_sizes": {
        "handler": list_directory_with_sizes,
        "description": (
            "Like list_directory, but each entry also includes its size in "
            "bytes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "directory_tree": {
        "handler": directory_tree,
        "description": (
            "Return a directory's contents recursively as a nested JSON "
            "tree. Use list_directory for a single level; reach for this only "
            "when the entire subtree is needed at once. The path must resolve "
            "under the ceiling."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "search_files": {
        "handler": search_files,
        "description": (
            "Recursively find files whose names match a glob pattern beneath "
            "the given directory. Matches file names, not file contents. The "
            "search is confined to the instance's root directory (the "
            "ceiling); a path outside it is refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pattern": {"type": "string"},
            },
            "required": ["path", "pattern"],
        },
    },
    "get_file_info": {
        "handler": get_file_info,
        "description": (
            "Return metadata for a single file or directory — type, size, "
            "and timestamps — without reading its contents. The path must "
            "resolve under the ceiling."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

WRITE_OPS: dict[str, dict] = {
    "write_file": {
        "handler": write_file,
        "description": (
            "Write content to a file under the active scope. Parent "
            "directories are created if missing. Target must be under "
            "the instance's ceiling AND under at least one active-scope "
            "path (when restrict has been called)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    "edit_file": {
        "handler": edit_file,
        "description": (
            "Apply a list of {oldText, newText} edits. Each oldText "
            "must match exactly once."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oldText": {"type": "string"},
                            "newText": {"type": "string"},
                        },
                        "required": ["oldText", "newText"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
    },
    "create_directory": {
        "handler": create_directory,
        "description": (
            "Create a directory and any missing parents (idempotent). The "
            "target must be under the active scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "move_file": {
        "handler": move_file,
        "description": (
            "Move/rename a file. Both source AND destination must be "
            "under the active scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "destination": {"type": "string"},
            },
            "required": ["source", "destination"],
        },
    },
}
