"""Cross-process coordination smoke test.

Spawns 5 simultaneous ``hares-mcp --enable=shell`` processes sharing
one ``HARES_COORDINATION_DIR``. Each is asked to run a sleep-like
command. With ``HARES_MAX_CONCURRENT=2``, the global flock-based cap
must limit total concurrent subprocesses across all 5 processes to 2
— not 5×2=10.

This is the load-bearing property of cross-process coordination:
deploying 5 Hares under an orchestrator without it would multiply the
resource budget by 5.

No external dependencies required — the flock mechanism is built into
the kernel and is crash-safe by construction.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ._proto_helpers import hares_session, parse_text_result


@pytest.mark.asyncio
async def test_global_concurrency_cap_holds_across_5_processes(tmp_path):
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()

    cap = 2
    n_processes = 5
    sleep_seconds = 1.5

    common_env = {
        "HARES_SANDBOX_DISABLED": "1",
        "HARES_COORDINATION_DIR": str(coord_dir),
        "HARES_MAX_CONCURRENT": str(cap),
    }

    async def fire_one_command(ceiling: Path):
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
            pytest.fail(
                f"hares process {i} did not write timeline marker; "
                f"result was: {results[i]}"
            )
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
    # Sanity: all 5 commands actually ran.
    assert sum(1 for _, k, _ in events if k == "START") == n_processes
