"""Minimal example of using hares.runner.Runner directly.

Skips the MCP layer entirely — useful when you want kernel-enforced
caps inside a test runner, CI script, or framework code that doesn't
talk to an LLM. The Runner uses the same engine as the MCP server,
so the caps and the bwrap sandbox apply identically.

Run:

    python examples/python-library/runner_basic.py

To share a single concurrency cap across this script AND any
hares-mcp processes running in parallel, set
``HARES_COORDINATION_DIR=/some/per-run/dir`` in the env before
launching either.
"""

from __future__ import annotations

import asyncio
import os

from hares.runner import Runner
from hares.sandbox import load_sandbox_config


async def main() -> None:
    runner = Runner(
        max_concurrent=2,
        mem_limit_mb=4096,
        cpu_limit_sec=300,
        # Reads HARES_SANDBOX_* env vars; pass sandbox=None to skip
        # bwrap entirely while keeping the RLIMIT + concurrency caps.
        sandbox=load_sandbox_config(default_cwd=os.getcwd()),
    )

    # 1. Vanilla command.
    r = await runner.execute("echo hello", timeout=5)
    print("echo:", r["exit_code"], repr(r["stdout"]))

    # 2. Per-call resource override (clamps DOWN to operator default —
    # callers can ask for less, never more).
    r = await runner.execute(
        "ls -la /tmp", timeout=5,
        mem_limit_mb=128, cpu_limit_sec=10,
    )
    print("ls (right-sized):", r["exit_code"], "stdout=", len(r["stdout"]), "bytes")

    # 3. stdin support — pipe input without /bin/sh -c gymnastics.
    r = await runner.execute("cat", stdin="payload from caller\n")
    print("cat:", r["exit_code"], repr(r["stdout"]))

    # 4. Timeout / kill reasons surface in the result.
    r = await runner.execute("sleep 10", timeout=1.0)
    print("sleep:", r["exit_code"], "killed_reason=", r["killed_reason"])


if __name__ == "__main__":
    asyncio.run(main())
