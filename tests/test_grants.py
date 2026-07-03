"""Unit tests for hares.grants (GrantStore), the
hares.path_safety.validate_grant_target deny check, and the
hares.policy.elicit_path_access_approval elicitation helper.

These are pure unit tests — no MCP subprocess involved. Protocol-level
behavior (the request_path_access tool end to end, enforcement inside
fs operations / the shell Runner) is covered in
test_request_path_access_proto.py, test_fs_operations.py, and
test_runner.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hares.grants import Grant, GrantStore
from hares.path_safety import (
    DenyLists,
    PathDeniedError,
    PathSafetyError,
    validate_grant_target,
)
from hares.policy import elicit_path_access_approval


# ── GrantStore ──────────────────────────────────────────────────────────────


def test_add_returns_grant_and_lists_active(tmp_path):
    store = GrantStore()
    root = tmp_path / "outside"
    root.mkdir()
    g = store.add(root, "ro", "session")
    assert isinstance(g, Grant)
    assert g.root == root and g.mode == "ro" and g.lifetime == "session"
    active = store.list_active()
    assert active == [{"path": str(root), "mode": "ro", "lifetime": "session"}]


def test_ro_grant_covers_read_not_write(tmp_path):
    root = tmp_path / "outside"; root.mkdir()
    store = GrantStore()
    store.add(root, "ro", "session")
    assert store.covers_read(root / "file.txt") is True
    assert store.covers_write(root / "file.txt") is False


def test_rw_grant_covers_both(tmp_path):
    root = tmp_path / "outside"; root.mkdir()
    store = GrantStore()
    store.add(root, "rw", "session")
    assert store.covers_read(root / "sub" / "file.txt") is True
    assert store.covers_write(root / "sub" / "file.txt") is True


def test_grant_does_not_cover_unrelated_path(tmp_path):
    root = tmp_path / "outside"; root.mkdir()
    other = tmp_path / "other"; other.mkdir()
    store = GrantStore()
    store.add(root, "rw", "session")
    assert store.covers_read(other / "f.txt") is False
    assert store.covers_write(other / "f.txt") is False


def test_directory_grant_covers_entire_subtree(tmp_path):
    root = tmp_path / "outside"; root.mkdir()
    nested = root / "a" / "b" / "c.txt"
    store = GrantStore()
    store.add(root, "rw", "session")
    assert store.covers_read(nested) is True
    assert store.covers_write(nested) is True


def test_grant_covers_exact_root_itself(tmp_path):
    root = tmp_path / "outside"; root.mkdir()
    store = GrantStore()
    store.add(root, "ro", "session")
    assert store.covers_read(root) is True


def test_consume_once_removes_once_grant(tmp_path):
    root = tmp_path / "outside"; root.mkdir()
    store = GrantStore()
    store.add(root, "ro", "once")
    assert store.covers_read(root) is True
    store.consume_once(root, need_write=False)
    assert store.covers_read(root) is False
    assert store.list_active() == []


def test_consume_once_leaves_session_grant(tmp_path):
    root = tmp_path / "outside"; root.mkdir()
    store = GrantStore()
    store.add(root, "ro", "session")
    store.consume_once(root, need_write=False)
    assert store.covers_read(root) is True
    assert len(store.list_active()) == 1


def test_consume_once_noop_when_not_covered(tmp_path):
    root = tmp_path / "outside"; root.mkdir()
    other = tmp_path / "other"; other.mkdir()
    store = GrantStore()
    store.add(root, "ro", "once")
    store.consume_once(other, need_write=False)  # unrelated path — no-op
    assert store.covers_read(root) is True


def test_consume_all_once_removes_all_once_grants_only(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    c = tmp_path / "c"; c.mkdir()
    store = GrantStore()
    store.add(a, "ro", "once")
    store.add(b, "rw", "once")
    store.add(c, "rw", "session")
    store.consume_all_once()
    active = store.list_active()
    assert len(active) == 1
    assert active[0]["path"] == str(c)


def test_multiple_grants_first_covering_one_wins(tmp_path):
    root = tmp_path / "outside"; root.mkdir()
    store = GrantStore()
    store.add(root, "ro", "session")
    store.add(root, "rw", "once")
    # covers_write should find the rw one regardless of insertion order
    # relative to the ro one.
    assert store.covers_write(root / "f") is True


# ── validate_grant_target ────────────────────────────────────────────────


def test_rejects_dot_git_segment(tmp_path):
    bad = tmp_path / ".git" / "objects"
    bad.mkdir(parents=True)
    with pytest.raises(PathSafetyError, match=".git"):
        validate_grant_target(bad)


def test_rejects_dot_git_mid_path(tmp_path):
    bad = tmp_path / "repo" / ".git" / "hooks"
    bad.mkdir(parents=True)
    with pytest.raises(PathSafetyError, match=".git"):
        validate_grant_target(bad)


def test_accepts_normal_path_outside_ceiling(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    validate_grant_target(outside)  # must not raise


def test_rejects_system_dir_when_strict(monkeypatch):
    monkeypatch.setenv("HARES_DISALLOW_SYSTEM_DIRS", "1")
    with pytest.raises(PathSafetyError, match="system directory"):
        validate_grant_target(Path("/etc"))


def test_system_dir_allowed_when_not_strict(monkeypatch):
    monkeypatch.delenv("HARES_DISALLOW_SYSTEM_DIRS", raising=False)
    validate_grant_target(Path("/etc"))  # must not raise


def test_rejects_excluded_path_inside_ceiling(tmp_path):
    ceiling = tmp_path
    excluded = tmp_path / "secrets"; excluded.mkdir()
    deny = DenyLists(exclude=(excluded,), protect=())
    target = excluded / "api_key.txt"
    with pytest.raises(PathDeniedError):
        validate_grant_target(target, mode="ro", ceiling=ceiling, deny=deny)


def test_rejects_protected_path_inside_ceiling_for_rw(tmp_path):
    ceiling = tmp_path
    protected = tmp_path / "vendor"; protected.mkdir()
    deny = DenyLists(exclude=(), protect=(protected,))
    target = protected / "lib.py"
    with pytest.raises(PathDeniedError):
        validate_grant_target(target, mode="rw", ceiling=ceiling, deny=deny)


def test_protected_path_inside_ceiling_allowed_for_ro(tmp_path):
    """Protect is write-only — a read-mode grant target under a
    protected path is fine (mirrors validate_path_not_protected's own
    write-only semantics elsewhere in the codebase)."""
    ceiling = tmp_path
    protected = tmp_path / "vendor"; protected.mkdir()
    deny = DenyLists(exclude=(), protect=(protected,))
    target = protected / "lib.py"
    validate_grant_target(target, mode="ro", ceiling=ceiling, deny=deny)  # no raise


def test_exclude_protect_ignored_when_target_outside_ceiling(tmp_path):
    """A grant target genuinely outside the ceiling isn't affected by
    in-ceiling exclude/protect lists (they can't apply there)."""
    ceiling = tmp_path / "proj"; ceiling.mkdir()
    excluded = ceiling / "secrets"; excluded.mkdir()
    deny = DenyLists(exclude=(excluded,), protect=())
    outside = tmp_path / "elsewhere"; outside.mkdir()
    validate_grant_target(outside, mode="rw", ceiling=ceiling, deny=deny)  # no raise


# ── elicit_path_access_approval ──────────────────────────────────────────


class _MockSession:
    def __init__(self, action: str, content: dict | None = None):
        self._action = action
        self._content = content

    async def elicit(self, message: str, requestedSchema: dict, **_):
        class R:
            pass
        r = R()
        r.action = self._action
        r.content = self._content
        return r


@pytest.mark.asyncio
async def test_accept_once_grant():
    session = _MockSession("accept", {"grant": "once"})
    result = await elicit_path_access_approval(
        session, "/some/path", "ro", "need to read a config file",
    )
    assert result == "once"


@pytest.mark.asyncio
async def test_accept_session_grant():
    session = _MockSession("accept", {"grant": "session"})
    result = await elicit_path_access_approval(
        session, "/some/path", "rw", "need ongoing write access",
    )
    assert result == "session"


@pytest.mark.asyncio
async def test_accept_with_in_dialog_deny():
    """The dialog's own 'grant' enum includes a 'deny' option — even
    though the MCP action is 'accept' (the form was submitted), a
    grant='deny' choice must be treated as denied."""
    session = _MockSession("accept", {"grant": "deny"})
    result = await elicit_path_access_approval(
        session, "/some/path", "ro", "reason",
    )
    assert result is None


@pytest.mark.asyncio
async def test_lax_client_accept_with_missing_content_allows_once():
    """Per MCP spec, requestedSchema compliance is a SHOULD not a MUST.
    A client that returns accept with no content at all must be
    treated as the minimum-privilege allow-once."""
    session = _MockSession("accept", None)
    result = await elicit_path_access_approval(
        session, "/some/path", "ro", "reason",
    )
    assert result == "once"


@pytest.mark.asyncio
async def test_lax_client_accept_with_unrecognized_grant_value_allows_once():
    session = _MockSession("accept", {"grant": "forever"})
    result = await elicit_path_access_approval(
        session, "/some/path", "ro", "reason",
    )
    assert result == "once"


@pytest.mark.asyncio
async def test_lax_client_accept_with_empty_dict_allows_once():
    session = _MockSession("accept", {})
    result = await elicit_path_access_approval(
        session, "/some/path", "ro", "reason",
    )
    assert result == "once"


@pytest.mark.asyncio
async def test_decline_denies():
    session = _MockSession("decline")
    result = await elicit_path_access_approval(
        session, "/some/path", "ro", "reason",
    )
    assert result is None


@pytest.mark.asyncio
async def test_cancel_denies():
    session = _MockSession("cancel")
    result = await elicit_path_access_approval(
        session, "/some/path", "ro", "reason",
    )
    assert result is None


@pytest.mark.asyncio
async def test_no_elicitation_support_fails_closed():
    class NoElicitSession:
        async def create_elicitation(self, **_):
            raise AttributeError("no elicitation")

    result = await elicit_path_access_approval(
        NoElicitSession(), "/some/path", "ro", "reason",
    )
    assert result is None


@pytest.mark.asyncio
async def test_none_session_fails_closed():
    result = await elicit_path_access_approval(
        None, "/some/path", "ro", "reason",
    )
    assert result is None


@pytest.mark.asyncio
async def test_message_shows_resolved_path_mode_and_outside_warning():
    captured: list[str] = []

    class CapturingSession:
        async def elicit(self, message: str, requestedSchema: dict, **_):
            captured.append(message)
            class R:
                action = "decline"
                content = None
            return R()

    await elicit_path_access_approval(
        CapturingSession(), "/resolved/target/path", "rw",
        "I need to write build artifacts here",
    )
    assert captured, "elicit() was not called"
    msg = captured[0]
    assert "/resolved/target/path" in msg
    assert "rw" in msg
    assert "OUTSIDE" in msg
    # Agent's reason must be present but come AFTER the hard facts
    # (path/mode/warning) — visually subordinate, per the design.
    reason_idx = msg.index("I need to write build artifacts here")
    path_idx = msg.index("/resolved/target/path")
    mode_idx = msg.index("rw")
    warning_idx = msg.index("OUTSIDE")
    assert reason_idx > path_idx
    assert reason_idx > mode_idx
    assert reason_idx > warning_idx


@pytest.mark.asyncio
async def test_message_uses_enum_schema_with_grant_field():
    captured_schema: list[dict] = []

    class CapturingSession:
        async def elicit(self, message: str, requestedSchema: dict, **_):
            captured_schema.append(requestedSchema)
            class R:
                action = "decline"
                content = None
            return R()

    await elicit_path_access_approval(
        CapturingSession(), "/p", "ro", "reason",
    )
    assert captured_schema
    schema = captured_schema[0]
    assert schema["type"] == "object"
    assert "grant" in schema["properties"]
    assert set(schema["properties"]["grant"]["enum"]) == {"once", "session", "deny"}
    assert schema["required"] == ["grant"]
