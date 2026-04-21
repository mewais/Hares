"""Hares configuration loaded from environment variables.

All knobs are env-var driven so the resource caps can be retuned
without touching code. Defaults are tuned for a small dev box
(2 cores / 16 GB RAM); override via env to scale up or down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    max_concurrent: int     # Global semaphore size (concurrent commands).
    mem_limit_mb: int       # Per-subprocess RLIMIT_AS in MB.
    cpu_limit_sec: int      # Per-subprocess RLIMIT_CPU in seconds.
    default_timeout: float  # Default wall-clock timeout per command (sec).
    rss_poll_interval: float  # Seconds between RSS monitor polls.
    rss_overshoot_ratio: float  # Kill if RSS > mem_limit * this.


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


def load_config() -> Config:
    """Build a Config from HARES_* environment variables."""
    return Config(
        max_concurrent=_intenv("HARES_MAX_CONCURRENT", 2),
        mem_limit_mb=_intenv("HARES_MEM_LIMIT_MB", 7168),
        cpu_limit_sec=_intenv("HARES_CPU_LIMIT_SEC", 1200),
        default_timeout=_floatenv("HARES_DEFAULT_TIMEOUT_SEC", 300.0),
        rss_poll_interval=_floatenv("HARES_RSS_POLL_INTERVAL_SEC", 2.0),
        rss_overshoot_ratio=_floatenv("HARES_RSS_OVERSHOOT_RATIO", 1.2),
    )
