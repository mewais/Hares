# Changelog

All notable changes to Hares are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/).

## [0.2.1] — 2026-05-06

### Added

* **Sequence-numbered `restrict_paths` (replay-defense for the
  scope state).** `ActiveScope` gains a monotonic `seq: int` counter
  that increments by 1 on every successful `restrict_paths` call.
  Both `restrict_paths` and `get_active_paths` now return the seq
  alongside `active_paths`. External auditors (e.g. Bunyan's
  `seal_bundle` cross-check) can track the latest seq they expected
  and reject any `get_active_paths` reply whose seq is below that
  threshold — closes the silent-replay window where an attacker
  with state-file write access could swap a tighter scope back to
  a stale, looser one between an agent's last `restrict_paths` and
  the auditor's read.

* **`restrict_paths` `expected_seq` argument (compare-and-swap).**
  Optional integer kwarg; if provided, the call refuses with a
  structured `{"error": "scope_seq_mismatch", "expected_seq": …,
  "current_seq": …}` reply unless the current seq matches. Lets
  multiple agents sharing one scope race-safely tighten only when
  no one else has changed it since their last read.

* **`hares.fs.state.ScopeSeqMismatch` exception.** Raised by
  `ScopeStateStore.set` on CAS failure; the tool dispatcher catches
  it and returns the structured error response described above.

### Changed

* **State file format bumped to version 2** — adds the `"seq": N`
  field. Files written by 0.2.0 (version=1) trigger the existing
  version-mismatch warn-and-rebuild path on first start; agents
  re-call `restrict_paths` and the new seq begins at 1. No data
  loss because the active scope was advisory only — the ceiling +
  bwrap mounts are the kernel-enforced bound.

* **`get_active_paths` reply shape** now includes `"seq"`. Old
  consumers that only read `"active_paths"` keep working unchanged;
  new consumers gain replay-defense by checking the seq.

## [0.2.0] — 2026-05-05

This is a substantial reshape of Hares: one binary now hosts BOTH a
shell MCP and a filesystem MCP, with uniform scope/permission
semantics across both. See README's [Migration from 0.1.x](README.md#migration-from-01x).

### Added

* **Filesystem MCP surface** (`--enable=fs`) — exposes the standard
  `read_file`, `read_text_file`, `read_multiple_files`,
  `list_directory`, `list_directory_with_sizes`, `directory_tree`,
  `search_files`, `get_file_info`, `write_file`, `edit_file`,
  `create_directory`, `move_file`. Reads bounded by `--ceiling`,
  writes bounded by the active scope.
* **Combined fs+shell mode** (`--enable=fs+shell`) — both tool
  families in one process, sharing one active scope. A single
  `restrict_paths` call narrows both layers together.
* **`--scope-id` tool-name prefix** — disambiguates multi-instance
  deploys under a flat-namespace MCP registry. All tool names
  exposed by an instance get prefixed when set; bare names when
  unset (0.1-compat).
* **`--ceiling`** — outer bound for any path the instance can
  touch. Required (CLI or `HARES_FS_CEILING` env). Validated to
  not cover `.git/` always; validated against the system-dir
  blocklist when `HARES_DISALLOW_SYSTEM_DIRS=1`.
* **`--read-only`** — symmetric across fs and shell. fs: write
  tools not registered. shell: bwrap mounts active scope as RO;
  subprocess writes kernel-rejected.
* **`--state-file`** — atomic JSON persistence of the active
  scope across server restarts. Crash-recoverable (corrupt /
  scope-id-mismatched / ceiling-mismatched files fall back to
  empty scope with a warning log).
* **`restrict_paths(paths)` and `get_active_paths()`** runtime
  tools — always registered for any fs/shell/combined instance.
  Set-replace semantics, mkdir-p side effect on missing paths,
  traversal/escape rejection, system-dir validation when strict
  mode is on. For shell: re-spawns bwrap on next `execute_command`
  with the new mount list.
* **Cross-process subprocess throttling** via
  `HARES_COORDINATION_DIR` — POSIX named semaphore enforces
  `HARES_MAX_CONCURRENT` as a GLOBAL cap across N Hares processes.
  Plus a shared `core_pool.json` allocator (fcntl-locked) hands
  out non-overlapping CPU slices to sibling Hares processes.
  Falls back to in-process semaphore when `posix_ipc` isn't
  available.
* **`HARES_DISALLOW_SYSTEM_DIRS=1`** opt-in strict mode — rejects
  ceilings / mounts / active-scope entries under common system
  paths (`/etc`, `/proc`, `/dev`, `/bin`, `/sbin`, `/usr/bin`,
  `/usr/sbin`, `/boot`, `/root`, `/lib*`).
* **`HARES_EXTRA_SYSTEM_DIRS`** — colon-separated additions to the
  blocklist when strict mode is on (additive only, no override).
* **`HARES_FS_CEILING`** — default for `--ceiling` when not on the CLI.
* **`HARES_SANDBOX_DISABLED`** — opt-out escape for the new
  bwrap-required default. Set to `1` to restore 0.1 sandbox-off
  behavior.
* `posix_ipc>=1.1` runtime dep (soft — graceful in-process
  fallback when missing).
* Comprehensive README rewrite (1,000+ lines) covering every
  flag, env var, system-dir policy, coordination semantics,
  active-scope tool contract, bwrap mechanics, multi-instance
  patterns, threat model, quirks, and migration guide.
* `CHANGELOG.md` (this file).

### Changed

* **bwrap is REQUIRED by default.** This is the single intentional
  behavioral break vs 0.1. Set `HARES_SANDBOX_DISABLED=1` to opt out
  for non-Linux, restricted-userns containers, or debugging. Legacy
  `HARES_SANDBOX_MODE={none,off,false,0}` still honored.
* **`HARES_MAX_CONCURRENT` is now a GLOBAL cap when
  `HARES_COORDINATION_DIR` is set** (vs per-process unconditionally
  in 0.1). Per-process semantics preserved when coord dir unset.
* **Reorganized package layout** — `hares.server` moved to
  `hares.shell.server`. Imports updated; the entry point
  `hares-mcp` is unchanged. Public API
  (`hares.Runner`, `hares.__version__`) preserved.

### Deprecated

* `HARES_SANDBOX_MODE` — replaced by `HARES_SANDBOX_DISABLED` (see
  Changed). Legacy values `{none,off,false,0}` still honored but no
  longer documented; remove from your environment when convenient.

### Security

* Always-on ceiling guard rejects `.git/` as a ceiling regardless of
  strict-mode config. Version-control internals are never legitimate
  as a write-enabled project root.
* Active-scope mechanism enforces caller-decided write narrowing
  symmetrically across fs and shell tool families. A shell-equipped
  agent cannot bypass an fs-tool restriction by spawning a
  subprocess; the same active scope drives both layers.
* State-file mismatch checks (scope-id, ceiling) defend against
  accidentally reusing a stale state file from a differently-
  configured prior instance.

## [0.1.0] — initial release

* Single shell MCP server exposing `execute_command`.
* Per-process `RLIMIT_AS`, `RLIMIT_CPU`, wall-clock timeout, RSS
  monitor, in-process concurrency semaphore, CPU affinity pinning.
* Optional bwrap sandbox via `HARES_SANDBOX_MODE=bwrap` (off by
  default).
* Pre-flight rewriter for known overcommit patterns
  (`pytest -n auto`, `make -j`, `cargo build --jobs N`,
  `ninja -j N`).

[0.2.0]: https://github.com/.../releases/tag/v0.2.0
[0.1.0]: https://github.com/.../releases/tag/v0.1.0
