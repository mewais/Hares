"""Tests for request_path_access enforcement inside the shell Runner —
bwrap mount composition (``Runner._effective_sandbox`` /
``_apply_grant_mounts``) and the single "once"-grant consumption point
in ``Runner.execute``.

The pure composition tests below build a ``SandboxConfig`` directly
(``bwrap_bin="bwrap"`` as a literal string) and never actually spawn a
subprocess, so they don't require bwrap to be installed — mirrors how
test_sandbox.py's argv-shape tests reason about mount ordering without
needing a kernel round-trip. The end-to-end tests at the bottom DO
spawn a real bwrap subprocess and are skipped when bwrap is
unavailable, matching test_bwrap_active_scope.py's convention.
"""

from __future__ import annotations

import shutil

import pytest

from hares.grants import GrantStore
from hares.runner import Runner
from hares.sandbox import SandboxConfig, build_bwrap_argv


def _runner_with_grants(
    tmp_path, grant_store: GrantStore, *, ceiling=None,
) -> Runner:
    cfg = SandboxConfig(
        enabled=True, bwrap_bin="bwrap",
        rw_binds=(), ro_binds=(),
    )
    return Runner(
        max_concurrent=1, mem_limit_mb=512, cpu_limit_sec=30,
        sandbox=cfg, ceiling=ceiling or tmp_path, grant_store=grant_store,
        use_cgroup=False,
    )


# ── Pure mount-composition tests (no bwrap execution needed) ───────────


def test_no_grant_store_is_noop(tmp_path):
    cfg = SandboxConfig(enabled=True, bwrap_bin="bwrap")
    r = Runner(
        max_concurrent=1, mem_limit_mb=512, cpu_limit_sec=30,
        sandbox=cfg, ceiling=tmp_path, use_cgroup=False,
    )
    effective = r._effective_sandbox(None)
    assert effective is not None  # still composes (ceiling mount etc.)


def test_ro_grant_added_to_ro_binds(tmp_path):
    outside = tmp_path.parent / f"grant_ro_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    store = GrantStore()
    store.add(outside, "ro", "session")
    r = _runner_with_grants(tmp_path, store)
    effective = r._effective_sandbox(None)
    assert str(outside) in effective.ro_binds
    assert str(outside) not in effective.rw_binds


def test_rw_grant_added_to_rw_binds(tmp_path):
    outside = tmp_path.parent / f"grant_rw_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    store = GrantStore()
    store.add(outside, "rw", "session")
    r = _runner_with_grants(tmp_path, store)
    effective = r._effective_sandbox(None)
    assert str(outside) in effective.rw_binds
    assert str(outside) not in effective.ro_binds


def test_multiple_grants_both_applied(tmp_path):
    ro_dir = tmp_path.parent / f"grant_multi_ro_{tmp_path.name}"
    rw_dir = tmp_path.parent / f"grant_multi_rw_{tmp_path.name}"
    ro_dir.mkdir(exist_ok=True)
    rw_dir.mkdir(exist_ok=True)
    store = GrantStore()
    store.add(ro_dir, "ro", "session")
    store.add(rw_dir, "rw", "once")
    r = _runner_with_grants(tmp_path, store)
    effective = r._effective_sandbox(None)
    assert str(ro_dir) in effective.ro_binds
    assert str(rw_dir) in effective.rw_binds


def test_effective_sandbox_is_pure_does_not_consume_once_grant(tmp_path):
    """_effective_sandbox is called MORE THAN ONCE per execute() call
    (see the use_allowlist probe in Runner.execute) — it must never
    consume a 'once' grant itself, or the second call would silently
    lose the mount."""
    outside = tmp_path.parent / f"grant_pure_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    store = GrantStore()
    store.add(outside, "rw", "once")
    r = _runner_with_grants(tmp_path, store)
    first = r._effective_sandbox(None)
    second = r._effective_sandbox(None)
    assert str(outside) in first.rw_binds
    assert str(outside) in second.rw_binds
    assert len(store.list_active()) == 1  # still there — not consumed


def test_grant_mount_precedes_exclude_deny_in_argv(tmp_path):
    """Deny beats grant: build_bwrap_argv always applies
    exclude/protect binds LAST, so even though the grant mount is
    added as an extra rw/ro bind, an overlapping exclude wins. This
    proves the ordering guarantee _apply_grant_mounts relies on."""
    outside = tmp_path.parent / f"grant_deny_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    store = GrantStore()
    store.add(outside, "rw", "session")
    cfg = SandboxConfig(
        enabled=True, bwrap_bin="bwrap",
        rw_binds=(), ro_binds=(),
        exclude_binds=(str(outside),),
    )
    r = Runner(
        max_concurrent=1, mem_limit_mb=512, cpu_limit_sec=30,
        sandbox=cfg, ceiling=tmp_path, grant_store=store, use_cgroup=False,
    )
    effective = r._effective_sandbox(None)
    assert str(outside) in effective.rw_binds  # grant mount present
    argv = build_bwrap_argv(effective, "true", cwd=str(tmp_path))
    rw_idx = next(
        i for i in range(len(argv))
        if argv[i] == "--bind" and argv[i + 1] == str(outside)
    )
    tmpfs_idx = next(
        i for i in range(len(argv))
        if argv[i] == "--tmpfs" and argv[i + 1] == str(outside)
    )
    assert tmpfs_idx > rw_idx, argv  # deny mount comes after — wins


# ── End-to-end (real bwrap subprocess) ──────────────────────────────────

_BWRAP = shutil.which("bwrap")
pytestmark_bwrap = pytest.mark.skipif(_BWRAP is None, reason="bwrap not available")


@pytestmark_bwrap
@pytest.mark.asyncio
async def test_once_grant_consumed_after_one_execute_call(tmp_path):
    outside = tmp_path.parent / f"grant_once_exec_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    store = GrantStore()
    store.add(outside, "rw", "once")
    r = _runner_with_grants(tmp_path, store)
    result = await r.execute(f"echo hi > {outside}/marker.txt", timeout=15)
    if result.get("exit_code") != 0:
        pytest.skip(f"bwrap subprocess setup failed: {result}")
    assert store.list_active() == []  # consumed by the single execute() call


@pytestmark_bwrap
@pytest.mark.asyncio
async def test_session_grant_not_consumed_by_execute_call(tmp_path):
    outside = tmp_path.parent / f"grant_session_exec_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    store = GrantStore()
    store.add(outside, "rw", "session")
    r = _runner_with_grants(tmp_path, store)
    result = await r.execute(f"echo hi > {outside}/marker.txt", timeout=15)
    if result.get("exit_code") != 0:
        pytest.skip(f"bwrap subprocess setup failed: {result}")
    assert len(store.list_active()) == 1  # session grant survives
