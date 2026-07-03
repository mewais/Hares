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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Union


class PathSafetyError(ValueError):
    """Raised when a path argument fails one of the safety checks.

    Carries a human-readable message naming WHICH check failed and
    WHAT the offending input was, so operator-facing log output
    (and JSON-RPC error responses) explain the rejection without
    needing to inspect Hares source.
    """


class PathDeniedError(PathSafetyError):
    """Raised when a path is at-or-under an in-ceiling blacklist entry
    (``HARES_SANDBOX_EXCLUDE`` or ``HARES_SANDBOX_PROTECT``).

    Kept distinct from the base :class:`PathSafetyError` so callers can
    tell "blacklisted inside the ceiling" apart from "outside the
    ceiling entirely" or "under a system dir" — mirrors how
    ``ScopeViolationError`` is kept distinct in :mod:`hares.fs.operations`.
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


def _is_subpath(child: Union[Path, str], parent: Union[Path, str]) -> bool:
    """True if ``child`` equals ``parent`` or lives strictly under it,
    AFTER both are resolved to absolute paths.

    This is the ONE containment primitive for all of Hares (fs path
    validation, bwrap cwd checks, CLI/doctor diagnostics, cluster
    submission bounds). Semantics — chosen as the safest superset of
    the two historical implementations (this one and a former
    ``os.path.realpath``-based copy in :mod:`hares.sandbox`):

    * BOTH sides are symlink-resolved (``Path.resolve(strict=False)``,
      equivalent to ``os.path.realpath``) before comparing, so a
      symlink pointing outside ``parent`` does not count as contained,
      and a symlinked ``parent`` still matches its real children.
      ``strict=False``: nonexistent paths resolve lexically instead of
      failing, so bounds can be validated before paths exist on disk.
    * The prefix check is separator-aware — ``parent=/work/proj`` must
      NOT match ``/work/projsomething``, so ``/`` is appended before
      the ``startswith``.
    * Fail-closed: if resolution raises (``OSError``, or ``RuntimeError``
      on pathological symlink loops), the answer is False — callers
      treat "can't tell" as "not contained".
    """
    try:
        c = Path(child).resolve(strict=False)
        p = Path(parent).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if c == p:
        return True
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


# ── In-ceiling blacklist (exclude / protect) ───────────────────────────

# Two env-driven lists of paths INSIDE the ceiling, mirroring the
# HARES_SANDBOX_RW / HARES_SANDBOX_RO whitelist for paths OUTSIDE it:
#
#   HARES_SANDBOX_EXCLUDE  hide entirely — no read, no write. The path
#                          effectively does not exist for the agent.
#   HARES_SANDBOX_PROTECT  read-only-protect — readable, but writes are
#                          rejected even when the active scope would
#                          otherwise allow them.
#
# Both are colon-separated and accept absolute or ceiling-relative
# entries (resolved against the ceiling, the same way
# resolve_under_ceiling resolves a relative tool-call path). Enforcement
# lives in two mirrored surfaces: these validators (fs mode) and the
# bwrap mount composition in hares.sandbox (shell mode).

_EXCLUDE_ENV = "HARES_SANDBOX_EXCLUDE"
_PROTECT_ENV = "HARES_SANDBOX_PROTECT"


def _load_denylist(env_var: str, ceiling: Path) -> tuple[Path, ...]:
    """Parse a colon-separated env var into resolved paths under
    ``ceiling``. Each entry is expanded (``~``, ``$VAR``); relative
    entries are resolved against the ceiling. Empty entries dropped."""
    raw = os.environ.get(env_var, "")
    if not raw:
        return ()
    out: list[Path] = []
    for piece in raw.split(":"):
        if not piece:
            continue
        expanded = os.path.expanduser(os.path.expandvars(piece))
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = ceiling / candidate
        out.append(candidate.resolve(strict=False))
    return tuple(out)


def get_exclude_list(ceiling: Path) -> tuple[Path, ...]:
    """Resolved ``HARES_SANDBOX_EXCLUDE`` entries under ``ceiling``."""
    return _load_denylist(_EXCLUDE_ENV, ceiling)


def get_protect_list(ceiling: Path) -> tuple[Path, ...]:
    """Resolved ``HARES_SANDBOX_PROTECT`` entries under ``ceiling``."""
    return _load_denylist(_PROTECT_ENV, ceiling)


@dataclass(frozen=True)
class DenyLists:
    """Both in-ceiling blacklists, resolved against ONE specific ceiling.

    Servers build this once at server-construction time (the moment
    the fs ceiling is finalized) via :func:`resolve_deny_lists` and
    pass it down to the fs operations explicitly, so the per-operation
    hot path does not re-read ``HARES_SANDBOX_EXCLUDE`` /
    ``HARES_SANDBOX_PROTECT`` from the environment on every call.

    The resolution is keyed on the ceiling — a caller whose ceiling
    changes must resolve a fresh instance. (The shell Runner keeps its
    own call-time resolution in ``Runner._resolve_blacklist`` for
    exactly that reason: its ceiling can be refined from MCP roots
    after startup.)
    """

    exclude: tuple[Path, ...] = ()
    protect: tuple[Path, ...] = ()


def resolve_deny_lists(ceiling: Path) -> DenyLists:
    """Resolve both env-driven blacklists against ``ceiling`` (see
    :class:`DenyLists`)."""
    return DenyLists(
        exclude=get_exclude_list(ceiling),
        protect=get_protect_list(ceiling),
    )


def is_denied(path: Path, denylist: Iterable[Path]) -> bool:
    """True if ``path`` is at-or-under any entry in ``denylist``.

    Uses the separator-aware :func:`_is_subpath` so a denied
    ``/proj/secrets`` does NOT match a sibling ``/proj/secretsXYZ``.
    """
    return any(_is_subpath(path, denied) for denied in denylist)


def validate_path_not_excluded(
    resolved: Path,
    ceiling: Path,
    *,
    excludelist: Iterable[Path] | None = None,
) -> None:
    """Reject ``resolved`` if it is at-or-under any exclude entry.

    Applies to BOTH reads and writes — an excluded path is hidden
    entirely. ``excludelist`` overrides the env-loaded list (tests).
    """
    candidates = excludelist if excludelist is not None else get_exclude_list(ceiling)
    for denied in candidates:
        if _is_subpath(resolved, denied):
            raise PathDeniedError(
                f"Path {str(resolved)!r} is at-or-under excluded path "
                f"{str(denied)!r} ({_EXCLUDE_ENV}); it is hidden from the "
                f"agent (no read, no write). Remove the entry from "
                f"{_EXCLUDE_ENV} to allow access."
            )


def validate_path_not_protected(
    resolved: Path,
    ceiling: Path,
    *,
    protectlist: Iterable[Path] | None = None,
) -> None:
    """Reject ``resolved`` if it is at-or-under any protect entry.

    WRITE-ONLY — protected paths stay readable; only mutation is
    blocked, even when the active scope would otherwise allow it.
    ``protectlist`` overrides the env-loaded list (tests).
    """
    candidates = protectlist if protectlist is not None else get_protect_list(ceiling)
    for denied in candidates:
        if _is_subpath(resolved, denied):
            raise PathDeniedError(
                f"Path {str(resolved)!r} is at-or-under protected path "
                f"{str(denied)!r} ({_PROTECT_ENV}); it is read-only and "
                f"cannot be written, even within the active scope. Remove "
                f"the entry from {_PROTECT_ENV} to allow writes."
            )


def _contains_forbidden_segment(
    resolved: Path,
    segments: Iterable[str] = ALWAYS_FORBIDDEN_CEILING_SEGMENTS,
) -> bool:
    """True if ``resolved``'s parts contain any of ``segments`` as a
    contiguous subsequence at any depth — e.g. a ``.git`` path
    component anywhere in the path, not just as the final component.
    This catches both ``ceiling=.git`` (relative, resolves to a
    trailing segment) and ``ceiling=/work/proj/.git/objects``
    (absolute, ``.git`` mid-path) via the same check.

    Shared by :func:`validate_ceiling` (a ceiling can never cover
    ``.git/``) and :func:`validate_grant_target` (a runtime
    ``request_path_access`` grant can never open ``.git/`` either,
    regardless of whether the grant target lies inside or outside
    any ceiling).
    """
    parts = resolved.parts
    for forbidden_segment in segments:
        seg_parts = Path(forbidden_segment).parts
        for i in range(len(parts) - len(seg_parts) + 1):
            if parts[i:i + len(seg_parts)] == seg_parts:
                return True
    return False


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
    if _contains_forbidden_segment(resolved):
        raise PathSafetyError(
            f"Ceiling {str(ceiling)!r} (resolved to "
            f"{str(resolved)!r}) covers an always-forbidden segment "
            f"(e.g. '.git'); this is never legitimate as an "
            f"LLM-controlled scope. Pick a different ceiling."
        )
    validate_path_no_system_dir(resolved)


def validate_grant_target(
    resolved: Path,
    *,
    mode: str = "rw",
    ceiling: Optional[Path] = None,
    deny: Optional["DenyLists"] = None,
) -> None:
    """Validate that ``resolved`` may become (or continue to be used
    as) the root of a runtime ``request_path_access`` grant.

    This is the ONE deny check shared by grant-approval time (before
    a grant is recorded — see
    :func:`hares.policy.elicit_path_access_approval` and the
    ``request_path_access`` handler in :mod:`hares.grant_tools`) and
    use time (defense in depth — see the grant fallback in
    :mod:`hares.fs.operations` and the bwrap mount composition in
    :class:`hares.runner.Runner`). Deny always beats a grant; this
    function is what makes that true in exactly one place.

    Checks, applied regardless of whether ``resolved`` lies inside or
    outside any ceiling:

    * ``.git`` segment anywhere in the path (mirrors
      :func:`validate_ceiling` — never legitimate, no opt-out).
    * System-dir blocklist (opt-in via ``HARES_DISALLOW_SYSTEM_DIRS``).

    When ``ceiling`` is given AND ``resolved`` happens to fall inside
    it (an edge case — grants are meant to widen access OUTSIDE the
    ceiling, but nothing stops an agent from requesting a path that's
    already inside it), the in-ceiling blacklist is ALSO enforced:

    * ``HARES_SANDBOX_EXCLUDE`` — always (hides for both read+write).
    * ``HARES_SANDBOX_PROTECT`` — only when ``mode == "rw"`` (protect
      is write-only; a read-mode grant target under a protected path
      is fine to read, same as any other protected path).

    Raises :class:`PathSafetyError` / :class:`PathDeniedError` on any
    violation.
    """
    if _contains_forbidden_segment(resolved):
        raise PathSafetyError(
            f"Path {str(resolved)!r} is at-or-under a '.git' directory; "
            f"this can never be granted via request_path_access, "
            f"regardless of user approval."
        )
    validate_path_no_system_dir(resolved)
    if ceiling is not None and _is_subpath(resolved, ceiling):
        validate_path_not_excluded(
            resolved, ceiling,
            excludelist=deny.exclude if deny is not None else None,
        )
        if mode == "rw":
            validate_path_not_protected(
                resolved, ceiling,
                protectlist=deny.protect if deny is not None else None,
            )


def resolve_path_arg(path_str: str, base: Path) -> Path:
    """Validate a caller-supplied path arg (reject literal '..'
    traversal on the string), join it under `base` if relative, and
    symlink-resolve it (strict=False, so not-yet-existing paths
    resolve lexically). The shared resolution prologue for every
    tool-call path in Hares."""
    validate_path_no_traversal(path_str)
    raw = Path(path_str)
    candidate = raw if raw.is_absolute() else base / raw
    return candidate.resolve(strict=False)


def _finish_under_ceiling(
    resolved: Path,
    ceiling: Path,
    *,
    excludelist: Iterable[Path] | None = None,
) -> Path:
    """Everything :func:`resolve_under_ceiling` does AFTER resolution:
    assert ``resolved`` is under ``ceiling``, apply the system-dir
    check, and reject in-ceiling excluded paths. Split out so callers
    that already resolved the path once (e.g.
    :mod:`hares.fs.operations`) don't have to resolve it again just to
    run these checks.

    Raises:
      PathSafetyError: resolved path escapes ceiling, OR (when strict
        mode is on) lands under a system dir, OR is at-or-under an
        excluded path.
    """
    if not _is_subpath(resolved, ceiling):
        raise PathSafetyError(
            f"Resolved path {str(resolved)!r} is not under ceiling "
            f"{str(ceiling)!r}. Refusing to operate outside the "
            f"instance's outer bound (this includes symlink targets "
            f"that would escape via realpath resolution)."
        )
    validate_path_no_system_dir(resolved)
    # In-ceiling blacklist: excluded paths are hidden for reads AND
    # writes. (Protect is write-only, so it's checked by the write
    # chokepoint in hares.fs.operations, not here.)
    validate_path_not_excluded(resolved, ceiling, excludelist=excludelist)
    return resolved


def resolve_under_ceiling(
    path_str: str,
    ceiling: Path,
    *,
    excludelist: Iterable[Path] | None = None,
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
      excludelist: Pre-resolved ``HARES_SANDBOX_EXCLUDE`` entries
        (see :class:`DenyLists`). When None, the list is loaded from
        the environment per call (back-compat default for direct
        callers).

    Returns:
      The resolved absolute path (under ceiling).

    Raises:
      PathSafetyError: traversal in input, OR resolved path escapes
        ceiling, OR (when strict mode is on) lands under a system dir.
    """
    resolved = resolve_path_arg(path_str, ceiling)
    return _finish_under_ceiling(resolved, ceiling, excludelist=excludelist)
