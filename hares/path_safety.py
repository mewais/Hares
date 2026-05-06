"""Path safety primitives for Hares — system-dir validation, path
resolution, ceiling enforcement.

Three layers of policy live here:

1. **Always-on ceiling guard** — version-control state (``.git/``) is
   never legitimate as a ceiling, regardless of any opt-out env var.
   Operators who pass a ceiling that would cover it get a hard-fail
   at startup.

2. **System-dir blocklist (opt-in)** — when
   ``HARES_DISALLOW_SYSTEM_DIRS=1`` is set, any path argument
   (``--ceiling``, ``HARES_SANDBOX_RW``, ``HARES_SANDBOX_RO``, paths
   passed to the runtime ``restrict_paths`` tool) is checked
   against a default blocklist plus operator-extended additions
   (``HARES_EXTRA_SYSTEM_DIRS``). Default is permissive — operators
   opt in to the safety check, since legit deployments often need
   /opt or /var paths that a strict default would reject.

3. **Per-operation traversal / containment** — every path resolved
   for a tool call must be under its instance's ceiling and must not
   contain ``..`` components. Always on; the cheapest defense against
   accidental escape via ``write_file("../../etc/passwd", ...)``.

The module is dependency-free (only stdlib) so it can be imported
from anywhere in Hares without pulling in the MCP / asyncio stack.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


class PathSafetyError(ValueError):
    """Raised when a path argument fails one of the safety checks.

    Carries a human-readable message naming WHICH check failed and
    WHAT the offending input was, so operator-facing log output
    (and JSON-RPC error responses) explain the rejection without
    needing to inspect Hares source.
    """


# ── System-dir policy ──────────────────────────────────────────────────

# Default blocklist when HARES_DISALLOW_SYSTEM_DIRS=1. Conservative —
# the obvious "don't let an LLM agent point a writable scope here" set.
# Notably NOT included by default (policy-debatable, common legitimate
# uses): /opt (user-installed tools), /var (some subpaths sensitive but
# most isn't), /run (runtime sockets sometimes needed), /tmp (explicitly
# user-writable), /home (user data), /usr (parent — only /usr/bin and
# /usr/sbin are "system binaries"; /usr/local and /usr/share are fine).
DEFAULT_SYSTEM_DIRS: tuple[str, ...] = (
    "/etc",        # system config
    "/proc",       # kernel state
    "/sys",        # kernel state
    "/dev",        # device nodes
    "/bin",        # system binaries
    "/sbin",       # system binaries
    "/usr/bin",    # system binaries (note: /usr parent NOT in list)
    "/usr/sbin",   # system binaries
    "/boot",       # bootloader
    "/root",       # root home
    "/lib",        # system libraries
    "/lib32",      # system libraries
    "/lib64",      # system libraries
)


# Always-rejected segments inside any ceiling, regardless of opt-out.
# A write-enabled ceiling under .git/ would let an LLM agent rewrite
# version-control history; never legitimate.
ALWAYS_FORBIDDEN_CEILING_SEGMENTS: tuple[str, ...] = (
    ".git",
)


def system_dirs_disallowed() -> bool:
    """Read ``HARES_DISALLOW_SYSTEM_DIRS`` env. True iff the operator
    has opted in to strict system-dir validation."""
    raw = os.environ.get("HARES_DISALLOW_SYSTEM_DIRS", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def get_system_dir_blocklist() -> tuple[str, ...]:
    """Return the effective blocklist when strict mode is on:
    ``DEFAULT_SYSTEM_DIRS`` plus colon-separated entries from
    ``HARES_EXTRA_SYSTEM_DIRS``. Each path is resolved to absolute
    form (no symlink following at this stage — defer to the validator
    so the caller's path is checked AS RESOLVED)."""
    extra_raw = os.environ.get("HARES_EXTRA_SYSTEM_DIRS", "")
    extra = tuple(
        os.path.abspath(os.path.expanduser(os.path.expandvars(p)))
        for p in extra_raw.split(":")
        if p
    )
    return DEFAULT_SYSTEM_DIRS + extra


def _is_subpath(child: Path, parent: Path) -> bool:
    """True if ``child`` equals ``parent`` or lives strictly under it,
    AFTER both are resolved to absolute paths."""
    try:
        c = child.resolve(strict=False)
        p = parent.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if c == p:
        return True
    # Use os.sep-aware prefix check — a parent like /work/proj must NOT
    # match /work/projsomething, so add the separator before comparing.
    p_str = str(p).rstrip("/") + "/"
    c_str = str(c)
    return c_str.startswith(p_str)


def validate_path_no_traversal(path_str: str) -> None:
    """Reject path strings containing ``..`` as a path component.

    A literal ``..`` in any segment of the path — including unresolved
    forms like ``foo/../bar`` — is the cheapest way an LLM agent
    might try to escape a scope. Reject on string analysis BEFORE
    resolution so we never even attempt the realpath.

    Note: a filename that happens to contain two dots (e.g.
    ``..hidden`` or ``foo..bar``) is fine — only a bare ``..`` segment
    is rejected.
    """
    for segment in path_str.replace("\\", "/").split("/"):
        if segment == "..":
            raise PathSafetyError(
                f"Path {path_str!r} contains a '..' component; "
                f"refusing to resolve. Use absolute paths or paths "
                f"relative to the ceiling that do not require parent "
                f"traversal."
            )


def validate_path_no_system_dir(
    path: Path,
    *,
    blocklist: Iterable[str] | None = None,
) -> None:
    """If ``HARES_DISALLOW_SYSTEM_DIRS=1`` is set, reject ``path`` if it
    equals or is under any blocklist entry. Default blocklist is from
    :func:`get_system_dir_blocklist`; callers can override (mostly for
    tests). When strict mode is OFF, this is a no-op.
    """
    if not system_dirs_disallowed():
        return
    candidates = blocklist if blocklist is not None else get_system_dir_blocklist()
    for forbidden_str in candidates:
        forbidden = Path(forbidden_str)
        if _is_subpath(path, forbidden):
            raise PathSafetyError(
                f"Path {str(path)!r} is at-or-under system directory "
                f"{forbidden_str!r}; rejected because "
                f"HARES_DISALLOW_SYSTEM_DIRS=1 is set. To allow, "
                f"either remove the env var (permissive default) or "
                f"adjust HARES_EXTRA_SYSTEM_DIRS so this path is no "
                f"longer covered."
            )


def validate_ceiling(ceiling: Path) -> None:
    """Validate a ceiling path argument (passed to ``--ceiling`` or
    via ``HARES_FS_CEILING``).

    ALWAYS rejects ceilings that would cover ``.git/`` regardless of
    strict-mode opt-in — a write-enabled scope rooted in version-control
    state lets an agent rewrite history.

    ALSO applies system-dir validation when strict mode is on.
    """
    # Resolve once for both checks. strict=False so a ceiling that
    # doesn't yet exist (greenfield project root) doesn't fail
    # resolution — we only care about path shape, not file presence.
    resolved = ceiling.resolve(strict=False)
    for forbidden_segment in ALWAYS_FORBIDDEN_CEILING_SEGMENTS:
        # Match if the resolved ceiling literally contains the segment
        # OR is at-or-under a path ending in that segment. This catches
        # both `ceiling=.git` (relative) and
        # `ceiling=/work/proj/.git` (absolute) via the same check.
        parts = resolved.parts
        seg_parts = Path(forbidden_segment).parts
        # Look for seg_parts as a contiguous subsequence at any
        # depth in resolved.parts.
        for i in range(len(parts) - len(seg_parts) + 1):
            if parts[i:i + len(seg_parts)] == seg_parts:
                raise PathSafetyError(
                    f"Ceiling {str(ceiling)!r} (resolved to "
                    f"{str(resolved)!r}) covers always-forbidden "
                    f"segment {forbidden_segment!r}; this is never "
                    f"legitimate as an LLM-controlled scope. Pick a "
                    f"different ceiling."
                )
    validate_path_no_system_dir(resolved)


def resolve_under_ceiling(
    path_str: str,
    ceiling: Path,
) -> Path:
    """Resolve a tool-call path argument relative to ``ceiling``,
    follow symlinks, assert the result is under ceiling.

    The two-step (no-traversal first, then resolve-and-contain) is
    deliberate: a literal ``..`` segment is rejected on string analysis
    BEFORE realpath even runs (cheapest defense). After realpath, a
    symlink that points outside the ceiling is also rejected.

    Args:
      path_str: Caller-supplied path (relative or absolute).
      ceiling: The instance's outer bound.

    Returns:
      The resolved absolute path (under ceiling).

    Raises:
      PathSafetyError: traversal in input, OR resolved path escapes
        ceiling, OR (when strict mode is on) lands under a system dir.
    """
    validate_path_no_traversal(path_str)
    raw = Path(path_str)
    if raw.is_absolute():
        candidate = raw
    else:
        candidate = ceiling / raw
    resolved = candidate.resolve(strict=False)
    if not _is_subpath(resolved, ceiling):
        raise PathSafetyError(
            f"Resolved path {str(resolved)!r} is not under ceiling "
            f"{str(ceiling)!r}. Refusing to operate outside the "
            f"instance's outer bound (this includes symlink targets "
            f"that would escape via realpath resolution)."
        )
    validate_path_no_system_dir(resolved)
    return resolved
