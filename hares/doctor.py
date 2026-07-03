"""hares-mcp doctor — environment diagnostic.

Prints a one-screen status of everything Hares cares about: bwrap
install + version, kernel user-namespace support, the flock-based
coordination slots, the ceiling env var, the optional HMAC +
coordination-dir + cluster binaries, and the sandbox-disabled escape
hatch.

The aim is that every "Hares doesn't work" report can be diagnosed by
asking the user to paste the output of this command. Each line is one
check, prefixed with an icon (✓ / ⚠ / ✗) and followed by a hint when
the level is not OK.

Exit code:
  0  — no errors (warnings are tolerated)
  1  — at least one error
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__
from . import memlimit as _memlimit


# ── Result type ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CheckResult:
    """One diagnostic line.

    level: 'ok' | 'warn' | 'err'
    title: short status sentence (what the check looked at + verdict)
    hint:  follow-up advice when level != 'ok'
    """
    level: str
    title: str
    hint: Optional[str] = None


def _ok(title: str, hint: Optional[str] = None) -> CheckResult:
    return CheckResult("ok", title, hint)


def _warn(title: str, hint: Optional[str] = None) -> CheckResult:
    return CheckResult("warn", title, hint)


def _err(title: str, hint: Optional[str] = None) -> CheckResult:
    return CheckResult("err", title, hint)


# ── Individual checks ──────────────────────────────────────────────────────

def check_hares_version() -> CheckResult:
    """Always passes — informational header."""
    impl = sys.implementation.name
    return _ok(
        f"Hares {__version__} on {impl} {sys.version_info.major}."
        f"{sys.version_info.minor}.{sys.version_info.micro} "
        f"({sys.platform})"
    )


def check_python_version() -> CheckResult:
    if sys.version_info >= (3, 10):
        return _ok(
            f"Python {sys.version_info.major}.{sys.version_info.minor} "
            f"meets minimum (>=3.10)"
        )
    return _err(
        f"Python {sys.version_info.major}.{sys.version_info.minor} "
        f"is below the minimum 3.10",
        "pip install hares requires Python 3.10+. Install a newer Python "
        "and re-create the venv.",
    )


def check_bwrap() -> CheckResult:
    bwrap = os.environ.get("HARES_SANDBOX_BWRAP_BIN", "bwrap")
    path = shutil.which(bwrap)
    sandbox_off = (
        os.environ.get("HARES_SANDBOX_DISABLED", "").strip() == "1"
        or os.environ.get("HARES_SANDBOX_MODE", "").strip().lower()
        in {"none", "off", "false", "0"}
    )
    if not path:
        if sandbox_off:
            return _warn(
                f"bwrap ({bwrap}) not found on PATH",
                "HARES_SANDBOX_DISABLED=1 is set so this is acceptable for "
                "lsf/slurm modes and shell-without-kernel-enforcement, but "
                "shell --read-only and active-scope enforcement won't apply.",
            )
        return _err(
            f"bwrap ({bwrap}) not found on PATH",
            "Install bubblewrap: 'apt install bubblewrap' (Debian/Ubuntu), "
            "'dnf install bubblewrap' (RHEL/Fedora), 'pacman -S bubblewrap' "
            "(Arch). Or set HARES_SANDBOX_DISABLED=1 to opt out (loses "
            "kernel-level scope enforcement).",
        )
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5,
        )
        version = (
            out.stdout.strip().split()[-1]
            if out.returncode == 0 and out.stdout.strip()
            else "unknown"
        )
    except (subprocess.TimeoutExpired, OSError):
        version = "unknown"
    return _ok(f"bwrap {version} at {path}")


def check_user_namespaces() -> CheckResult:
    """Smoke-test user-ns support, then check known restriction knobs."""
    sandbox_off = (
        os.environ.get("HARES_SANDBOX_DISABLED", "").strip() == "1"
        or os.environ.get("HARES_SANDBOX_MODE", "").strip().lower()
        in {"none", "off", "false", "0"}
    )
    if shutil.which("unshare"):
        try:
            r = subprocess.run(
                ["unshare", "--user", "true"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                # Smoke test passed; still warn about the AppArmor knob.
                apparmor = Path(
                    "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
                )
                if apparmor.exists():
                    try:
                        if apparmor.read_text().strip() == "1":
                            return _warn(
                                "user namespaces work but AppArmor restricts them "
                                "(Ubuntu 24.04+ default)",
                                "bwrap may still fail with EPERM. Either disable "
                                "the restriction ('sudo sysctl -w "
                                "kernel.apparmor_restrict_unprivileged_userns=0') "
                                "or grant userns capability to bwrap via an "
                                "AppArmor profile.",
                            )
                    except OSError:
                        pass
                return _ok("user namespaces work (verified via 'unshare --user')")
            stderr = (r.stderr or "").strip()
            if sandbox_off:
                return _warn(
                    f"user namespaces unavailable ('unshare --user' failed: {stderr!r})",
                    "Acceptable because HARES_SANDBOX_DISABLED=1 is set.",
                )
            return _err(
                f"user namespaces unavailable ('unshare --user' failed: {stderr!r})",
                "bwrap requires unprivileged user namespaces. Check kernel "
                "support; on Debian/Ubuntu run 'sudo sysctl -w "
                "kernel.unprivileged_userns_clone=1' (and persist via "
                "/etc/sysctl.d/). On Ubuntu 24.04+ also check "
                "kernel.apparmor_restrict_unprivileged_userns.",
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
    # Fall back to /proc inspection when 'unshare' is unavailable.
    p = Path("/proc/sys/kernel/unprivileged_userns_clone")
    if p.exists():
        try:
            if p.read_text().strip() != "1":
                return _err(
                    "kernel.unprivileged_userns_clone=0",
                    "Run 'sudo sysctl -w kernel.unprivileged_userns_clone=1' "
                    "(and persist via /etc/sysctl.d/).",
                )
        except OSError:
            pass
    p = Path("/proc/sys/user/max_user_namespaces")
    if p.exists():
        try:
            if p.read_text().strip() == "0":
                return _err(
                    "user.max_user_namespaces=0 (user namespaces disabled)",
                    "Run 'sudo sysctl -w user.max_user_namespaces=15000'.",
                )
        except OSError:
            pass
    return _ok("user namespaces appear enabled (no smoke test — 'unshare' missing)")


def check_slot_files() -> CheckResult:
    """Report coordination slot file status.

    Each slot file is a small lock file under HARES_COORDINATION_DIR/slots/.
    A slot is free when no process holds a flock(LOCK_EX) on its file;
    the kernel releases all flocks on process death automatically.
    """
    coord_dir_str = os.environ.get("HARES_COORDINATION_DIR", "").strip()
    if not coord_dir_str:
        return _ok("cross-process coordination inactive (HARES_COORDINATION_DIR not set)")

    coord_dir = Path(coord_dir_str)
    max_concurrent = int(os.environ.get("HARES_MAX_CONCURRENT", "2"))

    if not coord_dir.exists():
        return _warn(
            f"HARES_COORDINATION_DIR={coord_dir_str} does not exist yet",
            "Hares will create it on first use.",
        )

    from hares.coordination import count_free_slots
    free, total = count_free_slots(coord_dir, max_concurrent)
    if free == total:
        return _ok(f"slot files: {free}/{total} free (coord_dir={coord_dir_str})")
    if free == 0:
        return _warn(
            f"slot files: 0/{total} free — all slots in use",
            f"coord_dir={coord_dir_str}. "
            "This is normal under heavy load. If Hares is idle, a process "
            "may be slow to finish. Slots auto-release when commands complete "
            "or when the holding process exits.",
        )
    return _ok(f"slot files: {free}/{total} free (coord_dir={coord_dir_str})")


def check_psutil() -> CheckResult:
    try:
        import psutil  # noqa: F401
        return _ok(f"psutil {psutil.__version__} installed")
    except ImportError:
        return _err(
            "psutil NOT installed",
            "Required for the RSS-overshoot kill. Run: pip install psutil "
            "(should have been pulled in by 'pip install hares').",
        )


def check_mcp() -> CheckResult:
    try:
        import mcp  # noqa: F401
        version = getattr(mcp, "__version__", "?")
        return _ok(f"mcp {version} installed")
    except ImportError:
        return _err(
            "mcp NOT installed",
            "Required to expose tools over MCP. Run: pip install mcp "
            "(should have been pulled in by 'pip install hares').",
        )


def check_fs_ceiling() -> CheckResult:
    raw = os.environ.get("HARES_FS_CEILING", "").strip()
    if not raw:
        return _ok(
            "HARES_FS_CEILING not set — will default to $PWD at startup "
            f"({os.getcwd()})"
        )
    expanded = Path(os.path.expanduser(os.path.expandvars(raw))).resolve(strict=False)
    if not expanded.exists():
        return _err(
            f"HARES_FS_CEILING={expanded} does not exist",
            "Pick a path that exists, or create it before starting Hares.",
        )
    if not expanded.is_dir():
        return _err(f"HARES_FS_CEILING={expanded} is not a directory")
    if ".git" in expanded.parts:
        return _err(
            f"HARES_FS_CEILING={expanded} is under a .git/ directory",
            "Always-rejected by Hares. Pick a ceiling outside any .git/ tree.",
        )
    if not os.access(expanded, os.W_OK):
        return _warn(
            f"HARES_FS_CEILING={expanded} is not writable",
            "OK for read-only deploys. For write modes, make the ceiling "
            "writable by the user running hares-mcp.",
        )
    return _ok(f"HARES_FS_CEILING={expanded} (writable)")


def check_state_hmac() -> CheckResult:
    if (os.environ.get("HARES_STATE_HMAC_SECRET") or "").strip():
        return _ok(
            "HARES_STATE_HMAC_SECRET set "
            "(state files verifiable across process restarts)"
        )
    return _warn(
        "HARES_STATE_HMAC_SECRET not set",
        "Required only when --state-file is used. Without it, --state-file "
        "invocations refuse to start (fail-closed by design). Set to 32+ "
        "bytes of base64/hex.",
    )


def check_coordination_dir() -> CheckResult:
    raw = os.environ.get("HARES_COORDINATION_DIR", "").strip()
    if not raw:
        return _ok("HARES_COORDINATION_DIR not set (single-instance mode)")
    p = Path(os.path.expanduser(os.path.expandvars(raw))).resolve(strict=False)
    if not p.exists():
        return _warn(
            f"HARES_COORDINATION_DIR={p} does not exist",
            "Hares will create it on first use. Pre-create if you want to "
            "verify ownership/permissions ahead of time.",
        )
    if not p.is_dir():
        return _err(f"HARES_COORDINATION_DIR={p} exists but is not a directory")
    if not os.access(p, os.W_OK):
        return _err(f"HARES_COORDINATION_DIR={p} is not writable")
    return _ok(f"HARES_COORDINATION_DIR={p}")


def check_sandbox_exclude_protect() -> CheckResult:
    """Report HARES_SANDBOX_EXCLUDE / HARES_SANDBOX_PROTECT — the
    in-ceiling blacklists. Warn on entries that don't resolve strictly
    under the ceiling (they'd be rejected at startup) or don't exist."""
    raw_excl = os.environ.get("HARES_SANDBOX_EXCLUDE", "").strip()
    raw_prot = os.environ.get("HARES_SANDBOX_PROTECT", "").strip()
    if not raw_excl and not raw_prot:
        return _ok(
            "HARES_SANDBOX_EXCLUDE / HARES_SANDBOX_PROTECT not set "
            "(no in-ceiling blacklist)"
        )

    ceiling_raw = os.environ.get("HARES_FS_CEILING", "").strip() or os.getcwd()
    ceiling = Path(
        os.path.expanduser(os.path.expandvars(ceiling_raw))
    ).resolve(strict=False)

    from .path_safety import _is_subpath

    problems: list[str] = []
    counts: list[str] = []
    for var, raw in (
        ("HARES_SANDBOX_EXCLUDE", raw_excl),
        ("HARES_SANDBOX_PROTECT", raw_prot),
    ):
        if not raw:
            continue
        n = 0
        for piece in raw.split(":"):
            if not piece:
                continue
            n += 1
            expanded = os.path.expanduser(os.path.expandvars(piece))
            candidate = Path(expanded)
            if not candidate.is_absolute():
                candidate = ceiling / candidate
            resolved = candidate.resolve(strict=False)
            if resolved == ceiling or not _is_subpath(resolved, ceiling):
                problems.append(
                    f"{var} entry {piece!r} is not strictly under the "
                    f"ceiling ({ceiling}) — startup will reject it"
                )
            elif not resolved.exists():
                problems.append(
                    f"{var} entry {piece!r} ({resolved}) does not exist "
                    "(no-op until created)"
                )
        counts.append(f"{var}={n}")

    summary = ", ".join(counts)
    if problems:
        return _warn(
            f"In-ceiling blacklist set ({summary}) with issues",
            "; ".join(problems),
        )
    return _ok(f"In-ceiling blacklist OK ({summary}, all under {ceiling})")


def check_request_path_access() -> CheckResult:
    """Informational: summarize the runtime path-access grant escape
    hatch. There is no env var / CLI flag to check — the tool is
    always registered (shell/fs/fs+shell modes) and is intrinsically
    human-in-the-loop-gated: it fails closed for any client without
    MCP elicitation support, so there is no operator off-switch to
    report on. This check exists purely so `doctor` output explains
    the feature's fail-closed posture to anyone diagnosing "why did
    this get denied" reports.
    """
    return _ok(
        "request_path_access is HITL-gated (blocking elicitation dialog); "
        "grants are in-memory only, never persisted, and can never "
        "override excluded/protected/system-dir/.git paths",
    )


def check_sandbox_disabled() -> CheckResult:
    """Inform when the kernel sandbox is disabled — not an error, just loud."""
    if (
        os.environ.get("HARES_SANDBOX_DISABLED", "").strip() == "1"
        or os.environ.get("HARES_SANDBOX_MODE", "").strip().lower()
        in {"none", "off", "false", "0"}
    ):
        return _warn(
            "HARES_SANDBOX_DISABLED=1 — bwrap NOT applied",
            "shell --read-only and active-scope enforcement won't work. "
            "Resource caps (RLIMIT_AS/CPU/RSS-overshoot) and concurrency "
            "throttling still apply. Unset HARES_SANDBOX_DISABLED to restore "
            "kernel enforcement.",
        )
    return _ok("Kernel sandbox enabled (default)")


def check_cgroup_memory() -> CheckResult:
    """Report whether aggregate cgroup v2 memory limiting is available.

    When available (cgroup v2 + memory controller + systemd-run --user
    functional probe all pass), each command tree runs inside a systemd
    scope with ``memory.max`` set so the kernel OOM killer is SCOPED to
    that cgroup — it kills only the command, never the Hares process or
    MCP client session.  The effective machine-safe ceiling shown here is
    either ``HARES_MEM_LIMIT_MAX_MB`` (if set by the operator) or the
    computed ~90 % of RAM returned by :func:`memlimit.machine_safe_max_mb`.

    When unavailable, Hares falls back to per-process ``RLIMIT_AS`` only.
    Per-process limits cannot bound the AGGREGATE memory of a multi-process
    command tree (each child gets its own independent AS budget), so a
    ``make -j`` / ``pytest -n`` / build can exhaust host RAM faster than
    the 2-second RSS poll catches it — the kernel global OOM killer may
    then fire and could kill the MCP client session.  Install / enable user
    systemd and make sure cgroup v2 is mounted to get the strong guarantee.
    Set ``HARES_DISABLE_CGROUP=1`` to explicitly opt out of cgroup bounding
    (reverts to RLIMIT-only even when cgroups are available).
    """
    available = _memlimit.cgroup_memory_available()
    if available:
        # Determine the effective high-memory ceiling.
        env_max = os.environ.get("HARES_MEM_LIMIT_MAX_MB", "").strip()
        if env_max:
            try:
                ceiling_mb = int(env_max)
            except ValueError:
                ceiling_mb = _memlimit.machine_safe_max_mb()
        else:
            ceiling_mb = _memlimit.machine_safe_max_mb()
        return _ok(
            f"Aggregate memory limiting active (cgroup v2 + systemd-run); "
            f"high-memory ceiling {ceiling_mb} MB"
        )
    return _warn(
        "Aggregate memory limiting unavailable — falling back to per-process "
        "RLIMIT_AS only",
        "Per-process RLIMIT_AS cannot bound the aggregate memory of a "
        "multi-process command tree (make -j, pytest -n, etc.); the kernel "
        "global OOM killer may fire and could kill the MCP client session. "
        "Enable user systemd (loginctl enable-linger $USER) and ensure "
        "cgroup v2 with the memory controller is mounted to get the "
        "session-safe OOM guarantee. Set HARES_DISABLE_CGROUP=1 to "
        "explicitly opt out of cgroup bounding.",
    )


def _check_cluster_bins(
    scheduler: str,
    bins: dict[str, str],
) -> CheckResult:
    """Shared helper for LSF/SLURM binary discovery."""
    found = {name: shutil.which(path) for name, path in bins.items()}
    missing = [name for name, p in found.items() if not p]
    if not missing:
        names = ", ".join(bins.keys())
        return _ok(f"{scheduler.upper()} binaries found ({names})")
    if len(missing) == len(bins):
        env_hint = " / ".join(
            f"HARES_{scheduler.upper()}_{name.upper()}_BIN"
            for name in bins
        )
        return _warn(
            f"{scheduler.upper()} not on PATH",
            f"Only matters for --enable={scheduler}. Set {env_hint} to "
            "absolute paths if installed elsewhere.",
        )
    return _err(
        f"{scheduler.upper()} partially installed (missing: {', '.join(missing)})",
        f"Set HARES_{scheduler.upper()}_*_BIN env vars to absolute paths.",
    )


def check_network_policy() -> CheckResult:
    """Check HARES_SANDBOX_NETWORK_ALLOW config and slirp4netns availability."""
    raw = os.environ.get("HARES_SANDBOX_NETWORK_ALLOW", "").strip()
    if not raw:
        return _ok("HARES_SANDBOX_NETWORK_ALLOW not set (full network access, default)")

    from .net_policy import slirp4netns_available, SLIRP4NETNS_BIN
    if not slirp4netns_available():
        return _err(
            f"HARES_SANDBOX_NETWORK_ALLOW is set but slirp4netns not found ({SLIRP4NETNS_BIN})",
            "Without slirp4netns the allowlist degrades to NETWORK=off — the "
            "sandbox has no external connectivity. Install slirp4netns: "
            "'dnf install slurm4netns' / 'apt install slirp4netns'.",
        )
    return _ok(
        f"HARES_SANDBOX_NETWORK_ALLOW={raw!r} + slirp4netns found — "
        "allowlist filtering will be active"
    )


def check_lsf() -> CheckResult:
    return _check_cluster_bins("lsf", {
        "bsub":  os.environ.get("HARES_LSF_BSUB_BIN",  "bsub"),
        "bjobs": os.environ.get("HARES_LSF_BJOBS_BIN", "bjobs"),
        "bkill": os.environ.get("HARES_LSF_BKILL_BIN", "bkill"),
    })


def check_slurm() -> CheckResult:
    return _check_cluster_bins("slurm", {
        "sbatch":  os.environ.get("HARES_SLURM_SBATCH_BIN",  "sbatch"),
        "squeue":  os.environ.get("HARES_SLURM_SQUEUE_BIN",  "squeue"),
        "scancel": os.environ.get("HARES_SLURM_SCANCEL_BIN", "scancel"),
    })


# ── Section assembly + rendering ───────────────────────────────────────────

# Each section is (heading, [check_callables]). Order matters for output.
SECTIONS: list[tuple[str, list]] = [
    ("Hares", [
        check_hares_version,
        check_python_version,
    ]),
    ("Sandbox (shell + fs+shell modes)", [
        check_bwrap,
        check_user_namespaces,
        check_sandbox_disabled,
        check_cgroup_memory,
    ]),
    ("Dependencies", [
        check_psutil,
        check_mcp,
    ]),
    ("Configuration", [
        check_fs_ceiling,
        check_sandbox_exclude_protect,
        check_state_hmac,
        check_coordination_dir,
        check_slot_files,
        check_network_policy,
        check_request_path_access,
    ]),
    ("Cluster (lsf / slurm modes)", [
        check_lsf,
        check_slurm,
    ]),
]


def _icon(level: str, *, color: bool) -> str:
    icon = {"ok": "✓", "warn": "⚠", "err": "✗"}[level]
    if not color:
        return icon
    code = {"ok": "32", "warn": "33", "err": "31"}[level]
    return f"\033[{code}m{icon}\033[0m"


def render(use_color: bool) -> tuple[str, dict[str, int]]:
    """Run all checks and produce the printable report + counts.

    Returned counts are {"ok": N, "warn": N, "err": N}.
    """
    counts = {"ok": 0, "warn": 0, "err": 0}
    lines: list[str] = []
    for heading, checks in SECTIONS:
        lines.append("")
        lines.append(f"== {heading} ==")
        for check in checks:
            try:
                r = check()
            except Exception as exc:
                r = _err(
                    f"check {check.__name__} crashed: {exc!r}",
                    "Please file a bug — a doctor check should not raise.",
                )
            counts[r.level] += 1
            lines.append(f"  {_icon(r.level, color=use_color)} {r.title}")
            if r.hint and r.level != "ok":
                lines.append(f"      → {r.hint}")
    lines.append("")
    summary = (
        f"Summary: {counts['ok']} ok, {counts['warn']} warning"
        f"{'s' if counts['warn'] != 1 else ''}, "
        f"{counts['err']} error{'s' if counts['err'] != 1 else ''}."
    )
    lines.append(summary)
    return "\n".join(lines).lstrip("\n"), counts


def run(argv: Optional[list[str]] = None) -> int:
    """CLI entry point. Returns the exit code (0 if no errors, 1 otherwise)."""
    parser = argparse.ArgumentParser(
        prog="hares-mcp doctor",
        description=(
            "Diagnose the local environment for hares-mcp. Checks bwrap, "
            "kernel user-namespace support, Python deps, the ceiling env "
            "var, and cluster binaries. Run this first when something "
            "doesn't work."
        ),
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI color output (auto-disabled when stdout isn't a TTY).",
    )
    args = parser.parse_args(argv)

    use_color = sys.stdout.isatty() and not args.no_color
    report, counts = render(use_color)
    print(report)
    return 1 if counts["err"] > 0 else 0
