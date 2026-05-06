# Hares (حارس)

Hares is **guard** — Arabic for guard / protector. One MCP binary, two
tool families, uniform scope and permission semantics across both,
kernel-enforced via [bubblewrap](https://github.com/containers/bubblewrap):

* **Shell** (`execute_command`): subprocess execution, bounded by a bwrap
  mount namespace + per-process `RLIMIT_AS` / `RLIMIT_CPU` / wall-clock
  timeouts + a (now optionally cross-process) concurrency semaphore.
* **Filesystem** (`read_file`, `write_file`, `list_directory`, …): direct
  file operations, bounded by in-process path validation against a
  configured **ceiling** and a runtime-narrowable **active scope**.

The same five CLI flags mean the same thing on both sides — see
[Symmetry table](#symmetry-table). The mechanism differs (bwrap mounts
for shell, in-Python validation for fs); the user-facing semantic does not.

> **Migrating from 0.1.x?** Jump to [Migration from 0.1.x](#migration-from-01x).

---

## Table of contents

1. [Overview](#overview)
2. [What's new in 0.2.0](#whats-new-in-020)
3. [Quick start](#quick-start)
4. [CLI reference](#cli-reference)
5. [Tool surface](#tool-surface)
6. [Env var reference](#env-var-reference)
7. [System-dir policy](#system-dir-policy)
8. [Cross-process coordination](#cross-process-coordination)
9. [Active scope and `restrict_paths`](#active-scope-and-restrict_paths)
10. [bwrap mechanics](#bwrap-mechanics)
11. [Multi-instance use under flat-namespace registries](#multi-instance-use-under-flat-namespace-registries)
12. [Threat model](#threat-model)
13. [Quirks and edge cases](#quirks-and-edge-cases)
14. [Migration from 0.1.x](#migration-from-01x)

---

## Overview

Every Hares instance has a single **role**: limit what an MCP-using LLM
can do under a specific budget (filesystem reach, write authority,
subprocess resource consumption). One binary covers the role for both
tool families:

```
hares-mcp \
  --enable {shell, fs, fs+shell}      # which tool family to expose
  [--scope-id <id>]                   # tool-name prefix; scope identifier
  --ceiling <path>                    # outer bound; required (CLI or env)
  [--read-only]                       # observe-only mode
  [--state-file <path>]               # persist active scope across restarts
```

A Hares instance can therefore be:

* a **shell guard** for inspection-class commands,
* a **filesystem guard** with a per-scope writable view,
* both at once with **one shared scope** (`fs+shell`), so a single
  `restrict_paths(['lib/parser'])` call narrows both layers together.

The same surface — flags, restrict tools, ceiling semantics — is the
right one whether you're deploying Hares standalone, plugging it into
an MCP gateway, or running multiple instances under a workflow
orchestrator.

---

## What's new in 0.2.0

* **Filesystem MCP surface** alongside shell, exposing the standard
  npm-style `read_file` / `write_file` / `list_directory` / etc.
  (under a configurable ceiling).
* **`--scope-id` tool-name prefix** so two instances of Hares can
  coexist under a flat-namespace MCP registry without colliding.
* **`--ceiling` is required** for any instance with `--enable` set.
  Defaults to `$HARES_FS_CEILING` when not on the CLI; missing both
  fails fast.
* **`--read-only`** is symmetric across fs and shell. For fs: write
  tools are not registered. For shell: bwrap mounts the active scope
  read-only.
* **Runtime-narrowable active scope** via the always-registered
  `restrict_paths` and `get_active_paths` tools. State optionally
  persists to `--state-file`.
* **Cross-process subprocess throttling** when `HARES_COORDINATION_DIR`
  is set: a POSIX named semaphore enforces `HARES_MAX_CONCURRENT` as a
  GLOBAL cap across N Hares processes (instead of per-process). CPU
  cores are also handed out non-overlappingly across siblings via a
  shared `core_pool.json` allocator.
* **bwrap is REQUIRED by default**. Set `HARES_SANDBOX_DISABLED=1` to
  opt out (non-Linux, restricted userns, debugging). This is the only
  intentional behavioral break vs 0.1.
* **System-dir validation** (opt-in, off by default) via
  `HARES_DISALLOW_SYSTEM_DIRS=1`. Rejects ceilings / mounts / active
  scope under common system paths (`/etc`, `/proc`, `/dev`, …);
  extend via `HARES_EXTRA_SYSTEM_DIRS`.
* **Always-on ceiling guard**: regardless of system-dir config, a
  ceiling under `.git/` is rejected — version-control internals are
  never legitimate as a project root.
* `posix_ipc>=1.1` is now a runtime dep (gracefully optional — falls
  back to in-process semaphore when unavailable).

See [Migration from 0.1.x](#migration-from-01x) for the upgrade path.

---

## Quick start

```sh
# 0.1-compatible shell-only invocation. Only difference vs 0.1: bwrap
# is required by default (set HARES_SANDBOX_DISABLED=1 to opt out),
# and HARES_FS_CEILING must be in env (or pass --ceiling).
HARES_FS_CEILING=/work/proj hares-mcp

# Filesystem MCP, read-write under /work/proj:
HARES_FS_CEILING=/work/proj hares-mcp --enable=fs

# Filesystem MCP, read-only:
HARES_FS_CEILING=/work/proj hares-mcp --enable=fs --read-only

# Shell + FS in one process, shared active scope, scoped tool names,
# state persisted across restarts:
HARES_FS_CEILING=/work/proj hares-mcp \
  --enable=fs+shell \
  --scope-id=block_x \
  --state-file=/tmp/hares-state-block_x.json

# Two instances coexisting under a flat-namespace MCP registry
# (must use unique scope-ids):
hares-mcp --enable=fs --scope-id=src         --ceiling=/work/proj &
hares-mcp --enable=fs --scope-id=unit_tests  --ceiling=/work/proj &
```

---

## CLI reference

| Flag | Required | Default | Validation |
|---|---|---|---|
| `--enable {shell,fs,fs+shell}` | no | `shell` | choices |
| `--scope-id <id>` | no | unset (no prefix) | `^[a-z][a-z0-9_]*$` |
| `--ceiling <path>` | **yes** (CLI or env) | `$HARES_FS_CEILING` | abs-resolved; rejected if under `.git/`; rejected if under any blocklist entry when `HARES_DISALLOW_SYSTEM_DIRS=1` |
| `--read-only` | no | off | flag |
| `--state-file <path>` | no | unset (in-memory only) | abs-resolved; warning if path is under ceiling |

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

### Symmetry table

| Flag | fs behavior | shell behavior |
|---|---|---|
| `--ceiling <path>` | Outer bound for tool-call paths | Outer bound for bwrap mount namespace |
| `--scope-id <id>` | Tool-name prefix | Tool-name prefix |
| `--read-only` | Write tools NOT registered | bwrap mounts active scope as RO; subprocess writes kernel-rejected |
| `--state-file <path>` | Persists active scope | Persists active scope |

The mechanism differs; the user-facing semantic does not.

---

## Env var reference

### Operator-deploy: ceiling default

| Variable | Purpose |
|---|---|
| `HARES_FS_CEILING` | Default for `--ceiling` when not on the CLI. Operators with a single project root set this once in their shell rc. Required at startup if `--ceiling` is not passed. |

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
and CPU-affinity is the first-N cores per process — same as 0.1.

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

### Out of scope

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

* **Bare `hares-mcp` invocation requires `HARES_FS_CEILING`** in env.
  This is the migration path from 0.1: set it once in your shell rc.
  Without it (and without `--ceiling`), startup fails with a clear
  error.
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

## Migration from 0.1.x

The only user-visible breaking changes:

1. **bwrap is required by default.** Previously `HARES_SANDBOX_MODE=bwrap`
   was opt-in. To restore 0.1 behavior, set `HARES_SANDBOX_DISABLED=1`.
   Legacy `HARES_SANDBOX_MODE={none,off,false,0}` is still honored.
2. **`--ceiling` is required** for any `--enable` mode. To preserve
   0.1's CLI-flag-free invocation, set `HARES_FS_CEILING=<path>` in
   your shell rc.
3. **Restrict tools are always registered** for any fs/shell
   instance. They were not present in 0.1. They're zero-cost when
   no agent has them in its allowlist.

Everything else is backward-compatible:

* The single `execute_command` tool name and signature are unchanged
  (and still unprefixed when `--scope-id` isn't set).
* All `HARES_*` env vars from 0.1 work identically.
* `posix_ipc` is a soft dep — not having it gracefully degrades to
  in-process semaphore.

### Concrete diff for `~/.bashrc`

```diff
+ # 0.2 requires bwrap by default; opt out for non-Linux / debug.
+ # export HARES_SANDBOX_DISABLED=1
+
+ # 0.2 requires --ceiling or HARES_FS_CEILING. Set once for all instances:
+ export HARES_FS_CEILING=$HOME/work
+
+ # Optional: opt in to system-dir validation in production.
+ # export HARES_DISALLOW_SYSTEM_DIRS=1
+ # export HARES_EXTRA_SYSTEM_DIRS=/opt:/var/log
+
+ # Optional: cross-process throttling across N Hares (set this from
+ # an orchestrator that spawns multiple instances; standalone
+ # single-instance deploys can leave it unset).
+ # export HARES_COORDINATION_DIR=/tmp/hares-coord-shared

  # Existing 0.1 vars, all still honored:
  export HARES_MAX_CONCURRENT=2
  export HARES_MEM_LIMIT_MB=7168
  export HARES_CPU_LIMIT_SEC=1200
- export HARES_SANDBOX_MODE=bwrap        # default in 0.2; redundant
  export HARES_SANDBOX_RW=$HOME/work:/tmp
```

### MCP client config diff

For a single-instance shell deploy, no changes needed:

```json
{
  "mcpServers": {
    "shell": {
      "command": "hares-mcp",
      "env": { "HARES_FS_CEILING": "/work/proj" }
    }
  }
}
```

For a multi-instance deploy under an orchestrator, add `--scope-id`
per instance and have the orchestrator export
`HARES_COORDINATION_DIR` to all spawned instances.

---

## License

Apache-2.0.
