"""Hares CLI entry point — argument parsing + dispatch.

Single binary, two tool families. The ``--enable`` flag selects which
family/families an instance exposes:

* ``--enable=shell`` (default) — exposes ``execute_command`` (the
  existing 0.1 surface) plus the runtime restrict tools.
* ``--enable=fs`` — exposes the full filesystem-server tool surface
  (read + write under ``--ceiling``) plus the restrict tools.
* ``--enable=fs+shell`` — union of both, sharing one ``--scope-id``
  prefix and one active scope (``restrict_paths`` narrows both
  layers in one call).

All flags are SYMMETRIC across fs and shell — same name, same
semantics, different mechanism. Bare ``hares-mcp`` invocation
(no flags) preserves the 0.1 backward-compat behavior: shell-only,
unprefixed ``execute_command``, in-process semaphore (when
``HARES_COORDINATION_DIR`` is unset), bwrap REQUIRED by default
(0.2.0 change — set ``HARES_SANDBOX_DISABLED=1`` to opt out).

Flag validation is fail-fast at startup with human-readable errors,
so misconfiguration surfaces before any MCP traffic flows.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .path_safety import (
    PathSafetyError,
    validate_ceiling,
    validate_path_no_system_dir,
)


logger = logging.getLogger(__name__)


_SCOPE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hares-mcp",
        description=(
            "Hares — guard MCP server. Provides scoped, throttled, "
            "and (optionally) sandboxed access to filesystem operations "
            "and shell-command execution for LLM agents.\n\n"
            "See README for the full env-var reference, the system-dir "
            "policy, and cross-process coordination semantics."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Backward compat: bare ``hares-mcp`` (no flags) preserves "
            "the 0.1 shell-only behavior with bwrap required by default "
            "(set HARES_SANDBOX_DISABLED=1 to opt out for non-Linux / "
            "debugging)."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--enable",
        choices=["shell", "fs", "fs+shell"],
        default="shell",
        help=(
            "Which tool family/families to expose. Default: shell "
            "(0.1-compat). 'fs' exposes the filesystem-server surface; "
            "'fs+shell' exposes both with a shared scope_id and "
            "active scope."
        ),
    )
    parser.add_argument(
        "--scope-id",
        default=None,
        help=(
            "Optional tool-name prefix for per-instance disambiguation. "
            "Pattern: ^[a-z][a-z0-9_]*$. When set, ALL tool names "
            "exposed by this instance are prefixed (e.g. 'src_write_file'). "
            "Required for multi-instance use under flat-namespace tool "
            "registries; operator's responsibility to "
            "ensure uniqueness across instances."
        ),
    )
    parser.add_argument(
        "--ceiling",
        default=None,
        help=(
            "REQUIRED for --enable involving fs or shell. Outer bound "
            "for any path the instance can touch. For fs: bounds tool-"
            "call paths. For shell: bounds the bwrap mount namespace. "
            "Defaults to $HARES_FS_CEILING when not passed; if neither "
            "is set, startup fails. Validated to NOT cover .git/ "
            "regardless of HARES_DISALLOW_SYSTEM_DIRS."
        ),
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help=(
            "Observe-only mode. For fs: write tools NOT registered. "
            "For shell: bwrap mounts the active scope as RO; subprocess "
            "writes are kernel-rejected. Symmetric semantic, different "
            "mechanism."
        ),
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help=(
            "Optional path where the active scope is persisted across "
            "server restarts. Atomic write via tmp+rename; crash-"
            "recoverable (corrupt content falls back to empty scope). "
            "When unset, the active scope is in-memory only."
        ),
    )
    return parser.parse_args(argv)


def _resolve_ceiling(args: argparse.Namespace) -> Path:
    """Resolve --ceiling from CLI > $HARES_FS_CEILING > error."""
    raw = args.ceiling or os.environ.get("HARES_FS_CEILING", "").strip()
    if not raw:
        raise SystemExit(
            "hares-mcp: --ceiling is required (pass --ceiling=PATH or "
            "set HARES_FS_CEILING in env). The ceiling is the outer "
            "bound for any path this instance can touch."
        )
    expanded = Path(os.path.expanduser(os.path.expandvars(raw))).resolve(strict=False)
    try:
        validate_ceiling(expanded)
    except PathSafetyError as exc:
        raise SystemExit(f"hares-mcp: invalid --ceiling: {exc}")
    return expanded


def _validate_scope_id(scope_id: Optional[str]) -> Optional[str]:
    if scope_id is None:
        return None
    if not _SCOPE_ID_RE.match(scope_id):
        raise SystemExit(
            f"hares-mcp: --scope-id={scope_id!r} is invalid. Must match "
            f"^[a-z][a-z0-9_]*$ (lowercase letters, digits, underscores; "
            f"must start with a letter)."
        )
    return scope_id


def _validate_state_file(
    state_file_arg: Optional[str], scope_id: Optional[str], ceiling: Path,
) -> Optional[Path]:
    """Resolve --state-file. No env-var default; in-memory if unset."""
    if not state_file_arg:
        return None
    expanded = Path(
        os.path.expanduser(os.path.expandvars(state_file_arg))
    ).resolve(strict=False)
    # Defensive: state file shouldn't live under the ceiling — agents
    # could otherwise corrupt their own scope state via write_file.
    # We don't HARD-fail here (operator may legitimately want it under
    # ceiling for crash recovery), but we warn loudly.
    if str(expanded).startswith(str(ceiling).rstrip("/") + "/"):
        logger.warning(
            "--state-file (%s) lives under --ceiling (%s); an agent "
            "with write access to the ceiling could corrupt the active "
            "scope. Move the state file outside the ceiling for tighter "
            "isolation.",
            expanded, ceiling,
        )
    # Round-4 [security-adversarial #3]: --state-file + per-process
    # random HMAC fallback is a real attack window. An attacker who
    # SIGTERMs Hares gets a wide-open scope until the next
    # restrict_paths call — the new process can't verify the prior
    # signed state (different secret) so it falls back to empty
    # scope (seq=0, no restrictions). Operators running stateful
    # deploys MUST pin HARES_STATE_HMAC_SECRET so the new process
    # can verify the prior file. Fail-closed at startup beats
    # silently-degraded-replay-defense at runtime.
    if not (os.environ.get("HARES_STATE_HMAC_SECRET") or "").strip():
        raise SystemExit(
            "hares-mcp: --state-file (%s) is configured but "
            "HARES_STATE_HMAC_SECRET is unset. Stateful deployments "
            "require an operator-pinned HMAC secret so the state "
            "file can be verified across process restarts (the "
            "per-process random fallback can't verify a prior "
            "process's file → silent empty-scope on every restart, "
            "which an attacker can trigger with SIGTERM to widen "
            "the scope window). Set HARES_STATE_HMAC_SECRET to a "
            "high-entropy value (32+ bytes of base64/hex) and "
            "re-run, or drop --state-file for in-memory-only mode."
            % expanded
        )
    return expanded


def _validate_env_paths() -> None:
    """Validate HARES_SANDBOX_RW / HARES_SANDBOX_RO entries against
    the system-dir policy (when strict mode is on). Doesn't validate
    presence — bwrap silently skips missing mounts at runtime.
    """
    for var in ("HARES_SANDBOX_RW", "HARES_SANDBOX_RO"):
        raw = os.environ.get(var, "")
        if not raw:
            continue
        for piece in raw.split(":"):
            if not piece:
                continue
            expanded = Path(
                os.path.expanduser(os.path.expandvars(piece))
            ).resolve(strict=False)
            try:
                validate_path_no_system_dir(expanded)
            except PathSafetyError as exc:
                raise SystemExit(
                    f"hares-mcp: env var {var} contains a system-dir "
                    f"path that's blocked by HARES_DISALLOW_SYSTEM_DIRS: "
                    f"{exc}"
                )


def main(argv: Optional[list[str]] = None) -> None:
    """Console-script entry point (``hares-mcp``)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s hares: %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _parse_args(argv)

    scope_id = _validate_scope_id(args.scope_id)
    ceiling = _resolve_ceiling(args)
    state_file = _validate_state_file(args.state_file, scope_id, ceiling)
    _validate_env_paths()

    # Dispatch.
    if args.enable == "shell":
        from .shell.server import serve as shell_serve
        shell_serve(
            scope_id=scope_id,
            ceiling=ceiling,
            read_only=args.read_only,
            state_file=state_file,
        )
    elif args.enable == "fs":
        from .fs.server import serve as fs_serve
        fs_serve(
            scope_id=scope_id,
            ceiling=ceiling,
            read_only=args.read_only,
            state_file=state_file,
        )
    elif args.enable == "fs+shell":
        # The combined server reuses the fs server's scope_state +
        # registers shell's execute_command alongside the fs tools.
        # For the initial 0.2.0 release, run them as a single fs-side
        # server with the shell tool spliced in. The fs tools handler
        # set is the bigger surface; adding execute_command requires
        # importing the shell runner setup.
        from .combined.server import serve as combined_serve
        combined_serve(
            scope_id=scope_id,
            ceiling=ceiling,
            read_only=args.read_only,
            state_file=state_file,
        )
    else:
        raise SystemExit(f"hares-mcp: unknown --enable={args.enable!r}")


if __name__ == "__main__":
    main()
