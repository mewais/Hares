# Hares - حَارِس

> **The kernel-enforced guard for multi-agent LLM workflows.** (حَارِس — Arabic for "guard" / "guardian".)
> One binary stands between your LLM agents and your machine — enforcing what they can run, how much they can consume, where they can write, and where they can connect.

[![PyPI](https://img.shields.io/pypi/v/hares.svg)](https://pypi.org/project/hares/)
[![Python](https://img.shields.io/pypi/pyversions/hares.svg)](https://pypi.org/project/hares/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-linux-lightgrey.svg)]()
[![Ko-fi](https://img.shields.io/badge/support-ko--fi-FF5E5B.svg)](https://ko-fi.com/mewais)

---

## The problem

**1. Every agent thinks it owns the machine.**
Five parallel agents, five `pytest -j$(nproc)` calls, one OOM killer making decisions for you. None of them knew the others existed.

**2. The dev box can't run the heavy stuff.**
Simulators, synthesis, large builds — they belong on the cluster. But you don't want agents shelling onto LSF head nodes, juggling `bsub` flags, or leaving zombie jobs behind every time a session dies.

**3. "Please don't write outside this directory" isn't a security policy.**
It's a suggestion. With humans out of the loop, your isolation is only as strong as the kernel makes it — not as strong as your prompt asks for it.

**4. The agent connects to things you didn't sanction.**
`git push` to production. A POST to an external API. Code exfiltrated to a remote endpoint. The command exits 0 and you never know it happened — because your filesystem sandbox does nothing about network operations.

Hares fixes all four at the layer where it matters: the kernel.

---

## What Hares does

| | |
|---|---|
| **Filesystem isolation** | bwrap mount namespace. Agents physically cannot write outside their declared scope — the kernel rejects it, unless a human approves a runtime grant. No policy to argue with. |
| **Resource caps** | `RLIMIT_AS` + `RLIMIT_CPU` + RSS-overshoot kill + wall-clock timeout, all kernel-enforced. A runaway agent burns its allotment and dies; the host stays alive. |
| **Command policy** | Deny list for commands that never run. Approval-required list for commands that trigger an MCP elicitation dialog — the user decides before anything executes. Everything else runs immediately. |
| **Network egress control** | Full, off, or allowlist-filtered. Allowlist mode uses nftables inside an isolated netns: the agent cannot reach undeclared endpoints regardless of the command it runs. |
| **HPC cluster bridge** | Submit, poll, cancel jobs on LSF or SLURM from inside the agent. No shell on the cluster; jobs are tracked per-session. |
| **Cross-process coordination** | N parallel Hares instances share one subprocess cap and one core-pool via per-slot flock files. Five agents share six slots total — not thirty. |

**One `pip install`. Uniform semantics across every mode.**

---

## Quick setup

```sh
pip install hares
```

**For Claude Code (4 steps):**

**1.** Add to `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "hares": {
      "command": "hares-mcp",
      "args": ["--enable=shell"],
      "env": {
        "HARES_SANDBOX_RW": "/home/YOU/.gitconfig:/home/YOU/.cache"
      }
    }
  }
}
```

Replace `/home/YOU` with your actual home directory (or use `$(realpath -m ~/.gitconfig):$(realpath -m ~/.cache)` in your shell rc instead — see [Common sandbox additions](#cli-flags)).

**2.** Deny native Bash in `.claude/settings.json`:

```json
{
  "permissions": {
    "deny": ["Bash"],
    "allow": ["mcp__hares__*"]
  }
}
```

**3.** Tell Claude what changed in `CLAUDE.md`:

```md
## Shell execution
Use `mcp__hares__execute_command` for all shell work.

By default: git push, SSH connections, HTTP writes (curl -X POST/PUT/DELETE),
docker push, and similar remote-write operations will prompt for your approval
before running. Everything else — builds, tests, file edits, git status/log/diff
— runs immediately without interruption.

Nothing is hard-blocked by default. Add --deny to the server config for that.
```

**4.** Verify the install:

```sh
hares-mcp doctor
```

That's it. Most commands run without interruption. `git push origin main` triggers an approval dialog. Restart Claude Code to pick up the config.

**Default approval-required operations** (out of the box, no extra configuration):
`git push`, `git remote set-url`, `ssh`, `scp`, `curl -X POST/PUT/DELETE/PATCH`,
`wget --post-data/--post-file`, `gh` pr/issue/release/repo mutations, `docker push`,
`npm publish`, `twine upload`, `cargo publish`, `pip install --index-url`.

**Nothing is hard-denied by default.** To add hard blocks (e.g. `sudo`), use `--deny` in the server args. Suggested starting point:

```json
"args": ["--enable=shell", "--deny=sudo *,git push --force*"]
```

→ More details: [Claude Code integration](#claude-code)
→ Automated flows, HPC, Python library: [Integrations](#integrations)

---

## Integrations

### Claude Code

The default install above gives you:
- **Kernel-enforced filesystem sandbox** — no writes outside the project
- **Resource caps** — runaway builds can't OOM the machine
- **Command policy with elicitation** — `git push`, HTTP writes, and similar operations trigger a blocking user-approval dialog; hard-blocked commands are rejected outright
- **Network egress control** — add `--network-allow=...` to restrict which external hosts the agent can reach

**Adjusting the policy** in your `.mcp.json` `args`:

```jsonc
// Hard-block commands (no approval path, ever):
"--deny=sudo *,git push --force*,rm -rf /*"

// Change what requires approval (replaces the default list):
"--suspect=*git push*,*ssh *,*docker push*"

// Disable approval prompts entirely — everything runs (evaluate Hares risk-free):
"--suspect="

// Restrict network — only these endpoints reachable (requires slirp4netns):
"--network-allow=github.com:443,pypi.org:443"
```

**Also useful for Claude Code:**
- `--enable=fs` or `--enable=fs+shell` to route filesystem reads/writes through Hares too (see [Filesystem isolation](#filesystem-isolation))
- `--enable=lsf` or `--enable=slurm` for HPC cluster access

### Automated multi-LLM flows

No human in the loop means no elicitation — so the suspicious list becomes irrelevant. Put what you don't want into `--deny`; everything else runs.

```sh
# Fully hermetic — no external connectivity:
hares-mcp --enable=shell \
  --network=off \
  --deny="git push*,curl -X POST*,curl -X PUT*,curl -X DELETE*"

# With allowed external endpoints:
hares-mcp --enable=shell \
  --network-allow=pypi.org:443,github.com:443 \
  --deny="git push*"

# Multiple agents sharing one resource budget:
HARES_COORDINATION_DIR=/tmp/hares-run \
  hares-mcp --enable=shell --network=off
```

The key difference from interactive use: there's no approval-required tier. Commands either run or they don't. The default approval list (git push, ssh, curl writes, etc.) still applies, but since there's no human to answer the dialog, it fails closed — those commands are denied. Use `--deny` instead of relying on the approval tier, and use `--suspect=""` to disable the approval tier entirely. Handle the underlying risk via credentials (read-only tokens) and network policy.

The same fail-closed rule applies to `request_path_access`: no human means no approval, so it's always denied under automation. Predeclare any outside-ceiling paths an automated flow needs via `HARES_SANDBOX_RW`/`RO` at startup instead of requesting them at runtime.

### Already using an MCP filesystem with Claude Code?

Hares is a drop-in replacement. You're already paying MCP latency cost for filesystem ops — swapping gives you hardened, symlink-aware path validation (plus kernel-enforced scope when you run `fs+shell`) plus defense against the [CVE-2025-53109 / CVE-2025-53110](https://nvd.nist.gov/vuln/detail/CVE-2025-53109) class of path-validation bugs that shipped in the reference filesystem MCP.

```jsonc
// Before:
{ "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"] } }

// After:
{ "hares-fs": { "command": "hares-mcp", "args": ["--enable=fs"], "env": { "HARES_FS_CEILING": "/path" } } }
```

Add `--read-only` for observe-only agents.

### Python library

Import the engine directly — same kernel-enforced caps, no MCP layer needed for test runners, CI scripts, or orchestration code.

```python
import asyncio, os
from hares.runner import Runner
from hares.sandbox import load_sandbox_config

runner = Runner(
    max_concurrent=2, mem_limit_mb=8000, cpu_limit_sec=1800,
    sandbox=load_sandbox_config(default_cwd=os.getcwd()),
)

result = await runner.execute("pytest -q", timeout=300)
# {"exit_code": 0, "stdout": "...", "killed_reason": None, ...}
```

For HPC cluster jobs:

```python
from hares.cluster.slurm import SlurmExecutor, load_slurm_config
from hares.cluster import JobSpec

executor = SlurmExecutor(cfg=load_slurm_config())
result = await executor.execute_blocking(
    JobSpec(command="my_sim --config $SLURM_ARRAY_TASK_ID",
            array="1-100", resource_spec="--mem=8192 --time=02:00:00"),
    timeout_sec=7200,
)
# result["tasks"]["42"] = {"exit_code": 0, "stdout": "..."}
```

`HARES_COORDINATION_DIR` shared across library callers and MCP servers gives one global budget across all of them.

---

## Reference

### CLI flags

All flags apply to shell / fs / fs+shell modes unless noted.

**Filesystem**

| Flag | Default | Notes |
|---|---|---|
| `--enable {shell,fs,fs+shell,lsf,slurm}` | `shell` | Which tool family to expose |
| `--ceiling PATH` | `$HARES_FS_CEILING` → `$PWD` | Outer bound; rejected if under `.git/` |
| `--read-only` | off | fs: write tools not registered; shell: bwrap mounts RO |
| `--scope-id ID` | unset | Tool-name prefix for multi-instance (pattern `^[a-z][a-z0-9_]*$`) |
| `--state-file PATH` | unset | Persist active scope across restarts; requires `HARES_STATE_HMAC_SECRET` |

**Command policy**

| Flag | Default | Notes |
|---|---|---|
| `--deny PATTERNS` | none | Comma-separated globs. Matching commands rejected immediately, no elicitation. |
| `--suspect PATTERNS` | see below | Comma-separated fnmatch globs. Matching commands trigger an MCP elicitation dialog (user approves/declines). Non-interactive clients fail closed (deny). Pass `""` to disable entirely. |

Default approval-required patterns (active out of the box):
`*git push*`, `*git remote set-url*`, `*-X POST*`, `*-X PUT*`, `*-X DELETE*`, `*-X PATCH*`,
`*--request POST*`, `*--request PUT*`, `*--request DELETE*`, `*--request PATCH*`,
`*wget *--post-data*`, `*wget *--post-file*`,
`*gh pr create*`, `*gh pr merge*`, `*gh pr close*`, `*gh issue create*`, `*gh issue close*`,
`*gh release create*`, `*gh release delete*`, `*gh repo delete*`,
`*npm publish*`, `*twine upload*`, `*poetry publish*`, `*cargo publish*`,
`*ssh *`, `*scp *`, `*docker push*`,
`*pip install *--index-url*`, `*pip install *--extra-index-url*`.

**Network** (shell / fs+shell only)

| Flag | Default | Notes |
|---|---|---|
| `--network {on,off,allowlist}` | `on` | `off` = unshare netns, no external connectivity; `allowlist` is normally implied by `--network-allow` |
| `--network-allow HOST:PORT[,...]` | unset | Allowlist mode; only declared destinations reachable via nftables. Implies `--unshare-net`. Requires `slirp4netns`. |

**Sandbox tuning** (env vars; CLI flags override when set)

| Variable | Default | Notes |
|---|---|---|
| `HARES_MAX_CONCURRENT` | `2` | Per-process unless `HARES_COORDINATION_DIR` set (then global) |
| `HARES_MEM_LIMIT_MB` | `7168` | Per-subprocess `RLIMIT_AS` |
| `HARES_CPU_LIMIT_SEC` | `1200` | Per-subprocess `RLIMIT_CPU` |
| `HARES_DEFAULT_TIMEOUT_SEC` | `300` | Wall-clock timeout per command |
| `HARES_SANDBOX_DISABLED` | unset | Set to `1` to skip bwrap (loses kernel enforcement) |
| `HARES_MEM_LIMIT_MAX_MB` | ~90 % of RAM | Machine-safe ceiling (MB) for `execute_command_high_memory` runs; defaults to `machine_safe_max_mb()` (~90 % of `MemTotal`). |
| `HARES_DISABLE_CGROUP` | unset | Set to `1` to skip cgroup v2 aggregate bounding and use per-process RLIMIT only. |
| `HARES_SANDBOX_RW` | server cwd | Colon-separated extra RW mounts (gitconfig, pip cache, local tools) |
| `HARES_SANDBOX_RO` | empty | Colon-separated extra RO mounts |
| `HARES_SANDBOX_EXCLUDE` | empty | Colon-separated paths **inside** the ceiling to hide entirely (no read, no write). Absolute or ceiling-relative. |
| `HARES_SANDBOX_PROTECT` | empty | Colon-separated paths **inside** the ceiling to keep readable but never writable. Absolute or ceiling-relative. |
| `HARES_SANDBOX_NETWORK` | `on` | Overridden by `--network` flag |
| `HARES_SANDBOX_NETWORK_ALLOW` | unset | Overridden by `--network-allow` flag |
| `HARES_SLIRP4NETNS_BIN` | `slirp4netns` | Required for network allowlist mode |
| `HARES_COORDINATION_DIR` | unset | Shared flock-slot + core-pool dir across Hares processes |
| `HARES_STATE_HMAC_SECRET` | unset | Required when `--state-file` is set |
| `HARES_AUDIT_LOG` | unset | File path or `stderr` — structured JSONL of every tool call |
| `HARES_AUDIT_HMAC_SECRET` | unset | 32+ bytes → tamper-evident (HMAC-signed) audit entries |
| `HARES_AUDIT_REDACT_FIELDS` | `env` | Comma-separated arg fields to redact in the audit log; set empty to redact nothing |
| `HARES_AUDIT_MAX_VALUE_CHARS` | `500` | Per-value truncation length in the audit log |
| `HARES_EXTRA_SYSTEM_DIRS` | empty | Colon-separated extra paths added to the system-dir blocklist (see below) |
| `HARES_SANDBOX_BWRAP_BIN` | `bwrap` | Path/name of the `bwrap` binary |
| `HARES_SANDBOX_TMP_SIZE_MB` | unset | Size cap for the per-command `/tmp` tmpfs (MB) |
| `HARES_SYSTEMD_RUN_BIN` | `systemd-run` | Path/name of `systemd-run` (cgroup bounding) |
| `HARES_RSS_POLL_INTERVAL_SEC` | `2.0` | RSS monitor poll interval |
| `HARES_RSS_OVERSHOOT_RATIO` | `1.2` | RSS kill threshold as a multiple of the mem cap |

**Common sandbox additions for locally-installed tools:**

```bash
# In your shell rc:
export HARES_SANDBOX_RO="/tool:$(realpath -m ~/.local)"
export HARES_SANDBOX_RW="$(realpath -m ~/.gitconfig):$(realpath -m ~/.cache)"
```

**LSF / SLURM** (for `--enable=lsf` and `--enable=slurm`; use `HARES_LSF_*` / `HARES_SLURM_*` prefixes)

| Variable | Default |
|---|---|
| `HARES_LSF_QUEUE` / `HARES_SLURM_PARTITION` | unset (scheduler default) |
| `HARES_LSF_DEFAULT_RESOURCE_SPEC` / `HARES_SLURM_DEFAULT_RESOURCE_SPEC` | unset |
| `HARES_LSF_OUTPUT_DIR` / `HARES_SLURM_OUTPUT_DIR` | per-session tempdir |
| `HARES_LSF_POLL_INTERVAL_SEC` / `HARES_SLURM_POLL_INTERVAL_SEC` | `10` |
| `HARES_LSF_DEFAULT_TIMEOUT_SEC` / `HARES_SLURM_DEFAULT_TIMEOUT_SEC` | `86400` |
| `HARES_SLURM_ACCOUNT` | unset (SLURM only) |
| `HARES_LSF_{BSUB,BJOBS,BKILL}_BIN` / `HARES_SLURM_{SBATCH,SQUEUE,SCANCEL}_BIN` | scheduler binary names (`bsub`, `sbatch`, …) |

---

### Filesystem isolation

Every shell command runs inside a fresh bwrap mount namespace:

- **`--ro-bind / /`** — the entire host filesystem is read-only by default.
- **Active scope** — paths set via `restrict_paths` are RW (or RO with `--read-only`). The ceiling is also mounted RW when no scope is set.
- **HARES_SANDBOX_RW/RO** — extra mounts composited on top.
- **`/tmp`, `/run`** — fresh tmpfs per invocation.

Writes outside the declared scope get `EROFS: Read-only file system` — a clear, debuggable error the agent can act on.

**`restrict_paths`** narrows the writable surface mid-session:

```python
# Via MCP tool call:
await session.call_tool("hares_restrict_paths", {"paths": ["lib/parser"]})
# Now only lib/parser is writable; everything else in the ceiling is RO.
```

`get_active_paths` returns the current scope. Paths are validated against the ceiling (no `..`, no `.git/`), created if missing, and persisted to `--state-file` if configured.

**In-ceiling blacklist (`HARES_SANDBOX_EXCLUDE` / `HARES_SANDBOX_PROTECT`):**

The RW/RO mounts above are a *whitelist for paths OUTSIDE the ceiling*. The blacklist is the inverse — carve specific paths *inside* the ceiling back out, for credentials, vendored trees, or VCS state that live in the project but should never be touched by an agent:

```bash
# secrets/ and .env vanish entirely; vendor/ is readable but not writable.
export HARES_SANDBOX_EXCLUDE="secrets:.env"
export HARES_SANDBOX_PROTECT="vendor"
```

| Variable | Effect | Read | Write |
|---|---|---|---|
| `HARES_SANDBOX_EXCLUDE` | **Hidden** — the path does not exist for the agent | ✗ | ✗ |
| `HARES_SANDBOX_PROTECT` | **Read-only** — content visible, mutation rejected | ✓ | ✗ |

Entries are colon-separated, absolute or ceiling-relative, and must resolve **strictly under the ceiling** (an entry equal-to or outside the ceiling is rejected at startup — use RW/RO for outside-the-ceiling paths). Enforced on both surfaces:

- **fs mode** — path validation. Excluded paths are rejected for reads and writes and pruned from `list_directory` / `directory_tree` / `search_files`; protected paths reject writes only.
- **shell mode** — bwrap mounts. Excluded directories become a fresh tmpfs (`--tmpfs`), excluded files become `/dev/null` (`--ro-bind`), protected paths are re-mounted read-only over themselves (writes get `EROFS`).

**Deny beats allow.** The blacklist mounts are applied last, so a path stays excluded/protected even if `restrict_paths` would otherwise make it writable. A path in both lists is hidden (exclude wins). When `HARES_SANDBOX_DISABLED=1` (no bwrap), shell-side enforcement does not apply — the fs-mode Python checks still do.

**System-dir validation:** set `HARES_DISALLOW_SYSTEM_DIRS=1` to opt into strict mode — ceilings and mounts are validated against `/etc`, `/proc`, `/sys`, `/bin`, `/usr/bin`, etc. The `.git/` ceiling rejection is always on.

**Runtime path-access grants (`request_path_access`):**

Three ways the writable/readable surface can change, and who drives each one:

- `HARES_SANDBOX_RW` / `RO` (startup) — operator predeclares outside-ceiling access in server config.
- `restrict_paths` (runtime) — agent narrows the writable surface *within* the ceiling; it can never widen it.
- `request_path_access` (runtime) — agent requests *new* access **outside** the ceiling, gated by a human's click.

`request_path_access(path, mode, reason)` takes `mode` of `"ro"` or `"rw"` and an agent-authored free-text `reason`. It's registered in shell, fs, and fs+shell modes.

Every call blocks on an MCP elicitation dialog. The dialog shows the *resolved* absolute path (symlinks chased before display), the mode, and an explicit warning that the path is outside the sandbox and — if it's a directory — that the grant covers its entire subtree; the agent's `reason` is shown but visually subordinate to that warning. The human picks one of three options: **Allow once** — consumed by the next access (in shell mode, the very next `execute_command` call, whether or not that command touches the granted path, since bwrap mounts are rebuilt per command and can't tell what was actually touched; in fs mode, the moment a read/write op resolves the granted path) — **Allow for rest of session** — lives until the server process exits — or **Deny**.

Fails closed: non-interactive clients, a decline, a cancel, or any error all resolve to denied, same as the suspicious-command and high-memory elicitations. One deliberate exception: if a client returns *accept* but omits or garbles the scope field (the MCP spec makes response-schema validation a SHOULD, not a MUST), Hares reads it as the least-privileged accept — a single **Allow once** — rather than inventing a session grant. A clear accept is honored at minimum privilege; anything short of accept is denied.

Grants live in memory only. They're never written to disk and don't survive a restart; `--state-file` has no effect on them.

**Deny always wins.** A grant can never open a path blocked by `HARES_SANDBOX_EXCLUDE`, `HARES_SANDBOX_PROTECT`, the system-dir blocklist, or a `.git` directory — checked once when the grant is created (rejected before the human ever sees a dialog) and again at use time (defence in depth). In shell mode, an accepted grant becomes an extra bwrap bind mount applied *before* the exclude/protect mounts, so the kernel resolves any overlap in deny's favor. With `--read-only`, only `mode="ro"` grants are possible — an `rw` request is rejected outright.

`get_active_paths` also lists currently active grants (path, mode, lifetime).

No CLI flag governs this feature — it's intrinsically human-gated and fails closed by design — and there's no revoke tool. Use "Allow once" if you don't want a lasting grant; every grant dies on restart regardless.

---

### Resource caps and aggregate memory bounding

Every shell command is subject to three independent resource limits:

- **`RLIMIT_CPU`** — hard per-process CPU time cap (kernel-enforced, `SIGKILL` on breach).
- **`RLIMIT_AS`** — per-process virtual-address-space cap.  Applied to every subprocess individually.
- **Wall-clock timeout** — enforced by Hares's async monitor; the process group is killed on expiry.

**The RLIMIT_AS gap: multi-process memory exhaustion.**  Per-process `RLIMIT_AS` cannot bound the *aggregate* memory of a multi-process command tree.  Each child process inherits an independent AS budget, so `make -j16`, `pytest -n auto`, or a build that forks many workers can collectively exhaust host RAM far faster than Hares's 2-second RSS poll detects it.  When that happens the kernel global OOM killer fires — and it may choose to kill the MCP client session rather than the offending command.

**Cgroup v2 aggregate bounding (when available).**  On systems where cgroup v2 is mounted and `systemd --user` is running, Hares wraps every command tree in a `systemd-run --user --scope` with `memory.max` set.  The kernel OOM killer is then *scoped* to that cgroup: it kills only the command's process tree.  The Hares process and the MCP client are completely unaffected.  `RLIMIT_AS` is kept per-process as defence-in-depth.

**Fallback when cgroups/user-systemd are absent.**  Hares falls back to per-process RLIMIT + RSS poll.  This is best-effort: a fast multi-process memory bomb can exhaust RAM between polls.  Run `hares-mcp doctor` to see which mode is active.  To enable the strong guarantee: ensure cgroup v2 is mounted (`/sys/fs/cgroup/cgroup.controllers` must exist with `memory` listed) and run `loginctl enable-linger $USER` so a user-level systemd instance is always running.  Set `HARES_DISABLE_CGROUP=1` to opt out of cgroup bounding even when it is available.

**`execute_command_high_memory` — approved large-budget runs.**  Some commands (large model loads, heavy builds) legitimately need more memory than the normal `HARES_MEM_LIMIT_MB` cap.  The `execute_command_high_memory` tool lets the agent request a larger budget; Hares always prompts the user for explicit approval before running.  The approved run stays cgroup-bounded to `HARES_MEM_LIMIT_MAX_MB` (default: ~90 % of installed RAM) so even an approved high-memory command cannot take down the session.  Non-interactive clients (no elicitation support) fail closed — the command is denied.

---

### Command policy

Three tiers, evaluated in order per `execute_command` call:

```
DENY (--deny)         → structured error, no approval possible
APPROVE (--suspect)   → MCP elicitation dialog → user approves or declines
ALLOW (default)       → runs immediately
```

**Pattern syntax:** comma-separated fnmatch globs matched against the full command string as passed to `execute_command`. Patterns without wildcards are treated as substrings (`git push` → `*git push*`).

```sh
# Deny: these never run, regardless of who asks
--deny="git push --force*,sudo *,rm -rf /*"

# Require approval: dialog before running (automated clients fail closed)
--suspect="*git push*,*ssh *,*docker push*"

# Disable the approval tier entirely (only deny + allow):
--suspect=""
```

**MCP elicitation:** when a command matches an approval-required pattern, Hares sends an `elicitation/create` request to the client. Claude Code shows a blocking "Allow / Decline" dialog — the server waits for the response before proceeding. If the client doesn't support elicitation (automated flows, older clients), the command is denied (fail closed). No flag needed to switch modes; the client's capability determines behaviour.

**Pattern matching is string-based and bypassable.** `python -c "import subprocess; subprocess.run(['git','push'])"` does not contain `*git push*` and runs without triggering approval. The pattern tier is a first line of defence against direct invocations; it is not a sandbox. The bwrap filesystem boundary and network controls are independent of it and are not bypassable by command construction.

**Rejection result shape:**
```json
{
  "exit_code": -1,
  "stdout": "", "stderr": "",
  "killed_reason": "rejected_by_policy",
  "rejected_reason": "Command declined by user (pattern: '*git push*').",
  "matched_pattern": "*git push*"
}
```

---

### Network controls

Three modes:

| Mode | Flag | What it does |
|---|---|---|
| **Full** (default) | *(omit)* | Host network accessible. pip, git, curl all work. |
| **Off** | `--network=off` | Network namespace unshared — hermetic, no external connectivity. |
| **Allowlist** | `--network-allow=host:port,...` | Only declared `host:port` destinations reachable; everything else kernel-dropped via nftables. Requires `slirp4netns`. |

**How allowlist works:** `--unshare-net` creates an isolated network namespace with `CAP_NET_ADMIN`. slirp4netns provides real connectivity into it (the same mechanism rootless Docker uses). nftables rules inside the namespace enforce the allowlist: default-drop outbound, accept only declared IPs/ports plus loopback and established/related connections. Hostnames are resolved to IPs at server startup.

**What network controls do and don't cover:**

| | `--network=off` | `--network-allow=...` | `--network=on` |
|---|---|---|---|
| Exfiltration to unknown hosts blocked | ✓ | ✓ | ✗ |
| `git push` / HTTP POST to allowed hosts blocked | ✓ | ✗ | ✗ |
| Kernel-enforced | ✓ | ✓ | n/a |

The allowlist controls *who* the agent can talk to. Whether the agent can `git push` to an allowed host is a question of **credential scoping**: read-only tokens, IAM roles without write permissions, database users with only SELECT. These are already built into every system worth protecting — use them.

**slirp4netns installation:**

```sh
dnf install slirp4netns    # RHEL / Fedora
apt install slirp4netns    # Debian / Ubuntu
```

Run `hares-mcp doctor` to verify.

---

### HPC cluster

`--enable=lsf` and `--enable=slurm` expose five tools for cluster job management. These modes have a fundamentally different security model: jobs run on remote cluster nodes with the submitting user's full filesystem permissions. bwrap, RLIMIT, and active-scope enforcement do not apply on the cluster. Resource governance is the scheduler's job via `resource_spec`.

**Tools (prefix `lsf_` or `slurm_` depending on mode):**

- `execute_blocking` — submit one job, wait, return stdout/stderr/exit_code
- `submit` — submit N jobs non-blocking, return job_ids
- `wait` — wait for job_ids to finish; returns per-job results
- `cancel` — cancel job_ids
- `jobs` — list all session jobs + current status

**Job arrays** for parameter sweeps:

```python
JobSpec(
    command="my_sim --config $SLURM_ARRAY_TASK_ID",  # or $LSB_JOBINDEX for LSF
    array="1-100",
    resource_spec="--mem=8192 --time=02:00:00",
)
# result["tasks"] = {"1": {...}, "2": {...}, ..., "100": {...}}
# result["summary"] = {"done": 98, "failed": 2, "unknown": 0}
```

**RIGHT-SIZE resource_spec per job.** Schedulers prioritize jobs whose asks fit current cluster slack — a smoke test doesn't need a multi-GPU allocation. The tool description includes guidance; Claude should right-size per submission.

---

### Threat model

**What Hares enforces:**

| Guarantee | shell/fs | cluster | Notes |
|---|---|---|---|
| Filesystem writes outside scope | **Blocked — kernel** | Not applicable | `--ro-bind / /` + ceiling |
| Access to blacklisted in-ceiling paths | **Blocked — kernel (shell) / validated (fs)** | Not applicable | `HARES_SANDBOX_EXCLUDE` / `HARES_SANDBOX_PROTECT` |
| Runtime access outside scope | Only via explicit human approval (`request_path_access`); fails closed | Not applicable | In-memory grants; deny/exclude/protect/system-dirs always win |
| Resource exhaustion (CPU/RAM) | **Capped — kernel** | Use `resource_spec` | RLIMIT + RSS monitor |
| Multi-process memory exhaustion killing the session | **Blocked — cgroup `memory.max` scopes the OOM killer to the command** / fallback: best-effort RLIMIT + RSS poll | Not applicable | Requires cgroup v2 + user-systemd; `HARES_DISABLE_CGROUP=1` reverts to RLIMIT |
| Concurrency overrun | **Capped — flock slots** | Scheduler manages | `HARES_MAX_CONCURRENT` |
| Connections to non-allowlisted hosts | **Blocked — nftables** | Not applicable | Requires `--network-allow` |
| Suspicious commands without approval | **Denied / elicited** | Not applicable | `--suspect` + elicitation |
| Hard-denied commands | **Blocked** | Not applicable | `--deny` |

**What Hares does not cover:**

- Kernel exploits, namespace escapes, privilege escalation.
- Side-channel attacks (timing, `/proc` info disclosure).
- Write operations to *allowed* network hosts (credential scoping is the right tool).
- Agents burning their allotted resources (that's expected; it's the cap working as intended).
- Cluster-side filesystem access (cluster nodes are unrestricted).
- Command policy bypass via indirect invocation — `python -c "subprocess.run(['git','push'])"` does not match `*git push*`. Pattern matching is best-effort for direct invocations; bwrap and network controls are the real enforcement layers.

---

### Quirks and edge cases

- **Ceiling defaults to `$PWD`** when neither `--ceiling` nor `$HARES_FS_CEILING` is set (logged at INFO). Rejected if it resolves under `.git/`.
- **MCP-roots ceiling refinement is shell/bwrap-only.** When no explicit `--ceiling`/`$HARES_FS_CEILING` is set, Hares refines the ceiling from the MCP client's declared roots — but only the `execute_command` bwrap mounts track that refinement. The fs-tool path validation and `request_path_access`'s in-ceiling exclude/protect checks keep using the ceiling resolved at startup. The `.git` and system-dir denials are ceiling-independent and always apply; only the `HARES_SANDBOX_EXCLUDE`/`PROTECT` overlay can go stale this way. If your client's roots differ from the server's launch directory and you rely on exclude/protect, pass an explicit `--ceiling`.
- **Elicitation fails closed** — if the client doesn't support MCP elicitation, suspicious commands are denied. Automated flows: use `--deny` instead of `--suspect`.
- **RLIMIT hard limit inheritance** — if Hares runs inside another Hares process (e.g. test suite), the configured limit is silently clamped to the inherited hard limit. The result includes `applied_mem_limit_mb` showing what was actually applied.
- **`restrict_paths([])`** — legal; means "no writes". Active-scope freeze useful for operator-initiated lockdown.
- **In-flight subprocess + restrict change** — existing bwrap trees keep their original mounts; only the next `execute_command` gets the new scope.
- **`request_path_access` "once" is consumed by the next tool call on the granting surface** — in shell mode a "once" grant is consumed by the next `execute_command` call regardless of whether that command actually touches the granted path (bwrap mounts are rebuilt per command and the mount layer can't introspect actual access); in fs mode it's consumed the moment a read/write op resolves the granted path. **In `fs+shell` (combined) mode the two surfaces share one grant store**, so an "Allow once" grant is consumed by whichever comes first — an `execute_command` call (any command) *or* a file op — even if you approved it intending the other surface. If a workflow interleaves shell and file operations before using the granted path, request **"Allow for the session"** instead. This fails safe: a prematurely-consumed grant causes a clear "not covered by any active grant" denial, never silent access.
- **One tool call, two paths, one "once" grant** — a call that resolves two paths under the same once-grant (e.g. `move_file` with source and destination both under one once-grant) consumes the grant on the first path and fails on the second. Request a "session" grant for operations like this.
- **Flock-file slot lifecycle** — coordination slots are per-slot lock files under `HARES_COORDINATION_DIR`. If a process crashes mid-run, the kernel releases its flock automatically when the file descriptor closes — no manual cleanup, no leaked-semaphore problem. The operator is still responsible for the `HARES_COORDINATION_DIR` directory itself between runs with different caps.
- **slirp4netns not found** — `--network-allow` degrades to `--network=off` with a warning. Run `hares-mcp doctor` to verify the full setup.
- **`--state-file` requires `HARES_STATE_HMAC_SECRET`** — startup refuses without it (a compromised restart could replay a stale scope). Set to 32+ bytes of base64/hex.

---

## FAQ

### Why not just Docker?

Docker isolates a whole container image — you build it, ship it, and run inside a persistent container lifecycle with its own daemon. Hares wraps each command in a fresh bwrap mount namespace instead: no daemon, no image to build, no container to keep alive. Commands run in your actual dev environment — same tools, same PATH, same files — with the writable surface, resource caps, network egress, and command policy enforced per invocation rather than baked into an image. That means Hares scopes individual command executions inside your real working tree, and layers in the resource-coordination, network-allowlist, command-approval, and HPC-bridge pieces Docker doesn't provide on its own. It's complementary to Docker, not a replacement for it as a deployment or packaging tool.

---

## Project status

Hares is **0.5.x — beta**. APIs are stable enough to build on; minor versions may tweak env-var names and tool signatures. Pin the minor version in production.

Tested on Linux (RHEL 8+, Ubuntu 20.04+, Fedora). Cluster modes require LSF or SLURM binaries on `PATH` and a shared filesystem visible to both submit and execute hosts.

The test suite is roughly the size of the source itself — about 9,300 lines across `tests/`.

## Contributing

Bug reports, feature requests, and PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, test isolation policy, and the test workflow. Security issues: email rather than file a public issue.

## License

Apache-2.0. See [LICENSE](LICENSE).
