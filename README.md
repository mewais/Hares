# Hares (حارس)

A resource-aware command-execution MCP server. *Hares* is Arabic for **guard /
protector**: the server guards the host from runaway subprocesses by enforcing
per-process memory caps, CPU time caps, wall-clock timeouts, a global
concurrency semaphore, and CPU-affinity pinning.

Designed as a drop-in replacement for unconstrained shell tools in any
MCP-aware client (Naseej, claude-code, etc.).

> **Tuning** — every cap is controlled by an env var (`HARES_MAX_CONCURRENT`,
> `HARES_MEM_LIMIT_MB`, `HARES_CPU_LIMIT_SEC`, …). See [Configuration
> (env vars)](#configuration-env-vars) below for the full table and tuning
> examples for big workstations vs small containers.

## What Hares does

For every command it runs:

1. **Pre-flight rewrite** — known overcommit patterns (`pytest -n auto`, bare
   `make -j`, `cargo build --jobs 64`, `ninja -j32`) are clamped to the
   per-command worker budget *before* the subprocess starts. The rewrite is
   reported in stdout so the agent can learn.
2. **CPU affinity** — the subprocess is pinned to a small set of cores
   (default: 1 per `weight` slot). Tools that auto-detect CPU count
   (pytest-xdist, make, ninja, cargo) naturally pick a safe value.
3. **`RLIMIT_AS`** — virtual address space cap. Overruns get `ENOMEM` /
   `SIGSEGV` from the kernel.
4. **`RLIMIT_CPU`** — CPU-seconds cap. Overruns get `SIGXCPU` from the kernel.
5. **psutil RSS monitor** — aggregates RSS across the process tree (catches
   xdist worker sprawl that `RLIMIT_AS` alone misses).
6. **Wall-clock timeout** — `asyncio.wait_for` + `killpg` of the whole
   process group on overrun.
7. **Global semaphore** — only `HARES_MAX_CONCURRENT` commands run at once
   across all callers; extras queue inside the server.

## Installing

```sh
cd /proj/hpc_solutions/mewais/Hares
pip install -e .
```

This installs the `hares-mcp` console script.

## Configuration (env vars)

All defaults are tuned for a 2-core / 16 GB box. Override via env to scale.

| Variable                       | Default | Meaning                                           |
|--------------------------------|---------|---------------------------------------------------|
| `HARES_MAX_CONCURRENT`         | `2`     | Global semaphore size (concurrent commands).      |
| `HARES_MEM_LIMIT_MB`           | `7168`  | Per-subprocess `RLIMIT_AS` in MB (7 GB).          |
| `HARES_CPU_LIMIT_SEC`          | `1200`  | Per-subprocess `RLIMIT_CPU` in seconds (20 min).  |
| `HARES_DEFAULT_TIMEOUT_SEC`    | `300`   | Default per-command wall-clock timeout (sec).     |
| `HARES_RSS_POLL_INTERVAL_SEC`  | `2`     | Seconds between RSS aggregation polls.            |
| `HARES_RSS_OVERSHOOT_RATIO`    | `1.2`   | Kill if process-tree RSS > `MEM_LIMIT × ratio`.   |

A per-call `timeout` argument always wins over `HARES_DEFAULT_TIMEOUT_SEC`.

### Tuning examples

```sh
# Big workstation — 16 cores, 128 GB:
export HARES_MAX_CONCURRENT=8
export HARES_MEM_LIMIT_MB=14336        # 14 GB per command
export HARES_CPU_LIMIT_SEC=3600        # 1 hour

# Tiny container — 1 core, 4 GB:
export HARES_MAX_CONCURRENT=1
export HARES_MEM_LIMIT_MB=2048         # 2 GB
export HARES_CPU_LIMIT_SEC=300

# Stress test — let everything overrun the cap quickly:
export HARES_RSS_OVERSHOOT_RATIO=1.05  # kill at +5% over cap
export HARES_RSS_POLL_INTERVAL_SEC=0.5 # check every 500 ms
```

The same vars can be set in an MCP client's `env:` block (see next section)
so each MCP-aware tool gets a separate Hares instance with its own limits.

## MCP client config

```json
{
  "mcpServers": {
    "shell": {
      "command": "hares-mcp",
      "env": {
        "HARES_MAX_CONCURRENT": "2",
        "HARES_MEM_LIMIT_MB":   "7168",
        "HARES_CPU_LIMIT_SEC":  "1200"
      }
    }
  }
}
```

The single tool exposed is `execute_command(command, cwd?, env?, timeout?, weight?)`.

## Tool schema

```json
{
  "name": "execute_command",
  "inputSchema": {
    "type": "object",
    "required": ["command"],
    "properties": {
      "command": { "type": "string" },
      "cwd":     { "type": "string" },
      "env":     { "type": "object", "additionalProperties": { "type": "string" } },
      "timeout": { "type": "number" },
      "weight":  { "type": "integer", "minimum": 1 }
    }
  }
}
```

Result:

```json
{
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "killed_reason": null,                        // or "timeout" | "rss_exceeded" | "cpu_exceeded"
  "rewrites": [
    {
      "kind": "pytest_xdist",
      "original": "-n auto",
      "replacement": "-n 1",
      "reason": "pytest-xdist requested auto workers; clamped to 1 to fit Hares cap."
    }
  ]
}
```

When `rewrites` is non-empty, the same notice is also prepended to `stdout`
so the LLM agent sees it inline and learns to pick the right value next time.

## When commands get killed

| Cause                                      | `killed_reason`  | What the agent should do                                           |
|--------------------------------------------|------------------|--------------------------------------------------------------------|
| Wall-clock `timeout` exceeded              | `"timeout"`      | Increase `timeout`, or split the work into smaller commands.       |
| Process tree RSS > `MEM_LIMIT × overshoot` | `"rss_exceeded"` | Run fewer parallel workers; pick a narrower test selection.        |
| `SIGXCPU` from `RLIMIT_CPU`                | `"cpu_exceeded"` | The job needs more CPU time than allowed; bump `HARES_CPU_LIMIT_SEC` or split it. |

The host stays alive in every case — the worst an agent can do is keep
retrying the same overcommit and waste compute waiting to be killed.

## Why setrlimit + psutil (not cgroups, not systemd-run)

| Mechanism                       | Fits an unprivileged dev box? | Notes                                                      |
|---------------------------------|-------------------------------|------------------------------------------------------------|
| `resource.setrlimit(RLIMIT_AS)` | ✅ unprivileged               | Per-process AS cap. Kernel handles the kill.               |
| `resource.setrlimit(RLIMIT_CPU)`| ✅ unprivileged               | Per-process CPU-seconds cap.                               |
| `os.sched_setaffinity`          | ✅ unprivileged (Linux)        | Lets `pytest -n auto` see only the cores we want.          |
| psutil RSS poll + SIGKILL       | ✅                            | Catches AS ≠ RSS gaps (mmap, shared libs, xdist workers).  |
| cgroups v1                      | ❌ requires root              | Skip.                                                      |
| `systemd-run --user --scope`    | △ available, adds dep         | Skip for v1.                                               |

## Caveats

- The semaphore is per-server-process. Two MCP clients = two Hares processes
  = two independent caps. If you need a true host-wide cap across processes,
  that's a v2 feature (named-semaphore via SysV).
- The pre-flight rewriter is conservative: only known patterns. Anything it
  doesn't recognise passes through to the kernel-level guards.
- `RLIMIT_AS` measures virtual address space, which can be noticeably larger
  than RSS (mmap, shared libs). The psutil monitor is the actual safety net.

## License

Apache-2.0.
