"""Minimal example of using hares.cluster.slurm.SlurmExecutor directly.

Demonstrates the three patterns: single blocking submission, batch
submit + wait, and cancel. The executor wraps sbatch / squeue /
scancel — no MCP layer, so this works in test runners, CI scripts,
or any framework code that wants to drive SLURM from Python.

Requires: sbatch / squeue / scancel on PATH (set HARES_SLURM_*_BIN
to override). The script prints a configuration-info line at the
top so you can sanity-check what it would do without actually
submitting if you don't have a cluster handy.

For LSF, swap the imports:

    from hares.cluster.lsf import LsfExecutor, load_lsf_config
"""

from __future__ import annotations

import asyncio

from hares.cluster import JobSpec
from hares.cluster.slurm import SlurmExecutor, load_slurm_config


async def main() -> None:
    cfg = load_slurm_config()
    print(
        f"SLURM config: partition={cfg.partition} "
        f"sbatch={cfg.sbatch_bin} default_timeout={cfg.default_timeout_sec}s"
    )
    executor = SlurmExecutor(cfg=cfg)

    # 1. Single blocking submission. Right-sized resource_spec so
    # this small job clears the queue fast — over-allocated jobs wait
    # for matching nodes regardless of cluster load.
    print("\nSingle blocking job:")
    result = await executor.execute_blocking(
        JobSpec(
            command="echo 'hello from $(hostname)' && date",
            resource_spec="--mem=512 --time=00:05:00",
        ),
        timeout_sec=300,
    )
    print(f"  status={result['status']} exit={result['exit_code']}")
    print(f"  stdout={result['stdout']!r}")

    # 2. Batch parallel submit. Three independent jobs run on the
    # cluster concurrently; we wait for all of them at once.
    print("\nBatch parallel submit (3 jobs):")
    submitted = await executor.submit([
        JobSpec(command=f"echo job-{i} && sleep 2",
                resource_spec="--mem=256 --time=00:05:00")
        for i in range(3)
    ])
    job_ids = [s["job_id"] for s in submitted if s.get("job_id")]
    print(f"  submitted: {job_ids}")
    results = await executor.wait(job_ids, timeout_sec=600)
    for jid, r in results.items():
        print(f"  {jid}: status={r['status']} exit={r.get('exit_code')}")

    # 3. Cancel — useful in cleanup paths or when an external signal
    # tells the orchestrator to abort.
    print("\nCancel demo (submit one, immediately kill):")
    submitted = await executor.submit([
        JobSpec(command="sleep 600", resource_spec="--mem=256 --time=00:15:00"),
    ])
    if submitted[0].get("job_id"):
        jid = submitted[0]["job_id"]
        cancel_result = await executor.cancel([jid])
        print(f"  {jid}: cancelled={cancel_result[jid]['cancelled']}")


if __name__ == "__main__":
    asyncio.run(main())
