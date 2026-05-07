"""Cross-process coordination smoke test.

Spawns 5 simultaneous ``hares-mcp --enable=shell`` processes sharing
one ``HARES_COORDINATION_DIR``. Each is asked to run a sleep-like
command. With ``HARES_MAX_CONCURRENT=2``, the GLOBAL semaphore must
cap the total number of concurrent subprocesses across all 5 hares
processes at 2 — not 5×2=10.

This is the load-bearing property added in 0.2: cross-process
throttling via POSIX named semaphore. Without it, deploying 5 Hares
under any orchestrator would multiply the resource budget by 5.

Skipped when ``posix_ipc`` isn't installed (the in-process fallback
makes the global cap impossible).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from ._proto_helpers import hares_session, parse_text_result


posix_ipc = pytest.importorskip(
    "posix_ipc",
    reason="posix_ipc not installed — cross-process semaphore unavailable",
)


@pytest.mark.asyncio
async def test_global_concurrency_cap_holds_across_5_processes(tmp_path):
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    # Pre-clean any stale semaphore from a prior failed run.
    try:
        from hares.coordination import _semaphore_name
        try:
            posix_ipc.unlink_semaphore(_semaphore_name(coord_dir))
        except posix_ipc.ExistentialError:
            pass
    except ImportError:
        pass

    cap = 2
    n_processes = 5
    sleep_seconds = 1.5

    common_env = {
        "HARES_SANDBOX_DISABLED": "1",
        "HARES_COORDINATION_DIR": str(coord_dir),
        "HARES_MAX_CONCURRENT": str(cap),
    }

    async def fire_one_command(ceiling: Path):
        # Own session per task — anyio cancel scopes must be exited
        # from the same task that entered them.
        async with hares_session(
            enable="shell", ceiling=ceiling, extra_env=common_env,
        ) as s:
            marker = ceiling / "timeline.log"
            cmd = (
                f"echo \"START $(date +%s.%N)\" >> {marker}; "
                f"sleep {sleep_seconds}; "
                f"echo \"END   $(date +%s.%N)\" >> {marker}"
            )
            result = await s.call_tool(
                "execute_command", {"command": cmd, "timeout": 30},
            )
            return parse_text_result(result)

    ceilings = []
    for i in range(n_processes):
        c = tmp_path / f"hares_{i}"
        c.mkdir()
        ceilings.append(c)

    results = await asyncio.gather(*[fire_one_command(c) for c in ceilings])

    # Reconstruct the timeline from the per-process markers.
    events: list[tuple[float, str, int]] = []  # (ts, START|END, idx)
    for i, c in enumerate(ceilings):
        marker = c / "timeline.log"
        if not marker.exists():
            pytest.fail(f"hares process {i} did not write timeline marker; result was: {results[i]}")
        for line in marker.read_text().splitlines():
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            kind, ts = parts
            events.append((float(ts), kind, i))
    events.sort()

    # Walk events and track in-flight count; assert never exceeds cap.
    in_flight = 0
    max_in_flight = 0
    for _ts, kind, _i in events:
        if kind == "START":
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        elif kind == "END":
            in_flight -= 1

    assert max_in_flight <= cap, (
        f"global concurrency cap violated: max_in_flight={max_in_flight}, "
        f"cap={cap}, events={events}"
    )
    # Sanity: we DID actually run all 5 (not 0 due to early failure).
    assert sum(1 for _, k, _ in events if k == "START") == n_processes

    # Cleanup the named semaphore so re-runs start clean.
    try:
        from hares.coordination import _semaphore_name
        try:
            posix_ipc.unlink_semaphore(_semaphore_name(coord_dir))
        except posix_ipc.ExistentialError:
            pass
    except ImportError:
        pass
