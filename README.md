# Hares - حَارِس

> **The kernel-enforced guard for multi-agent LLM workflows.**
> One binary stands between your LLM agents and your machine — capping what they can run, how much they can consume, and where they can write.

[![PyPI](https://img.shields.io/pypi/v/hares.svg)](https://pypi.org/project/hares/)
[![Python](https://img.shields.io/pypi/pyversions/hares.svg)](https://pypi.org/project/hares/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-linux-lightgrey.svg)]()
[![Ko-fi](https://img.shields.io/badge/support-ko--fi-FF5E5B.svg)](https://ko-fi.com/mewais)

---

## Why Hares exists

The moment you let LLM agents touch a real shell — and especially the moment you run several at once with a human mostly out of the loop — three things break:

**1. Every agent thinks it owns the machine.**
Spin up five parallel agents and each one happily forks a `pytest -j$(nproc)`, a `cargo build --release`, or a `make -j`. None of them know the others exist. Your box becomes a space heater, the OOM killer starts making decisions for you, and the "concurrent agents" dream collapses into one big serial queue of crashes.

**2. The dev box can't run the heavy stuff.**
Simulators, large synthesis runs, big builds — they belong on the cluster, not your laptop. But you don't want agents shelling onto LSF head nodes, juggling `bsub` flags, or leaving zombie jobs behind every time a session crashes.

**3. "Please don't write outside this directory" isn't a security policy.**
It's a suggestion. With humans out of the loop, your isolation is only as strong as the kernel makes it — not as strong as your prompt asks for it.

Hares fixes all three at the layer where it actually matters: the kernel.

## What Hares does

| | |
|---|---|
| **bwrap mount namespace** | Agents *physically cannot* write outside their declared scope. The kernel rejects it — no policy to argue with, no jailbreak prompt that helps. |
| **`RLIMIT_AS` + `RLIMIT_CPU` + wall-clock timeout** | Every subprocess is memory-, CPU-, and time-capped at the kernel level. A runaway agent burns its allotment and dies; the host stays alive. |
| **Global concurrency semaphore** | N parallel Hares processes share **one** subprocess cap and **one** core-pool allocator. Five agents with a budget of six subprocesses run six total — not thirty. |
| **Runtime scope narrowing** | An orchestrator calls `restrict_paths(['lib/parser'])` mid-session to shrink an agent's writable surface to a single subdirectory. Both the fs validator and the bwrap mount list re-arm together. |
| **LSF + SLURM cluster bridge** | Submit, poll, cancel HPC jobs from MCP — backed by `bsub`/`bjobs`/`bkill` or `sbatch`/`squeue`/`scancel`. No one gets a shell on the cluster; jobs are tracked per-session and reaped on disconnect. |

**One binary**, **four tool families** (`shell`, `fs`, `lsf`, `slurm`), **uniform scope semantics** across all of them.

Hares is also an **importable Python library** — your test runners, CI scripts, and framework code get the same kernel-enforced caps without going through MCP. Library callers and MCP servers share a single concurrency budget via `HARES_COORDINATION_DIR`, so a hybrid deploy of agents and static code can't blow past the host budget either.

## Who this is for

- You're building agentic systems that fan out across multiple LLM sessions and need them to coexist on one box.
- You run automated LLM flows where a human isn't reviewing every tool call.
- You want HPC cluster access from agents without handing them the cluster.
- You're tired of "the agent ran `rm -rf` somewhere it shouldn't have" being a thing that can happen.

## Try it (3 paths)

Pick the one that matches your setup. Each is a few minutes to wire up and reversible in one line.

**Claude Code user?** Hares slots in as an MCP server. Native `Read` / `Write` / `Edit` / `Glob` / `Grep` keep working unchanged — only shell is routed through Hares for the kernel-enforced sandbox + resource caps. Two small config files, fully reversible.
→ [Claude Code setup](#claude-code-lose-nothing-gain-a-lot)

**Already using an MCP filesystem (Cline, Roo Code, Continue, custom Anthropic SDK app)?** You're already paying MCP cost for fs ops. Swapping to Hares is free at that point and gains you kernel-enforced scope plus defense against the [CVE-2025-53109](https://nvd.nist.gov/vuln/detail/CVE-2025-53109) / [53110](https://nvd.nist.gov/vuln/detail/CVE-2025-53110) class of path-validation bugs.
→ [Drop-in for existing MCP filesystem servers](#drop-in-for-existing-mcp-filesystem-servers)

**Building a custom agent in Python?** Import the engine directly, skip the MCP layer entirely. Same kernel-enforced caps; one shared concurrency budget with any MCP-side Hares instances via `HARES_COORDINATION_DIR`.
→ [Python library usage](#python-library-usage)

> Working configs and runnable scripts for all three paths live in [`examples/`](examples/) — copy and tweak.

---

## Table of contents

- [Quick start](#quick-start) · [Integrations](#integrations) · [Python library usage](#python-library-usage)
- [CLI reference](#cli-reference) · [Tool surface](#tool-surface) · [Env var reference](#env-var-reference)
- [System-dir policy](#system-dir-policy) · [Cross-process coordination](#cross-process-coordination)
- [Active scope and `restrict_paths`](#active-scope-and-restrict_paths) · [bwrap mechanics](#bwrap-mechanics)
- [Multi-instance use under flat-namespace registries](#multi-instance-use-under-flat-namespace-registries)
- [Threat model](#threat-model) · [Quirks and edge cases](#quirks-and-edge-cases)
- [Project status](#project-status) · [Contributing](#contributing) · [License](#license)

---

## Quick start

```sh
pip install hares
```

```sh
# Shell guard — resource-capped, bwrap-sandboxed execute_command:
HARES_FS_CEILING=/work/proj hares-mcp

# Filesystem MCP, read-write under /work/proj:
HARES_FS_CEILING=/work/proj hares-mcp --enable=fs

# Filesystem MCP, read-only (observe-only, no write tools registered):
HARES_FS_CEILING=/work/proj hares-mcp --enable=fs --read-only

# Shell + FS in one process, shared active scope, scoped tool names,
# state persisted across restarts:
HARES_FS_CEILING=/work/proj hares-mcp \
  --enable=fs+shell \
  --scope-id=agent_x \
  --state-file=/tmp/hares-state-agent_x.json

# Two instances coexisting under a flat-namespace MCP registry
# (unique scope-ids prevent tool name collisions):
hares-mcp --enable=fs --scope-id=src         --ceiling=/work/proj &
hares-mcp --enable=fs --scope-id=unit_tests  --ceiling=/work/proj &

# LSF cluster execution — submit jobs, poll, cancel (no ceiling required):
HARES_LSF_QUEUE=gpu hares-mcp --enable=lsf --scope-id=cluster

# SLURM cluster execution — same surface, different scheduler:
HARES_SLURM_PARTITION=gpu hares-mcp --enable=slurm --scope-id=cluster
```

> **Non-Linux / container without user namespaces?** Set
> `HARES_SANDBOX_DISABLED=1` to skip bwrap. Shell `--read-only` and
> kernel scope enforcement won't apply, but resource caps and concurrency
> throttling still work.

---

## Integrations

Hares plugs into the major MCP-aware coding agents and orchestrators. Each subsection below is independent — pick the one that matches your setup. All are fully reversible: drop the deny rules / config block and you're back to the agent's defaults.

### Claude Code (lose nothing, gain a lot)

Drop Hares into your Claude Code setup with two small config files. **You lose nothing** — Claude Code's native `Read` / `Write` / `Edit` / `Glob` / `Grep` keep working unchanged, with zero MCP round-trip latency and zero extra tokens spent on tool schemas. Only shell execution is swapped, which is also where every "Claude broke my dev box" story comes from.

What you gain:

- 🛡️ **Kernel-enforced shell sandbox** — every command Claude runs goes through bwrap. No more wondering whether `rm -rf` could touch the wrong path; the kernel rejects writes outside the declared scope.
- 🔥 **Resource caps that actually fire** — `RLIMIT_AS` + `RLIMIT_CPU` + RSS-overshoot kill stop a runaway `pytest -n auto` or `make -j$(nproc)` from OOM-ing your machine. Claude can still ask for too much; it just won't *get* too much.
- 🪢 **Cross-session concurrency cap** — N parallel Claude windows share **one** subprocess budget instead of N independent ones. Five Claude sessions with `HARES_MAX_CONCURRENT=6` run six commands total, not thirty.
- 🛰️ **HPC cluster access from inside Claude** — submit, poll, and cancel LSF or SLURM jobs without giving Claude a shell on the cluster head node. Each job's `resource_spec` is right-sized by Claude per submission.
- 🎯 **Mid-session scope narrowing** — call `restrict_paths(['lib/parser'])` to shrink Claude's writable surface to one directory for the rest of the session. No edit anywhere else can succeed, kernel-enforced.

**1. Register Hares in `.mcp.json`** (project root) or `~/.claude.json` (user-wide):

```json
{
  "mcpServers": {
    "hares": {
      "command": "hares-mcp",
      "args": ["--enable=shell", "--scope-id=hares"],
      "env": {
        "HARES_FS_CEILING": "/path/to/your/project",
        "HARES_MAX_CONCURRENT": "2",
        "HARES_MEM_LIMIT_MB": "8000",
        "HARES_CPU_LIMIT_SEC": "1800"
      }
    },
    "hares-slurm": {
      "command": "hares-mcp",
      "args": ["--enable=slurm", "--scope-id=hpc"],
      "env": { "HARES_SLURM_PARTITION": "your-partition" }
    }
  }
}
```

(Drop the `hares-slurm` block if you don't have a cluster. Swap `slurm` → `lsf` and `HARES_SLURM_PARTITION` → `HARES_LSF_QUEUE` for LSF.)

**2. Deny native Bash in `.claude/settings.json`** so Claude is forced to use Hares for shell:

```json
{
  "permissions": {
    "deny":  ["Bash"],
    "allow": ["mcp__hares__*", "mcp__hares-slurm__*"]
  }
}
```

**3. Point Claude at the alternative in `CLAUDE.md`:**

```md
## Shell execution
Native Bash is denied. Use `mcp__hares__hares_execute_command` for all
shell work — it gives you bwrap sandbox + RLIMIT + a global concurrency
cap shared across any other Claude session running in parallel.

For HPC jobs use `mcp__hares-slurm__hpc_slurm_*` (or `mcp__hares-lsf__*`
if your cluster uses LSF). Right-size each job's `resource_spec` —
small asks queue faster.
```

That's it. Cost: ~50–100 ms per shell call vs native (MCP round-trip). Reversible in one line — drop `"Bash"` from `deny` and you're back to native instantly.

> **Multi-session bonus:** add `"HARES_COORDINATION_DIR": "/tmp/hares-claude"` to the env block and every Claude window using this config will share **one** concurrency cap. No coordination needed in your prompts; the kernel does it.

### Claude Code — maximum security mode (route filesystem through Hares too)

The default setup above keeps Claude's native file tools because for most users the latency and token cost aren't worth it. **For some users they absolutely are.** If any of these is true, swap the filesystem layer too:

- 🔐 **Claude is touching sensitive paths** (production configs, customer data, SSH keys, anything with consequences). The native file tools enforce paths at the policy layer; Hares enforces them at the *kernel* layer. Different threat model, different defense weight.
- 🤖 **You're running Claude unattended** (CI loops, automated PR review, scheduled tasks, autonomous agents). When a human isn't reviewing every tool call, you need guardrails the LLM can't talk its way around. Prompt injection becomes a real concern; only the kernel ignores prompts.
- 🏢 **Multi-tenant or shared box** where another user's files are reachable from Claude's process. Hares' bwrap mount namespace makes those paths *invisible* to the subprocess, not just denied.
- 🎯 **You want `restrict_paths` to actually restrict edits, not just shell.** With native fs tools, narrowing the scope mid-session only tightens shell commands; Claude's `Edit` and `Write` ignore it. With Hares fs, one `restrict_paths(['lib/parser'])` call locks BOTH layers.
- 🧱 **You've been bitten by the CVE-2025-53109 / 53110 class** (symlink escape, prefix-match bypass) in another MCP filesystem server. Hares canonicalizes paths and validates against the resolved tree, not the input string.

**Cost is real and you should know it:**
- ~5–10K extra prompt tokens for the 12 filesystem tool schemas (paid once per session, then cached).
- ~50–100 ms per file op, same as the shell case.
- Claude Code's inline diff renderer doesn't trigger for MCP edits — you'll see JSON tool output instead of the pretty side-by-side view.

If those costs are worth the kernel-enforced filesystem boundary for your use case, the swap is two edits:

**1. Switch the Hares server to `fs+shell`** (one shared scope across both layers):

```json
{
  "mcpServers": {
    "hares": {
      "command": "hares-mcp",
      "args": ["--enable=fs+shell", "--scope-id=hares"],
      "env": {
        "HARES_FS_CEILING": "/path/to/your/project",
        "HARES_MAX_CONCURRENT": "2",
        "HARES_MEM_LIMIT_MB": "8000"
      }
    }
  }
}
```

**2. Deny native file tools** in `.claude/settings.json` (in addition to `Bash`):

```json
{
  "permissions": {
    "deny":  ["Bash", "Read", "Write", "Edit"],
    "allow": ["mcp__hares__*"]
  }
}
```

Add a one-liner to `CLAUDE.md` so Claude knows to reach for the MCP file tools (`hares_read_file`, `hares_write_file`, `hares_edit_file`, etc.). Same reversibility — remove the deny entries to get the native tools back.

> Note: `Glob` and `Grep` aren't routed by Hares (they're search tools, not write tools, and the threat model doesn't require it). Leave them allowed; you keep fast filesystem search.

### Drop-in for existing MCP filesystem servers

If your agent setup (Cline, Roo Code, Continue, custom Anthropic SDK app, OpenHands, anything else) is already plugging in `@modelcontextprotocol/server-filesystem` or a fork of it — **you're already paying the MCP latency and token cost.** Swapping to Hares' fs is free at that point and gains you:

- **Kernel-enforced scope** instead of policy-layer path validation. The reference filesystem MCP and most forks have shipped CVEs in this class ([CVE-2025-53109 symlink escape](https://nvd.nist.gov/vuln/detail/CVE-2025-53109), [CVE-2025-53110 prefix-match bypass](https://nvd.nist.gov/vuln/detail/CVE-2025-53110)) because path-string validation is brittle. Hares canonicalizes via the kernel's own resolver and rejects what doesn't resolve under the ceiling — no string-prefix logic to bypass.
- **One shared scope across fs and shell** if you also run `--enable=fs+shell`. A single `restrict_paths` call narrows both layers; an agent with both can't use the shell to escape the fs scope.
- **Cross-process concurrency cap** when you run multiple agents — set `HARES_COORDINATION_DIR` and N agents share one budget.
- **Same tool surface** as the reference server (`read_file`, `write_file`, `edit_file`, `list_directory`, `search_files`, `get_file_info`, etc.) — your agent's existing prompts and tool-use patterns keep working.

Drop-in replacement in your agent's MCP config:

```jsonc
// before:
{
  "filesystem": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/project"]
  }
}

// after:
{
  "hares-fs": {
    "command": "hares-mcp",
    "args": ["--enable=fs"],
    "env": { "HARES_FS_CEILING": "/path/to/project" }
  }
}
```

Add `--read-only` if the agent only needs to observe. Add `--scope-id=<name>` if you're running multiple Hares instances behind a flat-namespace MCP gateway.

---

## Python library usage

Hares ships as both an `hares-mcp` binary and an importable Python package.
Static code (test runners, CI scripts, framework code, anything that wants
sandboxed subprocess without going through MCP) can use the same engine
directly.

### Local subprocess (`Runner`)

```python
import asyncio, os
from hares.runner import Runner
from hares.sandbox import load_sandbox_config

# One Runner per process — owns the semaphore + core pool.
_runner = Runner(
    max_concurrent=2,
    mem_limit_mb=7000,
    cpu_limit_sec=1800,
    sandbox=load_sandbox_config(default_cwd=os.getcwd()),
)

# Async caller:
result = await _runner.execute("pytest -q tests/", timeout=300)

# Sync caller (gets the same caps; one extra call):
result = asyncio.run(_runner.execute("pytest -q tests/", timeout=300))

# result == {
#   "exit_code":      int,
#   "stdout":         str,
#   "stderr":         str,
#   "killed_reason":  None | "timeout" | "rss_exceeded" | "cpu_exceeded",
#   "rewrites":       [...],   # any pre-flight overcommit edits applied
# }
```

`load_sandbox_config()` reads the same `HARES_SANDBOX_*` env vars as the
MCP server, so behavior is consistent across both surfaces. Pass
`sandbox=None` to skip bwrap while keeping RLIMITs and concurrency caps.

### LSF / SLURM cluster jobs (`LsfExecutor` / `SlurmExecutor`)

Both backends share one async core (`hares.cluster.ClusterExecutor`)
and expose an identical Python API. Pick the import that matches
your cluster:

```python
from hares.cluster.lsf   import LsfExecutor,   load_lsf_config
from hares.cluster.slurm import SlurmExecutor, load_slurm_config
from hares.cluster       import JobSpec

_executor = LsfExecutor(cfg=load_lsf_config())
# or:
_executor = SlurmExecutor(cfg=load_slurm_config())

# Submit one job and wait:
result = await _executor.execute_blocking(
    JobSpec(command="verilator --build sim.v",
            resource_spec="rusage[mem=8192]"),
    timeout_sec=3600,
)

# Submit several jobs to run in parallel on the cluster:
jobs = await _executor.submit([
    JobSpec(command="sim_config_1", resource_spec="rusage[mem=4096]"),
    JobSpec(command="sim_config_2", resource_spec="rusage[mem=4096]"),
])
results = await _executor.wait([j["job_id"] for j in jobs], timeout_sec=7200)
```

### Coordination across MCP and library callers

When `HARES_COORDINATION_DIR` is set, both library `Runner` instances and
`hares-mcp` server processes share one POSIX semaphore + one core-pool
allocator. A workflow that mixes LLM agents (calling MCP tools) with static
framework code (calling `Runner` directly) gets one global concurrency cap
across all of them — no agent or library caller can blow past
`HARES_MAX_CONCURRENT` while another is running.

Use cases:

- Test runners and CI systems that want resource caps on subprocess
- Framework / orchestration code that needs the same kernel-enforced
  caps as the LLM-driven side of the system
- Any sync code that occasionally needs a sandboxed subprocess
- Hybrid deploys where LLM agents and static Python coexist and must
  share a single host budget

---

## CLI reference

| Flag | Required | Default | Validation |
|---|---|---|---|
| `--enable {shell,fs,fs+shell,lsf,slurm}` | no | `shell` | choices |
| `--scope-id <id>` | no | unset (no prefix) | `^[a-z][a-z0-9_]*$` |
| `--ceiling <path>` | **yes for shell/fs** (CLI or env); optional for lsf/slurm | `$HARES_FS_CEILING` | abs-resolved; rejected if under `.git/`; rejected if under any blocklist entry when `HARES_DISALLOW_SYSTEM_DIRS=1` |
| `--read-only` | no | off | flag (shell/fs only; no effect for lsf/slurm) |
| `--state-file <path>` | no | unset (in-memory only) | abs-resolved; warning if path is under ceiling (shell/fs only) |

All flags fail fast at startup with a clear human-readable error. Bare
`hares-mcp` invocation works as long as `HARES_FS_CEILING` is in env.

---

## Tool surface

The exact tool list depends on `--enable`, `--scope-id`, and
`--read-only`. Below, `[<scope>_]` denotes the prefix (omitted when
`--scope-id` is unset).

### `--enable=shell`

* `[<scope>_]execute_command`
* `[<scope>_]restrict_paths`
* `[<scope>_]get_active_paths`

### `--enable=fs` (read tools always; writes suppressed by `--read-only`)

Reads:

* `[<scope>_]read_file`
* `[<scope>_]read_text_file`
* `[<scope>_]read_multiple_files`
* `[<scope>_]list_directory`
* `[<scope>_]list_directory_with_sizes`
* `[<scope>_]directory_tree`
* `[<scope>_]search_files`
* `[<scope>_]get_file_info`

Writes (suppressed by `--read-only`):

* `[<scope>_]write_file`
* `[<scope>_]edit_file`
* `[<scope>_]create_directory`
* `[<scope>_]move_file`

Restrict (always):

* `[<scope>_]restrict_paths`
* `[<scope>_]get_active_paths`

### `--enable=fs+shell`

Union of the fs and shell surfaces, sharing **one** active scope. A
single `<scope>_restrict_paths` call narrows BOTH the fs path
validator AND the shell bwrap mount list.

### `--enable=lsf` and `--enable=slurm`

Five tools per scheduler for HPC cluster job management. Tool names
follow the pattern `[<scope>_]<scheduler>_<verb>` so an org with both
schedulers can run two Hares instances side by side without name
collisions.

| Verb | Returns |
|---|---|
| `[<scope>_]<scheduler>_execute_blocking` | Submit one job, wait for it, return stdout/stderr/exit_code |
| `[<scope>_]<scheduler>_submit` | Submit a list of jobs (non-blocking), return job_ids |
| `[<scope>_]<scheduler>_wait` | Wait for job_ids; returns when all reach DONE/EXIT or timeout |
| `[<scope>_]<scheduler>_cancel` | Cancel a list of job_ids |
| `[<scope>_]<scheduler>_jobs` | List all jobs submitted in this session with current status |

Both backends share one async core (poll loop, output capture, timeout,
session-job map, cancel bookkeeping). Differences are confined to
argv builders and status parsers:

| | LSF | SLURM |
|---|---|---|
| Submit binary | `bsub` | `sbatch --parsable` |
| Status query | `bjobs -noheader <id>` | `squeue -h -j <id> -o '%T'`, fall back to exitcode file when empty |
| Cancel binary | `bkill` | `scancel` |
| `resource_spec` | Contents of `-R` (e.g. `rusage[mem=8192]`) | Free-form sbatch flags, shlex-split (e.g. `--mem=8192 --cpus-per-task=4 --time=01:00:00`) |
| Federation | n/a | `--parsable` returns `JOBID;CLUSTER`; the suffix is stripped |

In both cases stdout/stderr are captured by an inner shell redirect
(bypassing the scheduler's own output-file headers). Exit codes are
written to a separate file and read when the job completes — this
works on SLURM clusters that don't have `slurmdbd` accounting
enabled.

> **Security note for cluster modes.** See
> [Cluster modes — what does NOT apply](#cluster-modes--what-does-not-apply).
> bwrap, RLIMIT, and active-scope enforcement do NOT extend to cluster
> nodes for either backend.

### Symmetry table

| Flag | `fs` | `shell` | `lsf` / `slurm` |
|---|---|---|---|
| `--ceiling` | Outer bound for tool-call paths | Outer bound for bwrap mount namespace | Optional; used for pre-submission cwd check only |
| `--scope-id` | Tool-name prefix | Tool-name prefix | Tool-name prefix |
| `--read-only` | Write tools NOT registered | bwrap mounts active scope RO | No effect |
| `--state-file` | Persists active scope | Persists active scope | Not applicable |
| bwrap sandbox | No | **Yes** — kernel-enforced | No — cluster node runs unrestricted |
| RLIMIT_AS/CPU | No | **Yes** — kernel-enforced | No — use scheduler `resource_spec` |
| Concurrency cap | No | **Yes** — semaphore + core pool | No — scheduler manages cluster scheduling |

The mechanism differs; the scope semantics are uniform where applicable.

---

## Env var reference

### Operator-deploy: ceiling default

| Variable | Purpose |
|---|---|
| `HARES_FS_CEILING` | Default for `--ceiling` when not on the CLI. Operators with a single project root set this once in their shell rc. Falls back to `$PWD` (with an INFO log) when neither this nor `--ceiling` is set; the PWD default is rejected if it would resolve under a `.git/` tree, in which case an explicit `--ceiling` is required. |

### Operator-deploy: bwrap controls

| Variable | Default | Purpose |
|---|---|---|
| `HARES_SANDBOX_DISABLED` | unset | Set to `1` to opt OUT of bwrap entirely. **0.2 default is bwrap-required**; this is the escape hatch for non-Linux, restricted-userns containers, or debugging. When set, shell `--read-only` and active-scope bwrap-mount enforcement are NOT applied — the operator has explicitly declared "no kernel sandbox here." |
| `HARES_SANDBOX_RW` | server cwd | Colon-separated extra RW mount paths. Composes with the active scope. Use for tooling outside any narrowed scope (gitconfig, pip cache, /opt compilers). |
| `HARES_SANDBOX_RO` | empty | Colon-separated extra RO mount paths. |
| `HARES_SANDBOX_NETWORK` | `on` | `on` keeps pip/git working; `off` also unshares the netns (hermetic test runs). |
| `HARES_SANDBOX_TMP_SIZE_MB` | unset | Optional tmpfs size cap for `/tmp` inside the sandbox. |
| `HARES_SANDBOX_BWRAP_BIN` | `bwrap` | Override the bwrap binary location. |

Backward compat: legacy `HARES_SANDBOX_MODE={none,off,false,0}` is still
honored as an opt-out for older deploys.

### Operator-deploy: subprocess throttling

| Variable | Default | Purpose |
|---|---|---|
| `HARES_MAX_CONCURRENT` | `2` | Concurrent subprocess cap. **Per-process** when `HARES_COORDINATION_DIR` is unset; **GLOBAL across all Hares processes** when it's set. |
| `HARES_MEM_LIMIT_MB` | `7168` | Per-subprocess `RLIMIT_AS` (MB). |
| `HARES_CPU_LIMIT_SEC` | `1200` | Per-subprocess `RLIMIT_CPU` (sec). |
| `HARES_DEFAULT_TIMEOUT_SEC` | `300` | Default per-command wall-clock timeout. |
| `HARES_RSS_POLL_INTERVAL_SEC` | `2` | psutil RSS aggregation interval. |
| `HARES_RSS_OVERSHOOT_RATIO` | `1.2` | Kill on process-tree RSS > `MEM_LIMIT × ratio`. |

### Operator-deploy: cross-process coordination

| Variable | Default | Purpose |
|---|---|---|
| `HARES_COORDINATION_DIR` | unset | When set, multiple Hares processes share ONE subprocess concurrency semaphore + ONE core-pool allocator via files in this dir. Set this to the same path across all sibling instances launched for one workload (e.g. an orchestrator's per-run dir). Standalone single-instance deploys can leave it unset. |

### Operator-deploy: system-dir validation

| Variable | Default | Purpose |
|---|---|---|
| `HARES_DISALLOW_SYSTEM_DIRS` | unset | Set to `1` to opt IN to strict mode. When set, every path argument (`--ceiling`, `HARES_SANDBOX_RW/RO`, `restrict_paths` targets) is validated against the system-dir blocklist. **Default is permissive** — operators can use any path. Recommended for production / shared-tenant deployments. |
| `HARES_EXTRA_SYSTEM_DIRS` | unset | Colon-separated additional dirs added to the blocklist when `HARES_DISALLOW_SYSTEM_DIRS=1`. Operators tune for their environment. Additive only — no override / subtract. |

### Operator-deploy: LSF cluster jobs (`--enable=lsf`)

| Variable | Default | Purpose |
|---|---|---|
| `HARES_LSF_QUEUE` | unset | LSF queue passed to `bsub -q`. When unset, LSF uses its site default. |
| `HARES_LSF_DEFAULT_RESOURCE_SPEC` | unset | Default `bsub -R` resource spec applied to every job unless the caller overrides per-job (e.g. `rusage[mem=8192] span[hosts=1]`). |
| `HARES_LSF_POLL_INTERVAL_SEC` | `10` | How often `lsf_wait` polls `bjobs` for job status. Lower values increase responsiveness at the cost of more bjobs traffic. |
| `HARES_LSF_DEFAULT_TIMEOUT_SEC` | `86400` | Default timeout for `lsf_wait` and `lsf_execute_blocking` when the caller doesn't pass one. |
| `HARES_LSF_OUTPUT_DIR` | per-session tempdir | Directory where stdout/stderr/exitcode files are written. Must be on a shared filesystem visible to both the submitting node and cluster nodes. |
| `HARES_LSF_BSUB_BIN` | `bsub` | Path to the `bsub` binary. Override if LSF is not on `PATH`. |
| `HARES_LSF_BJOBS_BIN` | `bjobs` | Path to the `bjobs` binary. |
| `HARES_LSF_BKILL_BIN` | `bkill` | Path to the `bkill` binary. |

### Operator-deploy: SLURM cluster jobs (`--enable=slurm`)

| Variable | Default | Purpose |
|---|---|---|
| `HARES_SLURM_PARTITION` | unset | SLURM partition passed to `sbatch --partition`. When unset, SLURM uses the cluster's default partition. |
| `HARES_SLURM_ACCOUNT` | unset | Account passed to `sbatch --account`. Required by some clusters for accounting/billing. |
| `HARES_SLURM_DEFAULT_RESOURCE_SPEC` | unset | Default `sbatch` flags applied to every job unless the caller overrides per-job. Free-form, shlex-split (e.g. `--mem=8192 --cpus-per-task=4 --time=01:00:00 --gres=gpu:1`). |
| `HARES_SLURM_POLL_INTERVAL_SEC` | `10` | How often `slurm_wait` polls `squeue` for job status. |
| `HARES_SLURM_DEFAULT_TIMEOUT_SEC` | `86400` | Default timeout for `slurm_wait` and `slurm_execute_blocking` when the caller doesn't pass one. |
| `HARES_SLURM_OUTPUT_DIR` | per-session tempdir | Directory where stdout/stderr/exitcode files are written. Must be on a shared filesystem visible to both the submitting node and cluster nodes. |
| `HARES_SLURM_SBATCH_BIN` | `sbatch` | Path to the `sbatch` binary. Override if SLURM is not on `PATH`. |
| `HARES_SLURM_SQUEUE_BIN` | `squeue` | Path to the `squeue` binary. |
| `HARES_SLURM_SCANCEL_BIN` | `scancel` | Path to the `scancel` binary. |

---

## System-dir policy

When `HARES_DISALLOW_SYSTEM_DIRS=1`, the following are rejected by
default as ceiling / mount / active-scope arguments:

```
/etc            /proc           /sys            /dev
/bin            /sbin
/usr/bin        /usr/sbin
/boot           /root
/lib            /lib32          /lib64
```

Explicitly **NOT** in the default blocklist (policy-debatable):

| Path | Why not in default |
|---|---|
| `/opt` | Conventional location for user-installed tools (qemu, gradle, custom toolchains). Blocking by default would break common workflows. |
| `/var` | Too broad. Some subpaths are sensitive (`/var/log`); most isn't. |
| `/run` | Runtime state; sometimes needed for sockets, dbus. |
| `/tmp` | Explicitly user-writable. |
| `/home` | User data. |
| `/usr` (parent) | `/usr/local` and `/usr/share` are fine; only `/usr/bin` and `/usr/sbin` are blocked. |

Operators tune via `HARES_EXTRA_SYSTEM_DIRS`. Example for a stricter
deployment:

```sh
export HARES_DISALLOW_SYSTEM_DIRS=1
export HARES_EXTRA_SYSTEM_DIRS=/opt:/var/log:/run/user/1000
```

### Why default-permissive on system-dir validation but default-required on bwrap?

Asymmetric for a reason:

* bwrap is **load-bearing** — without it, shell `--read-only` and
  active-scope enforcement collapse to lies (no kernel enforcement
  of mount RO/RW).
* System-dir validation is **paranoia** — defense against typos and
  pasted-wrong paths, not load-bearing security.

Different security weight, different defaults.

### Always-on ceiling guard

Regardless of `HARES_DISALLOW_SYSTEM_DIRS`, a ceiling whose resolved
path contains `.git` is ALWAYS rejected — a write-enabled scope rooted
in version-control state would let an agent rewrite history. This rule
cannot be opted out of.

---

## Cross-process coordination

When `HARES_COORDINATION_DIR` is **unset** (the default for standalone
single-instance deploys), Hares uses an in-process `asyncio.Semaphore`
and CPU-affinity is the first-N cores per process (no inter-process coordination).

When `HARES_COORDINATION_DIR` **is** set, Hares processes that share
the dir coordinate via:

* A **POSIX named semaphore** (`posix_ipc.Semaphore`), name derived
  from `sha1(coord_dir)[:16]`. Capacity = `HARES_MAX_CONCURRENT`.
  This is a GLOBAL cap: with 5 Hares processes and cap=6, the total
  is 6 subprocesses in flight, not 30.
* A **shared `core_pool.json`** allocator (fcntl-locked). Each Hares
  process claims a non-overlapping CPU-affinity slice on first
  `execute_command`; stale-PID cleanup on the next allocator entry.

Worked example — a typical multi-Hares orchestrator deploy:

```
HARES_MAX_CONCURRENT=6        (six subprocesses in flight system-wide)
5 Hares processes (3 fs + 2 shell)
HARES_COORDINATION_DIR=/path/to/per-run-coord-dir/

→ At most 6 subprocesses across all 5 hares ever run concurrently.
→ Each Hares pins its subprocesses to its own slice of cores.
→ Without coordination dir, the same deploy would allow 5×6=30
  concurrent subprocesses and have all of them fight for the same
  low-numbered cores.
```

POSIX semaphore lifecycle:

* First Hares process to access creates with `O_CREAT | O_EXCL` race-
  safely; others open the existing instance.
* Cleanup is **skipped at process exit** — POSIX semaphores are
  kernel-persistent until explicit unlink or reboot. The orchestrator
  spawning Hares processes is responsible for unlinking at workload
  end (or just removing the coordination dir, which Hares treats as
  authoritative). Standalone operators can manually
  `posix_ipc.unlink_semaphore(...)` if needed.
* A stale semaphore from a prior abnormal termination whose capacity
  doesn't match the current `HARES_MAX_CONCURRENT` is logged as a
  warning and reused as-is. Operators should clean up between runs
  with mismatched caps OR set a fresh `HARES_COORDINATION_DIR`
  per-run.

When `posix_ipc` isn't installed, Hares falls back to the in-process
semaphore even if `HARES_COORDINATION_DIR` is set — with a logged
warning. Multi-instance throttling is then NOT enforced.

---

## Active scope and `restrict_paths`

The "active scope" is a runtime-narrowable subset of the ceiling. It
exists to let any allowlisted caller decide, mid-session, "the write
authority for this work is exactly these directories." The two
restrict tools are ALWAYS registered when `--enable` covers fs or
shell — gating who calls them is the responsibility of the agent's
tools-allowlist (client-side concern).

### `[<scope>_]restrict_paths(paths: list[str]) -> {"active_paths": [...]}`

Set-replace semantics, not accumulate. Each path:

* may be relative (resolved against the ceiling) or absolute,
* must NOT contain `..` (traversal-rejection),
* must resolve to a path under the ceiling (escape-rejection),
* is `mkdir -p`'d if it doesn't exist,
* is validated against the system-dir blocklist when
  `HARES_DISALLOW_SYSTEM_DIRS=1`.

After validation, the in-memory state is replaced and atomically
written to `--state-file` if configured.

For a shell instance, the call also re-arms the runner so the next
`execute_command` re-spawns bwrap with the new mount list. **In-flight
subprocesses are not affected** — each one has its own bwrap process
tree.

For an `fs+shell` instance, ONE call narrows both layers.

`restrict_paths([])` is legal: it means "no writes / no runnable
scope." Reads against the ceiling still work for fs.

### `[<scope>_]get_active_paths() -> {"active_paths": [...]}`

Returns the current scope. Use it for round-trip introspection or to
verify the narrow took effect.

### State file shape

```json
{
  "version": 1,
  "scope_id": "src",
  "ceiling": "/work/proj",
  "active_paths": [
    "/work/proj/lib/parser",
    "/work/proj/lib/lexer"
  ],
  "last_restrict_at": "2026-05-05T10:30:00Z"
}
```

Atomic write: write to `<file>.tmp`, fsync, `os.replace` to `<file>`.

### Crash recovery

On startup, if `--state-file` is set Hares attempts to load it. The
load FAILS GRACEFULLY (instance starts with active scope = `[]`,
i.e. no writes allowed until restrict is called) when:

* file doesn't exist,
* file is not valid JSON,
* schema version mismatch,
* `scope_id` in file doesn't match the instance's `--scope-id`,
* `ceiling` in file doesn't match the instance's `--ceiling`.

The mismatch checks defend against accidentally reusing a state file
from a prior, differently-configured instance.

---

## bwrap mechanics

When bwrap is enabled (the default — disable with
`HARES_SANDBOX_DISABLED=1`), each `execute_command` runs inside a
fresh mount namespace assembled from:

* The active scope: mounted **read-write** by default, **read-only**
  when the instance has `--read-only`. (When the active scope is
  empty: ceiling alone is mounted, RO if `--read-only`.)
* The ceiling: mounted **read-only** if not already covered by an
  active-scope RW entry.
* `HARES_SANDBOX_RW` / `HARES_SANDBOX_RO` extras: composed on top.
* Standard system paths (RO): `/usr`, `/etc`, plus symlinks for
  `/lib`, `/lib64`, `/bin`, `/sbin` (handles merged-usr distros).
* `/proc`, `/dev`, fresh tmpfs at `/tmp`, `/run`, `/var/tmp`.

`HARES_SANDBOX_NETWORK=off` additionally unshares the netns.

Resource caps (`RLIMIT_AS`, `RLIMIT_CPU`, CPU affinity) still apply
inside the sandbox: bwrap is the parent process; limits flow through
`fork`+`execve`. `--die-with-parent` ensures the inner process tree
is reaped when bwrap (or Hares) dies, so killpg-on-timeout still
works cleanly.

### Why shell `--read-only` REQUIRES bwrap to enforce

The fs `--read-only` flag is enforced by NOT registering the write
tools at all — Python-level enforcement, no kernel needed.

Shell `--read-only` is enforced by mounting the active scope RO in
bwrap. Without bwrap, the kernel doesn't restrict the subprocess'
filesystem access, so `cat > /any/path` succeeds.

When `HARES_SANDBOX_DISABLED=1` is set on a shell `--read-only`
instance, Hares logs a warning and proceeds with no enforcement.
This is intentional: the operator has declared "no kernel sandbox
here," and reverting to a noisy-but-permissive mode is more useful
than refusing to start.

### Requirements

* `bubblewrap` installed on the host (`apt install bubblewrap`,
  `dnf install bubblewrap`, `pacman -S bubblewrap`).
* Kernel with user namespaces enabled (default on every modern
  distro — RHEL 8+, Ubuntu 14.10+, Fedora, Arch).

If bwrap is required (default) but missing from `PATH`, Hares fails
fast at startup with a clear error.

---

## Multi-instance use under flat-namespace registries

Many MCP gateways maintain a **flat global tool-name registry**: when
two MCP server instances expose tools with the same name, only the
last-registered server's tools are reachable. The others register but
never receive calls. The collision is silent.

`--scope-id` is the workaround. When set, ALL tool names exposed by
the instance are prefixed with `<scope_id>_`. Two Hares instances
with distinct scope-ids therefore expose disjoint tool-name sets and
coexist cleanly.

**It is the operator's responsibility to ensure scope-id uniqueness
across instances.** Hares does not coordinate this — by design, the
scope-id is purely a naming convention.

Convention example — three writable scopes plus a read-only view and
two shell tiers:

| Instance | `--scope-id` | Purpose |
|---|---|---|
| `fs_repo_read` | unset | Project-wide read view |
| `fs_src_write` | `src` | Source-only writable scope |
| `fs_unit_tests_write` | `unit_tests` | Unit-test writable scope |
| `fs_verification_write` | `verification` | Verification-artifact writable scope |
| `shell_safe` | `safe` | Inspection-class commands |
| `shell_runtime` | `runtime` | Build / simulators / emulators |

---

## Threat model

Two layers of write scoping, top-down. The same layers apply to fs
MCP writes (`write_file` / `edit_file` / etc.) AND shell-spawned
writes (`cat > /file`, `gcc -o /path`, etc. via `execute_command`):

1. **Operator-set hard ceiling** (`--ceiling` / `HARES_FS_CEILING`).
   Cannot be widened by anyone at runtime. Validated to NOT cover
   `.git/` always; validated against the system-dir blocklist when
   `HARES_DISALLOW_SYSTEM_DIRS=1`.
2. **Caller-set active scope** (via `restrict_paths`, subset of
   ceiling). Set-replace; persists to `--state-file`.
   * For fs: enforced at path-validation time on each write tool call.
   * For shell: enforced at bwrap mount-setup time (active scope RW,
     rest of ceiling RO); re-spawns bwrap on each restrict change.

Below those, kernel-level filesystem-namespace enforcement for
shell-spawned processes via bwrap. Higher-level frameworks layered
on top of Hares may add additional audit / policy layers (post-hoc
git-diff inspection, capability tokens, etc.) — those live outside
Hares.

The shell-restrict symmetry is the critical security property: an
agent with both fs and shell access cannot use the shell to bypass
the fs scope, because the bwrap mount list is derived from the same
active scope.

### Cluster modes — what does NOT apply

`--enable=lsf` and `--enable=slurm` have a fundamentally different security
model from shell/fs modes. The cluster node runs the job with the submitting
user's full filesystem permissions; Hares has no handle on it.

| Guarantee | shell/fs | lsf / slurm |
|---|---|---|
| bwrap mount namespace (scope enforcement) | **Yes — kernel** | **No** |
| RLIMIT_AS / RLIMIT_CPU | **Yes — kernel** | **No** — use scheduler `resource_spec` (LSF `-R rusage[mem=N]`, SLURM `--mem=N --time=hh:mm`) |
| Active-scope write enforcement at runtime | **Yes** | **No** |
| `--ceiling` / `--read-only` | **Yes** | Pre-submission cwd check only (best-effort) |
| Concurrency semaphore | **Yes** | No — scheduler manages cluster scheduling |

The only path-safety measure for cluster modes is a pre-submission
ceiling check on the job's working directory (`cwd`). This catches
configuration mistakes (pointing a job at the wrong directory), not a
determined agent that computes paths at runtime.

Resource governance for cluster jobs belongs in the `resource_spec`
field (or `HARES_LSF_DEFAULT_RESOURCE_SPEC` /
`HARES_SLURM_DEFAULT_RESOURCE_SPEC`), not in Hares.

### Out of scope (all modes)

* Kernel exploits (privilege escalation, namespace escape).
* Side-channel attacks (timing, /proc info disclosure).
* Resource-exhaustion DoS within the cap (an agent that wants to
  burn its allotted CPU and memory is welcome to).
* Denial of MCP service via malformed JSON-RPC (the mcp library
  handles that; Hares just trusts it).
* Tampering with `--state-file` from outside Hares (an attacker
  with write access to the state file path can replay a stale
  scope; defense: keep the state file outside the ceiling, which
  Hares warns about if violated).

---

## Quirks and edge cases

* **Bare `hares-mcp` invocation defaults `--ceiling` to `$PWD`**
  (since 0.5) with an INFO log line so the choice is visible. Set
  `HARES_FS_CEILING` in your shell rc (or pass `--ceiling=PATH`) when
  you want a different root. The PWD default is rejected if it
  resolves under a `.git/` tree — in that case the operator must pass
  `--ceiling` explicitly (better to fail loudly than to default badly).
* **State file under ceiling**: warned about but not rejected. An
  agent with write access to the ceiling could corrupt the active
  scope. Move the state file outside the ceiling for tighter
  isolation.
* **Corrupt state file**: falls back to "no active scope" with a
  warning log. The instance starts with empty active scope until a
  fresh restrict call.
* **Scope-id / ceiling mismatch on state file**: same behavior as
  corrupt — fallback to empty, warning logged. Defends against
  accidentally reusing a state file from a different instance.
* **`restrict_paths([])`**: legal. Means "no writes allowed" (fs
  writes fail; shell bwrap mounts only the ceiling RO). Useful for
  operator-initiated freeze.
* **In-flight subprocess + restrict change**: existing subprocesses
  keep their original bwrap mount tree; only NEXT subprocess gets
  the new scope.
* **Ceiling and `HARES_SANDBOX_RW` overlap**: composes — both apply.
  No override semantics. Sandbox-RW paths can extend reach beyond
  the ceiling for shell instances (e.g. mount in pip cache); they
  do NOT affect the fs path validator on fs tools.
* **`fs+shell` shares ONE scope**: by design. For different
  fs-vs-shell scopes, deploy two separate Hares instances.
* **`--read-only` is operator-locked**: a caller cannot widen an
  instance's mode at runtime via restrict. Restrict only narrows.
* **Reads bounded by ceiling, not active scope**: an instance with
  active scope `[lib/parser]` can still `read_file('/whole/proj/foo')`
  if foo is under ceiling. Active scope narrows WRITES, not reads.
  This is by design — the operator picks ceiling for "what's
  observable"; the caller picks active scope for "what's modifiable."
* **POSIX semaphore persistence**: kernel-persistent until unlinked
  or reboot. The orchestrator that creates `HARES_COORDINATION_DIR`
  is responsible for cleanup. If you abandoned a coordination dir,
  `python -c "import posix_ipc; posix_ipc.unlink_semaphore('<sha1>')"`.
* **Stale semaphore with mismatched cap**: warning logged, reused
  as-is. Safer to set a fresh `HARES_COORDINATION_DIR` per run than
  to rely on the operator to remember to unlink.
* **`posix_ipc` not installed + coordination dir set**: falls back
  to in-process semaphore with a logged warning. Multi-instance
  throttling is NOT enforced.

---

## Project status

Hares is **0.4.x — beta**. The MCP and Python-library APIs are stable enough to build on, but minor versions may still tweak env-var names and tool signatures. Pin the minor version in production.

The bwrap, RLIMIT, and concurrency layers are tested on Linux (RHEL 8+, Ubuntu 20.04+, Fedora). Cluster modes require either IBM Platform LSF (`bsub` / `bjobs` / `bkill`) or SLURM (`sbatch` / `squeue` / `scancel`) on `PATH`, and a shared filesystem visible to both submit and execute hosts.

## Contributing

Bug reports, feature requests, and PRs are all welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and the test workflow. Security issues — please email rather than file a public issue (details in CONTRIBUTING).

## License

Apache-2.0. See [LICENSE](LICENSE).
