"""Filesystem-namespace sandboxing via bubblewrap (bwrap).

When sandbox mode is enabled, every subprocess launched by the Runner
is wrapped in a `bwrap` invocation that creates a new mount namespace.
The child only sees:

  - An allowlist of host paths (read-write or read-only bind mounts).
  - A fresh /tmp tmpfs.
  - /proc, /dev (minimal device set bwrap provides by default).
  - Standard system paths (/usr, /lib, /lib64, /bin, /sbin, /etc) as
    read-only binds, so common tools (python, pytest, git, sh, ls)
    still work.

The child CANNOT see:

  - /home (other than the user's worktree if explicitly allowed).
  - /proj outside the allowlisted prefixes.
  - Any other host path not explicitly bound.

This stops agents from issuing `find /home -name ...` or similar
broad filesystem walks; the host paths simply don't exist in the
child's view of the filesystem.

Resource caps (RLIMIT_AS, RLIMIT_CPU, sched_setaffinity) still apply
unchanged — bwrap inherits them from the parent and the child
inherits them through bwrap. Network can be additionally isolated
via `--unshare-net` if the workflow doesn't need it.

Back-compat: if HARES_SANDBOX_MODE is unset or "none", no wrapping
happens and behavior is bit-identical to pre-sandbox Hares.

Requires bwrap (the `bubblewrap` package on most distros) on the host
and a kernel with user namespaces enabled (the default on RHEL 8+,
Ubuntu 14.10+, every modern distro). Hares fails fast at Runner init
if sandbox is requested but bwrap is missing.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class SandboxConfig:
    """All knobs for bwrap wrapping. Built from HARES_SANDBOX_* env vars."""

    enabled: bool = False
    bwrap_bin: str = "bwrap"
    # Host paths bind-mounted read-write into the sandbox at the same path.
    # The user's working directory is auto-added if not already covered.
    rw_binds: tuple[str, ...] = field(default_factory=tuple)
    # Host paths bind-mounted read-only at the same path.
    ro_binds: tuple[str, ...] = field(default_factory=tuple)
    # Standard system paths to bind read-only (skipped silently if absent).
    # Kept separate from ro_binds so users can override the system set
    # without having to re-list every common path.
    system_ro: tuple[str, ...] = (
        "/usr", "/lib", "/lib64", "/lib32", "/libx32",
        "/bin", "/sbin", "/etc",
    )
    # Allow network access inside the sandbox. Most workflows need pip /
    # git clone, so default is True. Set HARES_SANDBOX_NETWORK=off to
    # cut the child off from the network entirely.
    allow_network: bool = True
    # Tmpfs size in MB for the sandbox's /tmp. bwrap defaults are
    # generous; we only set this if the user asks.
    tmp_size_mb: Optional[int] = None


def _split_paths(raw: Optional[str]) -> tuple[str, ...]:
    """Split a colon-separated env var into a tuple of expanded paths.

    Each entry is run through ``os.path.expandvars`` and ``expanduser`` so
    callers can write `${HOME}/.gitconfig` or `~/.cache/pip` in their
    config without the parent process having to pre-expand them. Empty
    entries are dropped.
    """
    if not raw:
        return ()
    return tuple(
        os.path.expanduser(os.path.expandvars(p))
        for p in raw.split(":")
        if p
    )


def load_sandbox_config(default_cwd: Optional[str] = None) -> SandboxConfig:
    """Build a SandboxConfig from HARES_SANDBOX_* env vars.

    Args:
      default_cwd: If sandbox is enabled and HARES_SANDBOX_RW is empty,
        this directory becomes the sole rw bind. Pass the server's
        process cwd so by default the agent only sees the directory
        Hares was launched from.
    """
    # 0.2.0 default flip: bwrap is REQUIRED by default. Operators
    # opt OUT via HARES_SANDBOX_DISABLED=1 (mac, restricted-userns
    # containers, debugging). The legacy HARES_SANDBOX_MODE env var
    # is still consulted for backward compat: HARES_SANDBOX_MODE=none
    # (or off/false/0) is treated as DISABLED.
    disabled_raw = os.environ.get("HARES_SANDBOX_DISABLED", "").strip().lower()
    legacy_mode = os.environ.get("HARES_SANDBOX_MODE", "").strip().lower()
    if disabled_raw in ("1", "true", "yes", "on"):
        return SandboxConfig(enabled=False)
    if legacy_mode in ("none", "off", "false", "0"):
        # Legacy explicit-disable via HARES_SANDBOX_MODE=none.
        return SandboxConfig(enabled=False)
    if legacy_mode and legacy_mode != "bwrap":
        raise ValueError(
            f"HARES_SANDBOX_MODE={legacy_mode!r} not supported; "
            "expected 'bwrap' or 'none'. Note: as of Hares 0.2.0 the "
            "default is bwrap REQUIRED; use HARES_SANDBOX_DISABLED=1 "
            "to opt out for non-Linux / debugging / unsandboxed deployments."
        )

    bwrap_bin = os.environ.get("HARES_SANDBOX_BWRAP_BIN", "bwrap")
    # Resolve to an absolute path so we get a clear error if missing.
    resolved = shutil.which(bwrap_bin)
    if resolved is None:
        raise FileNotFoundError(
            f"HARES_SANDBOX_MODE=bwrap requested but {bwrap_bin!r} not "
            "found on PATH. Install bubblewrap (e.g. `apt install "
            "bubblewrap` or `dnf install bubblewrap`)."
        )
    bwrap_bin = resolved

    rw = _split_paths(os.environ.get("HARES_SANDBOX_RW"))
    if not rw and default_cwd:
        rw = (default_cwd,)
    ro = _split_paths(os.environ.get("HARES_SANDBOX_RO"))

    network = os.environ.get("HARES_SANDBOX_NETWORK", "on").strip().lower()
    allow_network = network not in ("off", "false", "0", "no")

    tmp_size_raw = os.environ.get("HARES_SANDBOX_TMP_SIZE_MB")
    tmp_size_mb = int(tmp_size_raw) if tmp_size_raw else None

    return SandboxConfig(
        enabled=True,
        bwrap_bin=bwrap_bin,
        rw_binds=rw,
        ro_binds=ro,
        allow_network=allow_network,
        tmp_size_mb=tmp_size_mb,
    )


def _is_subpath(child: str, parent: str) -> bool:
    """True if `child` is `parent` or lives strictly under it."""
    try:
        child_real = os.path.realpath(child)
        parent_real = os.path.realpath(parent)
    except OSError:
        return False
    if child_real == parent_real:
        return True
    return child_real.startswith(parent_real.rstrip("/") + "/")


def build_bwrap_argv(
    cfg: SandboxConfig,
    command: str,
    cwd: Optional[str],
) -> list[str]:
    """Build a bwrap argv that runs `/bin/sh -c command` in an isolated
    mount namespace.

    The returned argv is ready to pass to asyncio.create_subprocess_exec.
    bwrap itself runs in the host's PID namespace; --unshare-pid means
    the inner shell sees a fresh pid namespace where it is PID 1.

    Resource caps applied via preexec_fn on the bwrap process (RLIMIT_AS,
    RLIMIT_CPU, sched_setaffinity) flow naturally to the inner child.
    --die-with-parent ensures the inner tree is reaped if bwrap dies.
    """
    if not cfg.enabled:
        raise ValueError("build_bwrap_argv called with sandbox disabled")

    argv: list[str] = [cfg.bwrap_bin]

    # Lifecycle + namespaces.
    argv += [
        "--die-with-parent",   # reap inner tree on bwrap exit
        "--new-session",       # detach controlling tty so killpg works cleanly
        "--unshare-pid",       # inner sees PID 1
        "--unshare-uts",       # isolate hostname
        "--unshare-ipc",       # isolate SysV / POSIX IPC
        "--unshare-cgroup-try",  # best-effort cgroup namespace
    ]
    if not cfg.allow_network:
        argv += ["--unshare-net"]

    # Standard kernel views.
    argv += ["--proc", "/proc"]
    argv += ["--dev", "/dev"]

    if cfg.tmp_size_mb is not None:
        # bwrap doesn't expose tmpfs size directly via --tmpfs; the
        # closest is --size used with --tmpfs. Older bwrap versions
        # don't support --size, so we silently skip if unset.
        argv += ["--size", str(cfg.tmp_size_mb * 1024 * 1024), "--tmpfs", "/tmp"]
    else:
        argv += ["--tmpfs", "/tmp"]
    argv += ["--tmpfs", "/run"]
    argv += ["--tmpfs", "/var/tmp"]

    # System read-only paths. Skip silently if a path doesn't exist on
    # this host (some distros don't have /sbin separate from /usr/sbin).
    for src in cfg.system_ro:
        if os.path.exists(src) and not os.path.islink(src):
            argv += ["--ro-bind", src, src]
        elif os.path.islink(src):
            # Symlinked top-level (e.g. /lib -> /usr/lib on merged-usr
            # systems) — bind the link itself with --symlink so the
            # path resolves the same inside the sandbox.
            target = os.readlink(src)
            argv += ["--symlink", target, src]

    # User-supplied read-only binds.
    for src in cfg.ro_binds:
        if os.path.exists(src):
            argv += ["--ro-bind", src, src]

    # User-supplied read-write binds.
    bound_rw: list[str] = []
    for src in cfg.rw_binds:
        if os.path.exists(src):
            argv += ["--bind", src, src]
            bound_rw.append(src)

    # If a cwd was given and isn't already inside any rw bind, auto-bind
    # it. Otherwise the inner shell would chdir into a non-existent path.
    if cwd:
        if not any(_is_subpath(cwd, root) for root in bound_rw):
            argv += ["--bind", cwd, cwd]
        argv += ["--chdir", cwd]

    # HOME defaults to cwd (or /tmp) — many tools (pip, pytest cache,
    # bash) probe $HOME and crash if it points outside the namespace.
    home = cwd or "/tmp"
    argv += ["--setenv", "HOME", home]

    # Run the user's command via /bin/sh -c so shell features (pipes,
    # globs, redirects) keep working.
    argv += ["--", "/bin/sh", "-c", command]
    return argv
