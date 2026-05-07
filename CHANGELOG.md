# Changelog

All notable changes to Hares are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/).

## [0.2.3] — 2026-05-06

Round-4 reviewer findings against 0.2.2:

### Security

* **Power-loss durability gap (security-engineer M.1 [HIGH]).**
  ``ScopeStateStore._save`` previously did
  ``tmp.write → fsync(tmp_fd) → os.replace(tmp, state_file)``
  without fsyncing the parent directory afterwards. On
  ext4(data=ordered) and XFS, the rename's directory-entry update
  can reach disk AFTER the inode data; power loss in that window
  leaves the parent dir pointing at the OLD inode while both files
  exist as durable, rolling seq back below what the auditor last
  observed → false-positive REPLAY alert (or, worse, silent scope
  widening). 0.2.3 opens the parent dir with O_RDONLY|O_DIRECTORY
  + fsyncs it after every os.replace. POSIX-compliant, cheap;
  no-op on platforms without O_DIRECTORY support.

* **Refuse-to-start when stateful without HMAC secret
  (security-adversarial #3 [MEDIUM]).** Pre-fix
  ``HARES_STATE_HMAC_SECRET`` was strongly recommended for
  multi-process / restart-tolerant deployments but not enforced.
  An attacker who could SIGTERM the Hares process got a
  wide-open scope window: the new process couldn't verify the
  prior signed state (different per-process random fallback
  secret) so it fell back to empty scope (seq=0, no
  restrictions) until the next restrict_paths call. 0.2.3 makes
  ``--state-file`` + unset ``HARES_STATE_HMAC_SECRET`` a hard
  startup error so the misconfigured-deploy case fails loudly
  instead of silently degrading replay-defense.

### Tests

* `tests/test_round4_durability.py`: structural test asserting
  `_save` fsyncs the parent-dir fd; subprocess test asserting
  the CLI refuses to start with --state-file but no HMAC secret;
  positive test that pinning the secret allows startup.

## [0.2.2] — 2026-05-06

Closes a HIGH finding against 0.2.1's seq design: the seq counter
alone is insufficient because an attacker with state-file write
access can fast-forward to ``seq=10⁸``, and any consumer expecting
``seq=4`` accepts. 0.2.2 adds an HMAC over the canonical
(version, scope_id, ceiling, seq, sorted_paths) payload so the
attacker can't forge a passing (seq, paths) tuple without knowing
the secret. Out-of-process verifiers should now enforce
``seq == expected_next`` AND verify the HMAC.

### Added

* **Per-process HMAC signing for the state file.** New
  ``HARES_STATE_HMAC_SECRET`` env var (operator-pinned; expected to
  be 32+ bytes of base64/hex). When unset, Hares generates a random
  per-process secret with a one-time WARNING; this defends the
  in-process case but breaks cross-process verification (the next
  process has a fresh secret + can't verify the prior file → falls
  back to empty scope on load). Out-of-process verifiers read the
  same env var to verify HMACs received over the MCP protocol.
* **`get_active_paths` reply now includes `hmac` + `version`** so
  any out-of-process verifier can confirm the (paths, seq) tuple is
  authentic. Use together with strict ``seq == expected_next``
  enforcement for end-to-end replay-and-forge defense.
* **`restrict_paths` reply now includes `hmac` + `version`** for
  symmetric verifiability of the just-set scope.
* **WAL recovery on load.** `_save` writes through `state.json.tmp`
  (fsync) then renames to `state.json`. A crash between fsync and
  rename used to roll the seq back below what an external consumer
  last observed (false-positive replay alert at the next get). The
  new load preferentially commits the `.tmp` file when its seq is
  higher AND its HMAC verifies.
* **`hares.fs.state.canonical_hmac_payload` + `compute_state_hmac`
  exposed** so out-of-process verifiers can compute the same
  canonical bytes Hares signs over.

### Changed

* **State file format bumped to version 3** — adds mandatory
  ``hmac`` field and makes ``seq`` mandatory (previously
  ``data.get("seq", 0)`` defaulted to 0 when missing, silently
  degrading replay-defense; round-3 [security-engineer D.3]).
  Files written by 0.2.0 / 0.2.1 trigger the existing version-
  mismatch warn-and-rebuild on first start.
* **`active_paths` is now sorted** before HMAC + persistence so
  the canonical bytes don't depend on insertion order. Already-
  sorted output is also nicer for human inspection of the state
  file.

### Security

* **seq fast-forward defense.** Pre-fix the documented consumer
  contract was "reject when seq is BELOW expected" — an attacker
  writing seq=10⁸ fast-forwarded past any expected value. With
  HMAC signing the attacker can't write any (seq, paths) without
  the secret, and the documented consumer contract is now
  ``seq == expected_next`` (strict next-step), not
  ``seq >= expected``. Out-of-process verifiers should enforce
  the strict-next-step rule alongside HMAC verification.

## [0.2.1] — 2026-05-06

### Added

* **Sequence-numbered `restrict_paths` (replay-defense for the
  scope state).** `ActiveScope` gains a monotonic `seq: int` counter
  that increments by 1 on every successful `restrict_paths` call.
  Both `restrict_paths` and `get_active_paths` now return the seq
  alongside `active_paths`. Out-of-process consumers can track the
  latest seq they expected and reject any `get_active_paths` reply
  whose seq is below that threshold — closes the silent-replay
  window where an attacker with state-file write access could swap
  a tighter scope back to a stale, looser one between an agent's
  last `restrict_paths` and the consumer's read. (Note: this
  contract was strengthened in 0.2.2 to ``seq == expected_next``
  + HMAC verification; the seq-only "below threshold" check
  documented here was insufficient.)

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
