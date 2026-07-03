# Project guidance for Claude

## Shell execution

Native `Bash` is denied. Use `mcp__hares__hares_execute_command` for
all shell work — it runs your command under a kernel-enforced bwrap
sandbox plus `RLIMIT_AS` / `RLIMIT_CPU` caps, and shares one global
concurrency budget with any other Claude session running in parallel
on this box.

Right-size resource asks per call when you can:

- Inspection commands (`ls`, `cat`, `grep`, `git status`):
  `mem_limit_mb=256 cpu_limit_sec=30`
- Test runs / small builds:
  `mem_limit_mb=4096 cpu_limit_sec=600`
- Heavy builds / simulators:
  default (omit the override; the operator's `HARES_MEM_LIMIT_MB`
  applies)

A tighter per-call cap means the kernel kill fires earlier if a
command unexpectedly grows — better debugging signal than letting it
consume the global default.

## HPC jobs

For LSF / SLURM cluster work, use `mcp__hares-slurm__hpc_slurm_*`
(or `mcp__hares-lsf__*` if your cluster uses LSF):

- `hpc_slurm_execute_blocking` — single job, wait for it, get
  stdout/stderr.
- `hpc_slurm_submit` — batch-submit N jobs, get back job_ids; pair
  with `hpc_slurm_wait`.

Right-size the job's `resource_spec` per submission. SLURM and LSF
both prioritize jobs whose resource asks fit current cluster slack —
small asks (e.g. `--mem=512 --time=00:05:00` for a smoke test) start
almost immediately, while over-allocated jobs queue for matching
nodes to free up.

## File operations

Native `Read`, `Write`, `Edit`, `Glob`, `Grep` are unchanged — keep
using them. Only shell is routed through Hares (that's where the
"Claude broke my dev box" risk actually lives).
