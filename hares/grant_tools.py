"""Shared ``request_path_access`` tool used by the fs, shell, and
combined MCP servers.

A human-in-the-loop-gated runtime escape hatch: an agent can ask for
read or write access to a path OUTSIDE the instance's current sandbox
scope (the ceiling, for fs/combined; the bwrap mount namespace, for
shell/combined). This is the runtime-WIDENING counterpart to the
startup-time ``HARES_SANDBOX_RW`` / ``HARES_SANDBOX_RO`` env vars and
to ``restrict_paths`` (which only narrows WITHIN the ceiling — it can
never widen past it). A grant CAN widen past the ceiling; that's the
whole point of this tool. A grant can NEVER, however, override the
deny tier (``HARES_SANDBOX_EXCLUDE`` / ``HARES_SANDBOX_PROTECT``,
the system-dir blocklist, or ``.git``) — see
:func:`hares.path_safety.validate_grant_target`, which is the single
function both this module and the enforcement points in
:mod:`hares.fs.operations` / :class:`hares.runner.Runner` call to
guarantee that.

Modeled on :mod:`hares.fs.tools` (restrict tools) and
:mod:`hares.exec_tools` (execute_command): this module exposes one
tool *descriptor* plus a bound async *handler*, keyed by the
``<scope_id>_`` prefix convention, so the three servers register it
via the same shared machinery rather than three separate
implementations.

No CLI flag gates this tool (see the feature's design notes) — it is
registered unconditionally in shell/fs/combined modes because it is
intrinsically human-gated (fails closed for any non-interactive
client) and therefore needs no separate operator off-switch. The one
mode-dependent behavior is ``--read-only``: a ``mode="rw"`` request is
rejected outright (no elicitation shown) when the server is read-only,
mirroring how ``--read-only`` unregisters the fs write tools.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

from mcp.server import Server
from mcp.types import Tool

from .grants import GrantStore
from .path_safety import (
    DenyLists,
    PathSafetyError,
    validate_grant_target,
    validate_path_no_traversal,
)
from .policy import elicit_path_access_approval

logger = logging.getLogger(__name__)


REQUEST_PATH_ACCESS_TOOL = "request_path_access"


def _prefixed(name: str, scope_id: Optional[str]) -> str:
    return f"{scope_id}_{name}" if scope_id else name


def request_path_access_tool_descriptor(scope_id: Optional[str]) -> Tool:
    """The MCP Tool descriptor, with optional scope prefix."""
    return Tool(
        name=_prefixed(REQUEST_PATH_ACCESS_TOOL, scope_id),
        description=(
            "Request read or write access to a path OUTSIDE the current "
            "sandbox scope (ceiling / active scope) — the runtime-"
            "widening counterpart to restrict_paths (which only narrows "
            "WITHIN the ceiling; it can never grant access past it). "
            "Fires a BLOCKING human-in-the-loop approval dialog showing "
            "the resolved path, the mode, and an explicit warning that "
            "this is outside the sandbox; there is no way to bypass it "
            "programmatically. The human also picks how long the grant "
            "lasts: 'once' (the very next covered read/write or "
            "execute_command call only) or for the rest of the session. "
            "A grant can NEVER open a path that is excluded, protected, "
            "under a system directory (when enabled), or under a .git "
            "directory — those are rejected before the dialog is even "
            "shown, regardless of what the human would have approved. "
            "Declining, cancelling, or a client with no elicitation "
            "support all fail closed (denied). Use this ONLY when a "
            "task genuinely requires touching a path outside the "
            "current scope — most work should stay within the ceiling "
            "and use restrict_paths instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path to request access to. Absolute, or "
                        "resolved relative to the instance's ceiling "
                        "(falls back to the server's cwd when no "
                        "ceiling is configured). Symlinks are chased "
                        "BEFORE the approval dialog is shown, so the "
                        "human sees the real target, not the link."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["ro", "rw"],
                    "description": (
                        "'ro' for read-only access, 'rw' for read-write. "
                        "In --read-only server mode, only 'ro' requests "
                        "can ever be granted — 'rw' is rejected "
                        "immediately, no dialog shown."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Free text explaining why this access is "
                        "needed. Shown to the human approving the "
                        "request, clearly labeled as agent-authored — "
                        "be specific and honest."
                    ),
                },
            },
            "required": ["path", "mode", "reason"],
        },
    )


def _resolve_candidate(path_str: str, base: Path) -> Path:
    """Resolve ``path_str`` to an absolute, symlink-chased Path
    relative to ``base`` (the ceiling, or cwd when there is none).
    Does not assert containment — request_path_access is explicitly
    for paths that may lie outside any ceiling."""
    validate_path_no_traversal(path_str)
    raw = Path(path_str)
    candidate = raw if raw.is_absolute() else base / raw
    return candidate.resolve(strict=False)


def build_request_path_access_handlers(
    *,
    server: Server,
    scope_id: Optional[str],
    ceiling: Optional[Path],
    deny: Optional[DenyLists],
    grant_store: GrantStore,
    read_only: bool,
) -> dict[str, Callable[[dict], Awaitable[dict]]]:
    """Construct the bound async handler for ``request_path_access``,
    keyed by its (optionally prefixed) tool name — same return
    convention as :func:`hares.fs.tools.build_restrict_tool_handlers`
    and :func:`hares.exec_tools.build_exec_tool_handlers`.

    Args:
      server: The MCP Server instance — elicitation reads the active
        request context off it (see :func:`hares.policy._resolve_session`).
      scope_id: Optional tool-name prefix.
      ceiling: The instance's ceiling, or None for bare shell mode
        with no ceiling configured. Used only to detect the (unusual)
        case where the requested path already lies inside the
        ceiling, so the in-ceiling deny lists (exclude/protect) are
        also enforced via :func:`hares.path_safety.validate_grant_target`.
        NOTE: this is the ceiling resolved at server-build time. When
        ``--use-roots`` later refines the Runner's ceiling from the MCP
        client's declared roots, that refinement updates only the
        shell/bwrap ceiling — this handler (like the fs-op dispatch)
        keeps validating against the build-time ceiling/deny. The `.git`
        and system-dir checks in ``validate_grant_target`` are ceiling-
        independent and always apply; only the in-ceiling exclude/protect
        overlay can go stale under roots refinement. Pass an explicit
        ``--ceiling`` / ``$HARES_FS_CEILING`` if you rely on those under
        roots refinement.
      deny: Pre-resolved exclude/protect lists against ``ceiling``
        (see :class:`hares.path_safety.DenyLists`). Treated as empty
        when None.
      grant_store: The (possibly shared, e.g. in combined mode)
        GrantStore approved grants are recorded into.
      read_only: Whether the server is running with ``--read-only``.
        When True, ``mode="rw"`` requests are rejected outright — no
        elicitation shown — mirroring how ``--read-only`` unregisters
        the fs write tools.
    """
    name = _prefixed(REQUEST_PATH_ACCESS_TOOL, scope_id)

    async def _request(args: dict) -> dict:
        path_str = args.get("path")
        mode = args.get("mode")
        reason = args.get("reason")
        if not isinstance(path_str, str) or not path_str:
            raise ValueError(
                f"{REQUEST_PATH_ACCESS_TOOL}: 'path' must be a non-empty string"
            )
        if mode not in ("ro", "rw"):
            raise ValueError(
                f"{REQUEST_PATH_ACCESS_TOOL}: 'mode' must be 'ro' or 'rw' "
                f"(got {mode!r})"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                f"{REQUEST_PATH_ACCESS_TOOL}: 'reason' must be a non-empty "
                f"string"
            )

        if read_only and mode == "rw":
            logger.info(
                "request_path_access: rw request rejected — server is "
                "--read-only (path=%r)", path_str,
            )
            return {
                "granted": False,
                "path": path_str,
                "mode": mode,
                "reason_denied": (
                    "This server is running in --read-only mode; only "
                    "mode='ro' requests can be granted. Request "
                    "mode='ro' instead."
                ),
            }

        base = ceiling if ceiling is not None else Path(os.getcwd())
        try:
            resolved = _resolve_candidate(path_str, base)
            validate_grant_target(
                resolved, mode=mode, ceiling=ceiling, deny=deny,
            )
        except PathSafetyError as exc:
            # Deny beats grant, always — reject BEFORE even showing
            # the human a dialog they could not have approved anyway.
            logger.info(
                "request_path_access: rejected before elicitation — "
                "path=%r mode=%r reason=%s", path_str, mode, exc,
            )
            return {
                "granted": False,
                "path": path_str,
                "mode": mode,
                "reason_denied": str(exc),
            }

        lifetime = await elicit_path_access_approval(
            server, resolved, mode, reason, read_only_mode=read_only,
        )
        if lifetime is None:
            return {
                "granted": False,
                "path": str(resolved),
                "mode": mode,
                "reason_denied": (
                    "Declined by user, cancelled, or client does not "
                    "support elicitation."
                ),
            }
        grant_store.add(resolved, mode, lifetime)
        return {
            "granted": True,
            "path": str(resolved),
            "mode": mode,
            "lifetime": lifetime,
        }

    return {name: _request}
