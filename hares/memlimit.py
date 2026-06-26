"""Aggregate-memory limiting via cgroup v2 scopes (systemd-run --user --scope).

WHY this module exists
======================
Per-process ``RLIMIT_AS`` cannot bound the AGGREGATE memory of a
multi-process command tree (each child gets its own independent AS
budget). A ``make -j`` / ``pytest -n auto`` / build can exhaust host
RAM faster than Hares's 2-second RSS poll catches it, causing the
kernel's GLOBAL OOM killer to fire and potentially killing the MCP
client (Claude Code), not the offending command — ending the whole
session.

Fix: run every command tree inside a **cgroup v2 scope** via
``systemd-run --user --scope`` with ``memory.max`` set. The kernel OOM
killer is then SCOPED to that cgroup — it kills only the command's
process tree, never anything outside it. The parent Hares process and
the MCP client are completely unaffected. ``RLIMIT_AS`` is kept per-
process as defence-in-depth (catches single-process overruns before they
even start aggregating).

This module is deliberately import-safe: it only uses stdlib + optional
``shutil``/``subprocess``. No imports from ``hares.config`` or
``hares.runner`` to avoid circular imports.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from uuid import uuid4

# ── Module-level constants ────────────────────────────────────────────

SYSTEMD_RUN_BIN: str = os.environ.get("HARES_SYSTEMD_RUN_BIN", "systemd-run")
SCOPE_NAME_PREFIX: str = "hares"

# ── Cache for the availability probe ─────────────────────────────────

# None  = not yet probed
# True  = cgroup memory limiting is fully available
# False = unavailable (any check failed; fall back to RLIMIT)
_cgroup_available_cache: Optional[bool] = None


# ── Public API ────────────────────────────────────────────────────────

def cgroup_memory_available() -> bool:
    """Return ``True`` iff aggregate memory limiting via
    ``systemd-run --user --scope`` is usable on this host.

    Checks, in order (result is cached in a module global so the
    expensive functional probe runs only once per process):

    1. ``HARES_DISABLE_CGROUP`` is not truthy (``1``/``true``/``yes``/
       ``on``) — operator opt-out.
    2. ``/sys/fs/cgroup`` is a cgroup v2 fs (detected by the presence of
       ``/sys/fs/cgroup/cgroup.controllers``).
    3. ``memory`` appears in ``/sys/fs/cgroup/cgroup.controllers`` (or
       the user-slice controllers if the root is not directly writable).
    4. ``shutil.which(SYSTEMD_RUN_BIN)`` is not None.
    5. Functional probe: run
       ``systemd-run --user --scope -p MemoryMax=64M --quiet -- /bin/true``
       with a short timeout; returncode 0 ⇒ available.

    Any failure ⇒ ``False`` (fall back to RLIMIT). **Never raises.**
    """
    global _cgroup_available_cache
    if _cgroup_available_cache is not None:
        return _cgroup_available_cache

    try:
        result = _probe_cgroup_memory()
    except Exception:
        result = False

    _cgroup_available_cache = result
    return result


def reset_cache() -> None:
    """Clear the cached :func:`cgroup_memory_available` result.

    Intended for use in tests that need to force re-evaluation of the
    probe (e.g. after monkeypatching ``HARES_DISABLE_CGROUP``).
    """
    global _cgroup_available_cache
    _cgroup_available_cache = None


def machine_safe_max_mb(fraction: float = 0.9) -> int:
    """Return approximately *fraction* of the host's ``MemTotal`` in MB.

    Reads ``/proc/meminfo`` to find the installed RAM; multiplies by
    *fraction* (clamped to ``(0, 1]``) and rounds down to the nearest
    megabyte.  This gives a machine-safe upper bound for operator
    approval: a memory-hungry command is allowed up to 90 % of RAM by
    default, leaving headroom for the OS and other processes.

    Fallback is 4096 MB if ``/proc/meminfo`` is unreadable (non-Linux
    host, container without procfs, etc.).

    Args:
        fraction: Fraction of MemTotal to use. Clamped to ``(0, 1]``
            so callers cannot accidentally request 0 or a negative cap.
    """
    # Clamp fraction to (0, 1].
    fraction = max(1e-6, min(1.0, fraction))

    mem_total_kb: Optional[int] = None
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    # Format: "MemTotal:    24609996 kB"
                    parts = line.split()
                    if len(parts) >= 2:
                        mem_total_kb = int(parts[1])
                    break
    except Exception:
        pass

    if mem_total_kb is None or mem_total_kb <= 0:
        return 4096

    mem_total_mb = mem_total_kb // 1024
    return max(1, int(mem_total_mb * fraction))


def new_scope_name(prefix: str = SCOPE_NAME_PREFIX) -> str:
    """Generate a unique cgroup scope name for one command invocation.

    Returns ``f'{prefix}-{uuid4().hex[:12]}'``, e.g.
    ``"hares-3a7f2d91bc4e"``. The 12 hex chars give ~47 bits of
    entropy — effectively zero collision probability per host lifetime.
    The ``--unit`` name passed to ``systemd-run`` must be a valid
    systemd unit name; this format (alphanumeric + hyphens, no dots,
    no slashes) satisfies that constraint.
    """
    return f"{prefix}-{uuid4().hex[:12]}"


def build_scope_argv(
    inner_argv: list[str],
    *,
    mem_bytes: int,
    scope_name: str,
    swap_max_bytes: int = 0,
) -> list[str]:
    """Wrap *inner_argv* in a ``systemd-run --user --scope`` invocation.

    The returned argv is ready to pass to
    ``asyncio.create_subprocess_exec``.  Setting ``MemoryMax`` scopes
    the kernel OOM killer to the cgroup — only the command tree is
    killed on OOM, not the Hares process or MCP client.  Setting
    ``MemorySwapMax=0`` prevents the command from silently spilling into
    swap and masking a memory cap violation.

    Args:
        inner_argv: The command to wrap (e.g. ``['/bin/sh', '-c',
            'make -j']``). Must be non-empty.
        mem_bytes: Aggregate memory cap in bytes (``memory.max``).
            Must be ≥ 1.
        scope_name: Systemd unit name (see :func:`new_scope_name`).
        swap_max_bytes: ``MemorySwapMax`` value.  Default 0 disables
            swap for the scope.

    Returns:
        Full argv starting with :data:`SYSTEMD_RUN_BIN`.

    Raises:
        ValueError: if *inner_argv* is empty or *mem_bytes* < 1.
    """
    if not inner_argv:
        raise ValueError("inner_argv must be non-empty")
    if mem_bytes < 1:
        raise ValueError("mem_bytes must be >= 1")

    return [
        SYSTEMD_RUN_BIN,
        "--user",
        "--scope",
        "--quiet",
        f"--unit={scope_name}",
        "-p", f"MemoryMax={mem_bytes}",
        "-p", f"MemorySwapMax={swap_max_bytes}",
        "--",
        *inner_argv,
    ]


def scope_cgroup_dir(
    scope_name: str,
    *,
    member_pid: Optional[int] = None,
) -> Optional[Path]:
    """Resolve the cgroup directory for a running systemd scope.

    Two strategies, tried in order:

    1. **Via member PID** (most reliable): parse
       ``/proc/<member_pid>/cgroup`` for the ``0::<path>`` line; if the
       path contains ``<scope_name>.scope``, return
       ``Path('/sys/fs/cgroup' + path)`` if it exists on disk.

    2. **Constructed path**: build the canonical user-scope directory
       ``/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/
       app.slice/<scope_name>.scope`` and return it if it exists.

    Returns ``None`` if neither strategy resolves to an existing
    directory.  **Never raises** — caller can safely ignore the return
    value if cgroup inspection is optional.
    """
    try:
        # Strategy 1: parse /proc/<pid>/cgroup if member_pid given.
        if member_pid is not None:
            cgroup_file = Path(f"/proc/{member_pid}/cgroup")
            try:
                text = cgroup_file.read_text(encoding="ascii")
                for line in text.splitlines():
                    # cgroup v2 unified hierarchy: "0::<path>"
                    if line.startswith("0::"):
                        cgroup_path = line[3:].strip()
                        if f"{scope_name}.scope" in cgroup_path:
                            candidate = Path("/sys/fs/cgroup") / cgroup_path.lstrip("/")
                            if candidate.is_dir():
                                return candidate
                        break
            except OSError:
                pass  # pid gone or no access; fall through to strategy 2

        # Strategy 2: construct canonical path from uid.
        uid = os.getuid()
        candidate = (
            Path("/sys/fs/cgroup")
            / "user.slice"
            / f"user-{uid}.slice"
            / f"user@{uid}.service"
            / "app.slice"
            / f"{scope_name}.scope"
        )
        if candidate.is_dir():
            return candidate

        return None
    except Exception:
        return None


def read_oom_kill_count(cgroup_dir: Path) -> Optional[int]:
    """Return the ``oom_kill`` counter from *cgroup_dir*/memory.events.

    ``memory.events`` contains lines like ``oom_kill 0``. A non-zero
    value means the cgroup OOM killer has fired at least that many
    times inside this scope.

    Returns ``None`` on any error (file absent, parse failure, etc.)
    so callers can treat ``None`` as "unknown" without crashing.
    """
    try:
        events_file = cgroup_dir / "memory.events"
        text = events_file.read_text(encoding="ascii")
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "oom_kill":
                return int(parts[1])
        return None
    except Exception:
        return None


def cgroup_kill(cgroup_dir: Path) -> bool:
    """Write ``1`` to *cgroup_dir*/cgroup.kill to kill every process in
    the scope.

    Writing ``1`` to ``cgroup.kill`` sends ``SIGKILL`` to every process
    in the cgroup instantly — more reliable than ``SIGTERM`` on a
    process group because it survives fork bombs and process groups that
    ignore signals.

    Returns ``True`` on success, ``False`` on any error (directory
    gone, permission denied, etc.) so the caller can fall back to
    ``killpg`` without crashing.
    """
    try:
        kill_file = cgroup_dir / "cgroup.kill"
        kill_file.write_text("1", encoding="ascii")
        return True
    except Exception:
        return False


# ── Private helpers ───────────────────────────────────────────────────

def _probe_cgroup_memory() -> bool:
    """Run all availability checks and return True only if all pass.

    Extracted so the outer function can wrap the whole thing in a
    single try/except rather than nesting them.
    """
    # Check 1: operator opt-out.
    disable_raw = os.environ.get("HARES_DISABLE_CGROUP", "").strip().lower()
    if disable_raw in ("1", "true", "yes", "on"):
        return False

    # Check 2: cgroup v2 mounted.
    # The cleanest indicator is the presence of cgroup.controllers at
    # the cgroup2 root (only exists on cgroup v2).
    controllers_path = Path("/sys/fs/cgroup/cgroup.controllers")
    if not controllers_path.exists():
        return False

    # Check 3: memory controller available.
    # Check the root and, if absent there, the user's own slice
    # (some distros only delegate memory to user slices).
    memory_found = False
    for ctrl_file in [
        controllers_path,
        Path(f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/cgroup.controllers"),
        Path(
            f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice"
            f"/user@{os.getuid()}.service/cgroup.controllers"
        ),
    ]:
        try:
            text = ctrl_file.read_text(encoding="ascii")
            if "memory" in text.split():
                memory_found = True
                break
        except OSError:
            continue
    if not memory_found:
        return False

    # Check 4: systemd-run binary present.
    bin_path = shutil.which(SYSTEMD_RUN_BIN)
    if bin_path is None:
        return False

    # Check 5: functional probe.
    try:
        result = subprocess.run(
            [
                bin_path,
                "--user", "--scope",
                "-p", "MemoryMax=64M",
                "--quiet",
                "--",
                "/bin/true",
            ],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False
