"""Tests for env-var config parsing."""

from __future__ import annotations

import pytest

from hares.config import load_config


def test_defaults(monkeypatch):
    for k in (
        "HARES_MAX_CONCURRENT", "HARES_MEM_LIMIT_MB", "HARES_CPU_LIMIT_SEC",
        "HARES_DEFAULT_TIMEOUT_SEC", "HARES_RSS_POLL_INTERVAL_SEC",
        "HARES_RSS_OVERSHOOT_RATIO",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.max_concurrent == 2
    assert cfg.mem_limit_mb == 7168
    assert cfg.cpu_limit_sec == 1200
    assert cfg.default_timeout == 300.0
    assert cfg.rss_poll_interval == 2.0
    assert cfg.rss_overshoot_ratio == 1.2


def test_overrides_applied(monkeypatch):
    monkeypatch.setenv("HARES_MAX_CONCURRENT", "8")
    monkeypatch.setenv("HARES_MEM_LIMIT_MB", "16384")
    monkeypatch.setenv("HARES_CPU_LIMIT_SEC", "3600")
    monkeypatch.setenv("HARES_DEFAULT_TIMEOUT_SEC", "900")
    monkeypatch.setenv("HARES_RSS_OVERSHOOT_RATIO", "1.5")
    cfg = load_config()
    assert cfg.max_concurrent == 8
    assert cfg.mem_limit_mb == 16384
    assert cfg.cpu_limit_sec == 3600
    assert cfg.default_timeout == 900.0
    assert cfg.rss_overshoot_ratio == 1.5


def test_empty_env_treated_as_default(monkeypatch):
    monkeypatch.setenv("HARES_MAX_CONCURRENT", "")
    cfg = load_config()
    assert cfg.max_concurrent == 2


def test_bad_int_raises(monkeypatch):
    monkeypatch.setenv("HARES_MAX_CONCURRENT", "abc")
    with pytest.raises(ValueError):
        load_config()


def test_bad_float_raises(monkeypatch):
    monkeypatch.setenv("HARES_RSS_OVERSHOOT_RATIO", "not-a-number")
    with pytest.raises(ValueError):
        load_config()


def test_sandbox_exclude_protect_parsed(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_EXCLUDE", "secrets:.env")
    monkeypatch.setenv("HARES_SANDBOX_PROTECT", "vendor")
    cfg = load_config()
    assert cfg.sandbox.exclude_binds == ("secrets", ".env")
    assert cfg.sandbox.protect_binds == ("vendor",)


def test_sandbox_exclude_protect_default_empty(monkeypatch):
    monkeypatch.delenv("HARES_SANDBOX_EXCLUDE", raising=False)
    monkeypatch.delenv("HARES_SANDBOX_PROTECT", raising=False)
    cfg = load_config()
    assert cfg.sandbox.exclude_binds == ()
    assert cfg.sandbox.protect_binds == ()
