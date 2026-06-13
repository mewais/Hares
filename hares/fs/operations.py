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
from typing import Any

from ..path_safety import (
    PathSafetyError,
    resolve_under_ceiling,
    validate_path_no_system_dir,
)
from .state import ActiveScope

logger = logging.getLogger(__name__)


class ScopeViolationError(ValueError):
    """Raised when a write target is under the ceiling but NOT under
    any active-scope path. Distinct from PathSafetyError so the JSON-
    RPC error code can differentiate "outside ceiling entirely" (a
    safety check) from "outside the architect-narrowed scope" (a
    policy decision)."""


def _ensure_under_active_scope(target: Path, scope: ActiveScope) -> None:
    """For write operations: assert target is under at least one of
    the active-scope paths. Reads bypass this check (bounded by
    ceiling alone). When the scope is empty (no restrict ever called),
    writes are bounded by CEILING alone — same as reads."""
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


async def read_file(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    target = resolve_under_ceiling(args["path"], ceiling)
    return {
        "path": str(target),
        "content": target.read_text(encoding="utf-8"),
    }


async def read_text_file(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    """Alias for ``read_file`` matching @modelcontextprotocol/server-
    filesystem's tool surface (which has both for legacy reasons)."""
    return await read_file(args, ceiling=ceiling, scope=scope)


async def read_multiple_files(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    paths = args.get("paths", [])
    out: dict[str, Any] = {}
    for p in paths:
        try:
            target = resolve_under_ceiling(p, ceiling)
            out[p] = {"content": target.read_text(encoding="utf-8")}
        except (PathSafetyError, OSError) as exc:
            out[p] = {"error": str(exc)}
    return {"files": out}


async def list_directory(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    target = resolve_under_ceiling(args["path"], ceiling)
    entries = []
    for entry in sorted(target.iterdir()):
        kind = "directory" if entry.is_dir() else "file"
        entries.append({"name": entry.name, "type": kind})
    return {"path": str(target), "entries": entries}


async def list_directory_with_sizes(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    target = resolve_under_ceiling(args["path"], ceiling)
    entries = []
    for entry in sorted(target.iterdir()):
        kind = "directory" if entry.is_dir() else "file"
        size = entry.stat().st_size if entry.is_file() else None
        entries.append({"name": entry.name, "type": kind, "size": size})
    return {"path": str(target), "entries": entries}


async def directory_tree(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    target = resolve_under_ceiling(args["path"], ceiling)
    def _walk(p: Path) -> dict:
        node: dict[str, Any] = {"name": p.name, "type": "directory" if p.is_dir() else "file"}
        if p.is_dir():
            try:
                node["children"] = [_walk(child) for child in sorted(p.iterdir())]
            except OSError:
                node["children"] = []
        return node
    return {"path": str(target), "tree": _walk(target)}


async def search_files(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    """Search for files matching ``pattern`` (glob) under ``path``.
    Mirrors @modelcontextprotocol/server-filesystem's search shape."""
    target = resolve_under_ceiling(args["path"], ceiling)
    pattern = args["pattern"]
    matches: list[str] = []
    for root, dirs, files in os.walk(target):
        for name in files + dirs:
            if fnmatch.fnmatch(name, pattern):
                matches.append(os.path.join(root, name))
    return {"path": str(target), "pattern": pattern, "matches": matches}


async def get_file_info(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    target = resolve_under_ceiling(args["path"], ceiling)
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


async def write_file(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    target = resolve_under_ceiling(args["path"], ceiling)
    _ensure_under_active_scope(target, scope)
    content = args["content"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes_written": len(content.encode("utf-8"))}


async def edit_file(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    """Apply a list of {oldText, newText} edits to a file. Each edit
    must match exactly once; ambiguity raises an error so the caller
    rewrites the input."""
    target = resolve_under_ceiling(args["path"], ceiling)
    _ensure_under_active_scope(target, scope)
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


async def create_directory(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    target = resolve_under_ceiling(args["path"], ceiling)
    _ensure_under_active_scope(target, scope)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": str(target)}


async def move_file(args: dict, *, ceiling: Path, scope: ActiveScope) -> dict:
    src = resolve_under_ceiling(args["source"], ceiling)
    dst = resolve_under_ceiling(args["destination"], ceiling)
    # Both endpoints must be in the active scope (mv counts as a write
    # on both sides — source is unlinked, destination is created).
    _ensure_under_active_scope(src, scope)
    _ensure_under_active_scope(dst, scope)
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
