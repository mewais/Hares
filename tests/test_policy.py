"""Tests for hares.policy — command policy engine.

Tests the deny list, suspicious list, pattern matching, and the
load_policy factory. Elicitation itself (the MCP network call) is
tested via mocking; no real Claude Code connection needed.
"""

from __future__ import annotations

import pytest

from hares.policy import (
    Decision,
    PolicyEngine,
    PolicyResult,
    _normalise,
    _parse_patterns,
    elicit_approval,
    elicit_memory_approval,
    load_policy,
    DEFAULT_SUSPECT_PATTERNS,
)


# ── _normalise ────────────────────────────────────────────────────────────────

def test_normalise_no_wildcards_wraps():
    assert _normalise("git push") == "*git push*"


def test_normalise_existing_wildcard_unchanged():
    assert _normalise("*git push*") == "*git push*"
    assert _normalise("git push *") == "git push *"


# ── _parse_patterns ───────────────────────────────────────────────────────────

def test_parse_patterns_splits_on_comma():
    assert _parse_patterns("a,b,c") == ("a", "b", "c")


def test_parse_patterns_strips_whitespace():
    assert _parse_patterns("a , b , c") == ("a", "b", "c")


def test_parse_patterns_skips_empty():
    assert _parse_patterns("a,,b") == ("a", "b")


# ── PolicyEngine.check — ALLOW ────────────────────────────────────────────────

def test_allow_when_no_patterns():
    engine = PolicyEngine(deny_patterns=(), suspect_patterns=())
    r = engine.check("ls -la")
    assert r.decision is Decision.ALLOW


def test_allow_unmatched_command():
    engine = PolicyEngine(
        deny_patterns=("sudo *",),
        suspect_patterns=("*git push*",),
    )
    r = engine.check("pytest -q tests/")
    assert r.decision is Decision.ALLOW


# ── PolicyEngine.check — DENY ────────────────────────────────────────────────

def test_deny_exact_pattern():
    engine = PolicyEngine(deny_patterns=("sudo *",), suspect_patterns=())
    r = engine.check("sudo rm -rf /")
    assert r.decision is Decision.DENY
    assert r.tier == "deny"
    assert "sudo *" in r.matched_pattern


def test_deny_substring_pattern_no_wildcards():
    engine = PolicyEngine(deny_patterns=("rm -rf /",), suspect_patterns=())
    r = engine.check("rm -rf /")
    assert r.decision is Decision.DENY


def test_deny_takes_priority_over_suspect():
    engine = PolicyEngine(
        deny_patterns=("*git push --force*",),
        suspect_patterns=("*git push*",),
    )
    r = engine.check("git push --force origin main")
    assert r.decision is Decision.DENY


def test_deny_message_mentions_pattern():
    engine = PolicyEngine(deny_patterns=("sudo *",), suspect_patterns=())
    r = engine.check("sudo something")
    assert "sudo *" in r.message or "sudo" in r.message


# ── PolicyEngine.check — ELICIT ───────────────────────────────────────────────

def test_elicit_matching_suspect_pattern():
    engine = PolicyEngine(deny_patterns=(), suspect_patterns=("*git push*",))
    r = engine.check("git push origin main")
    assert r.decision is Decision.ELICIT
    assert r.tier == "suspect"


def test_elicit_substring_match():
    engine = PolicyEngine(deny_patterns=(), suspect_patterns=("-X POST",))
    r = engine.check("curl https://api.example.com -X POST -d '{}'")
    assert r.decision is Decision.ELICIT


def test_elicit_no_match_on_unrelated():
    engine = PolicyEngine(deny_patterns=(), suspect_patterns=("*git push*",))
    r = engine.check("git status")
    assert r.decision is Decision.ALLOW


# ── Default suspicious patterns ───────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "git push origin main",
    "git push --force origin main",
    "git push -f main",
    "curl https://example.com -X POST -d '{}' ",
    "curl -X PUT https://api.example.com/resource",
    "curl -X DELETE https://api.example.com/resource",
    "curl --request POST https://example.com",
    "gh pr create --title 'Test PR'",
    "gh pr merge 42",
    "gh release create v1.0.0",
    "npm publish",
    "twine upload dist/*",
])
def test_default_suspect_catches_remote_writes(command):
    engine = PolicyEngine()  # uses defaults
    r = engine.check(command)
    assert r.decision is Decision.ELICIT, (
        f"Expected ELICIT for {command!r}, got {r.decision}"
    )


@pytest.mark.parametrize("command", [
    "git status",
    "git log --oneline -10",
    "git diff HEAD",
    "git fetch origin",    # fetch is read-only locally
    "ls -la",
    "cat README.md",
    "grep -r 'TODO' src/",
    "pytest -q tests/",
    "make build",
    "curl https://example.com",     # GET (no -X) is fine
    "curl -X GET https://example.com",  # explicit GET is fine
])
def test_default_suspect_allows_read_ops(command):
    engine = PolicyEngine()
    r = engine.check(command)
    assert r.decision is Decision.ALLOW, (
        f"Expected ALLOW for {command!r}, got {r.decision} "
        f"(matched: {r.matched_pattern!r})"
    )


# ── active property ───────────────────────────────────────────────────────────

def test_active_false_when_both_empty():
    engine = PolicyEngine(deny_patterns=(), suspect_patterns=())
    assert not engine.active


def test_active_true_with_deny():
    engine = PolicyEngine(deny_patterns=("sudo *",), suspect_patterns=())
    assert engine.active


