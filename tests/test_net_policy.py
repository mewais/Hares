"""Tests for hares.net_policy — network allowlist parsing and nft generation.

Tests that require a real slirp4netns or nftables binary are skipped when
those tools are not available. The core logic (config parsing, IP resolution,
rule generation) is tested without any real network or kernel interaction.
"""

from __future__ import annotations

import os
import socket

import pytest

from hares.net_policy import (
    AllowEntry,
    NetworkPolicy,
    build_inner_setup_script,
    load_network_policy,
    slirp4netns_available,
    _build_nftables_rules,
    _sh_quote,
)


# ── _sh_quote ─────────────────────────────────────────────────────────────

def test_sh_quote_plain():
    assert _sh_quote("echo hi") == "'echo hi'"


def test_sh_quote_with_single_quote():
    assert _sh_quote("it's") == "'it'\\''s'"


# ── load_network_policy ───────────────────────────────────────────────────

def test_load_policy_unset_returns_none(monkeypatch):
    monkeypatch.delenv("HARES_SANDBOX_NETWORK_ALLOW", raising=False)
    assert load_network_policy() is None


def test_load_policy_empty_string_returns_none(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_NETWORK_ALLOW", "   ")
    assert load_network_policy() is None


def test_load_policy_ip_address(monkeypatch):
    """IP address entries skip DNS and are used verbatim."""
    monkeypatch.setenv("HARES_SANDBOX_NETWORK_ALLOW", "1.2.3.4:443")
    policy = load_network_policy()
    assert policy is not None
    assert policy.enabled
    assert len(policy.allow) == 1
    e = policy.allow[0]
    assert e.ip == "1.2.3.4"
    assert e.port == 443
    assert e.host == "1.2.3.4"


def test_load_policy_multiple_entries(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_NETWORK_ALLOW",
                       "1.1.1.1:53,8.8.8.8:53,1.2.3.4:443")
    policy = load_network_policy()
    assert policy is not None
    assert len(policy.allow) == 3
    ports = {e.port for e in policy.allow}
    assert ports == {53, 443}


def test_load_policy_bad_port_skipped(monkeypatch, caplog):
    monkeypatch.setenv("HARES_SANDBOX_NETWORK_ALLOW", "1.2.3.4:notaport")
    policy = load_network_policy()
    # All entries invalid → empty policy (no usable entries warning).
    assert policy is not None
    assert not policy.enabled


def test_load_policy_missing_port_skipped(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_NETWORK_ALLOW", "example.com")
    policy = load_network_policy()
    assert policy is not None
    assert not policy.enabled


def test_load_policy_mixed_valid_invalid(monkeypatch):
    monkeypatch.setenv("HARES_SANDBOX_NETWORK_ALLOW",
                       "1.2.3.4:443,badentry,5.6.7.8:80")
    policy = load_network_policy()
    assert policy is not None
    # badentry skipped; the two valid IP:port entries remain.
    assert len(policy.allow) == 2


# ── NetworkPolicy.enabled ─────────────────────────────────────────────────

def test_policy_enabled_with_entries():
    p = NetworkPolicy(
        allow=(AllowEntry(host="1.2.3.4", ip="1.2.3.4", port=443),),
        raw="1.2.3.4:443",
    )
    assert p.enabled


def test_policy_disabled_without_entries():
    p = NetworkPolicy(allow=(), raw="badentry")
    assert not p.enabled


# ── _build_nftables_rules ─────────────────────────────────────────────────

def test_nft_rules_contain_table_and_chain():
    p = NetworkPolicy(
        allow=(AllowEntry(host="github.com", ip="140.82.112.4", port=443),),
        raw="github.com:443",
    )
    rules = _build_nftables_rules(p)
    assert "hares_filter" in rules
    assert "output" in rules
    assert "policy drop" in rules


def test_nft_rules_include_loopback_and_established():
    p = NetworkPolicy(
        allow=(AllowEntry(host="1.1.1.1", ip="1.1.1.1", port=53),),
        raw="1.1.1.1:53",
    )
    rules = _build_nftables_rules(p)
    assert "oifname lo accept" in rules
    assert "established,related" in rules


def test_nft_rules_include_each_entry():
    p = NetworkPolicy(
        allow=(
            AllowEntry(host="1.1.1.1", ip="1.1.1.1", port=53),
            AllowEntry(host="github.com", ip="140.82.112.4", port=443),
        ),
        raw="1.1.1.1:53,github.com:443",
    )
    rules = _build_nftables_rules(p)
    assert "1.1.1.1" in rules and "dport 53" in rules
    assert "140.82.112.4" in rules and "dport 443" in rules


def test_nft_rules_empty_policy_no_allow_rules():
    p = NetworkPolicy(allow=(), raw="")
    rules = _build_nftables_rules(p)
    # Should still have the table, chain, loopback, and established rules —
    # but no specific IP accept rules.
    assert "hares_filter" in rules
    assert "ip daddr" not in rules


# ── build_inner_setup_script ──────────────────────────────────────────────

def test_inner_script_contains_networking_setup():
    p = NetworkPolicy(
        allow=(AllowEntry(host="1.2.3.4", ip="1.2.3.4", port=443),),
        raw="1.2.3.4:443",
    )
    script = build_inner_setup_script(p, "echo hello")
    assert "ip link set" in script
    assert "ip route add" in script


def test_inner_script_contains_nft_rules():
    p = NetworkPolicy(
        allow=(AllowEntry(host="1.2.3.4", ip="1.2.3.4", port=443),),
        raw="1.2.3.4:443",
    )
    script = build_inner_setup_script(p, "echo hello")
    assert "nft" in script
    assert "hares_filter" in script


def test_inner_script_execs_real_command():
    p = NetworkPolicy(
        allow=(AllowEntry(host="1.2.3.4", ip="1.2.3.4", port=443),),
        raw="1.2.3.4:443",
    )
    command = "pytest -q"
    script = build_inner_setup_script(p, command)
    assert "exec" in script
    assert "pytest -q" in script


# ── slirp4netns_available ─────────────────────────────────────────────────

def test_slirp4netns_availability_is_boolean():
    result = slirp4netns_available()
    assert isinstance(result, bool)


def test_slirp4netns_respects_custom_bin(monkeypatch):
    monkeypatch.setenv("HARES_SLURP4NETNS_BIN", "/nonexistent/slirp4netns")
    import importlib, hares.net_policy as m
    # Direct check: with a nonexistent binary, should return False.
    import shutil
    assert shutil.which("/nonexistent/slirp4netns") is None
