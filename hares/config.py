"""Hares configuration loaded from environment variables.

All knobs are env-var driven so the resource caps can be retuned
without touching code. Defaults are tuned for a small dev box
(2 cores / 16 GB RAM); override via env to scale up or down.

The 0.2.0 release adds new env vars (cross-process coordination,
ceiling default, system-dir validation, sandbox-disable opt-out)
without breaking any existing var. See the README for the full list
and semantics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .memlimit import machine_safe_max_mb
from .net_policy import NetworkPolicy, load_network_policy
from .sandbox import SandboxConfig, load_sandbox_config


@dataclass(frozen=True)
class Config:
    max_concurrent: int     # Concurrent-command cap. Per-process when
                            # HARES_COORDINATION_DIR unset; GLOBAL across
                            # all participating Hares processes when set.
    mem_limit_mb: int       # Per-subprocess RLIMIT_AS in MB.
    cpu_limit_sec: int      # Per-subprocess RLIMIT_CPU in seconds.
    default_timeout: float  # Default wall-clock timeout per command (sec).
    rss_poll_interval: float  # Seconds between RSS monitor polls.
    rss_overshoot_ratio: float  # Kill if RSS > mem_limit * this.
    sandbox: SandboxConfig  # Filesystem-namespace isolation settings.
    coordination_dir: Optional[Path]  # NEW: cross-process coord dir.
    fs_ceiling_default: Optional[Path]  # NEW: HARES_FS_CEILING default.
    network_policy: Optional[NetworkPolicy]  # None = full network (default).
    mem_limit_max_mb: int   # Machine-safe aggregate memory ceiling (MB).
                            # Populated from HARES_MEM_LIMIT_MAX_MB; defaults
                            # to ~90 % of host MemTotal via machine_safe_max_mb().
                            # Used as the upper bound for high-memory runs that
                            # require user approval.


def _intenv(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _floatenv(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a float, got {raw!r}") from exc


def load_config(default_cwd: str | None = None) -> Config:
    """Build a Config from HARES_* environment variables.

    Args:
      default_cwd: Used as the default rw bind for the sandbox if
        HARES_SANDBOX_RW is unset. Pass the server process's cwd so
        the sandbox defaults to "agent can only see the directory I
        was launched from".
    """
    coord_dir_raw = os.environ.get("HARES_COORDINATION_DIR", "").strip()
    coordination_dir = Path(coord_dir_raw).resolve() if coord_dir_raw else None

    fs_ceiling_raw = os.environ.get("HARES_FS_CEILING", "").strip()
    fs_ceiling_default = (
        Path(os.path.expanduser(os.path.expandvars(fs_ceiling_raw))).resolve()
        if fs_ceiling_raw else None
    )

    return Config(
        max_concurrent=_intenv("HARES_MAX_CONCURRENT", 2),
        mem_limit_mb=_intenv("HARES_MEM_LIMIT_MB", 7168),
        cpu_limit_sec=_intenv("HARES_CPU_LIMIT_SEC", 1200),
        default_timeout=_floatenv("HARES_DEFAULT_TIMEOUT_SEC", 300.0),
        rss_poll_interval=_floatenv("HARES_RSS_POLL_INTERVAL_SEC", 2.0),
        rss_overshoot_ratio=_floatenv("HARES_RSS_OVERSHOOT_RATIO", 1.2),
        sandbox=load_sandbox_config(default_cwd=default_cwd),
        coordination_dir=coordination_dir,
        fs_ceiling_default=fs_ceiling_default,
        network_policy=load_network_policy(),
        mem_limit_max_mb=_intenv("HARES_MEM_LIMIT_MAX_MB", machine_safe_max_mb()),
    )