def test_active_true_with_suspect():
    engine = PolicyEngine(deny_patterns=(), suspect_patterns=("*git push*",))
    assert engine.active


# ── load_policy ───────────────────────────────────────────────────────────────

def test_load_policy_both_none_returns_engine_with_defaults():
    engine = load_policy(deny_arg=None, suspect_arg=None)
    # Defaults are non-empty so we get an engine
    assert engine is not None
    assert engine.active


def test_load_policy_explicit_deny():
    engine = load_policy(deny_arg="sudo *", suspect_arg="")
    assert engine is not None
    r = engine.check("sudo rm -rf /")
    assert r.decision is Decision.DENY


def test_load_policy_empty_suspect_disables_tier():
    engine = load_policy(deny_arg=None, suspect_arg="")
    # Empty suspect arg → no suspect patterns
    r = engine.check("git push origin main")
    assert r.decision is Decision.ALLOW


def test_load_policy_explicit_suspect():
    engine = load_policy(deny_arg=None, suspect_arg="*deploy*")
    r = engine.check("./deploy.sh production")
    assert r.decision is Decision.ELICIT


# ── elicit_approval ───────────────────────────────────────────────────────────

class _MockSession:
    def __init__(self, action: str):
        self._action = action

    async def elicit(self, message: str, requestedSchema: dict, **_):
        class R:
            pass
        r = R()
        r.action = self._action
        return r


@pytest.mark.asyncio
async def test_elicit_approval_accept():
    session = _MockSession("accept")
    engine = PolicyEngine(deny_patterns=(), suspect_patterns=("*git push*",))
    pr = engine.check("git push origin main")
    approved = await elicit_approval(session, "git push origin main", pr)
    assert approved is True


@pytest.mark.asyncio
async def test_elicit_approval_decline():
    session = _MockSession("decline")
    engine = PolicyEngine(deny_patterns=(), suspect_patterns=("*git push*",))
    pr = engine.check("git push origin main")
    approved = await elicit_approval(session, "git push origin main", pr)
    assert approved is False


@pytest.mark.asyncio
async def test_elicit_approval_cancel():
    session = _MockSession("cancel")
    engine = PolicyEngine(deny_patterns=(), suspect_patterns=("*git push*",))
    pr = engine.check("git push origin main")
    approved = await elicit_approval(session, "git push origin main", pr)
    assert approved is False


@pytest.mark.asyncio
async def test_elicit_approval_no_elicitation_support_fails_closed():
    """Client without elicitation support → denied (fail closed)."""
    class NoElicitSession:
        async def create_elicitation(self, **_):
            raise AttributeError("no elicitation")

    engine = PolicyEngine(deny_patterns=(), suspect_patterns=("*git push*",))
    pr = engine.check("git push origin main")
    approved = await elicit_approval(NoElicitSession(), "git push origin main", pr)
    assert approved is False


@pytest.mark.asyncio
async def test_elicit_approval_none_session_fails_closed():
    """None session (no MCP context) → denied."""
    engine = PolicyEngine(deny_patterns=(), suspect_patterns=("*git push*",))
    pr = engine.check("git push origin main")
    approved = await elicit_approval(None, "git push origin main", pr)
    assert approved is False


# ── elicit_memory_approval ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_elicit_memory_approval_accept():
    """Mock session that accepts → approved."""
    session = _MockSession("accept")
    approved = await elicit_memory_approval(
        session, "make -j8", requested_mb=16384, normal_cap_mb=7168,
    )
    assert approved is True


@pytest.mark.asyncio
async def test_elicit_memory_approval_decline():
    """Mock session that declines → not approved."""
    session = _MockSession("decline")
    approved = await elicit_memory_approval(
        session, "make -j8", requested_mb=16384, normal_cap_mb=7168,
    )
    assert approved is False


@pytest.mark.asyncio
async def test_elicit_memory_approval_cancel():
    """Cancel action is treated as not approved."""
    session = _MockSession("cancel")
    approved = await elicit_memory_approval(
        session, "make -j8", requested_mb=16384, normal_cap_mb=7168,
    )
    assert approved is False


@pytest.mark.asyncio
async def test_elicit_memory_approval_no_elicitation_support_fails_closed():
    """Client without elicitation method → denied (fail closed)."""
    class NoElicitSession:
        async def create_elicitation(self, **_):
            raise AttributeError("no elicitation")

    approved = await elicit_memory_approval(
        NoElicitSession(), "make -j8", requested_mb=16384, normal_cap_mb=7168,
    )
    assert approved is False


@pytest.mark.asyncio
async def test_elicit_memory_approval_none_session_fails_closed():
    """None session → denied."""
    approved = await elicit_memory_approval(
        None, "make -j8", requested_mb=16384, normal_cap_mb=7168,
    )
    assert approved is False


@pytest.mark.asyncio
async def test_elicit_memory_approval_message_mentions_budgets():
    """The elicitation message must mention both the requested and normal cap."""
    captured_message: list[str] = []

    class CapturingSession:
        async def elicit(self, message: str, requestedSchema: dict, **_):
            captured_message.append(message)
            class R:
                action = "decline"
            return R()

    await elicit_memory_approval(
        CapturingSession(), "make -j8",
        requested_mb=16384, normal_cap_mb=7168,
    )
    assert captured_message, "elicit() was not called"
    msg = captured_message[0]
    assert "16384" in msg, f"requested_mb not in message: {msg!r}"
    assert "7168" in msg, f"normal_cap_mb not in message: {msg!r}"
