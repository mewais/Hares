# Project guidance for Claude — full-lockdown posture

Every filesystem and shell operation is routed through Hares. The
native `Bash`, `Read`, `Write`, `Edit`, `Glob`, and `Grep` tools are
denied in `.claude/settings.json`; use the `mcp__hares__*` tools
instead. All of them are bounded by the kernel-enforced bwrap sandbox
and cannot touch anything outside the ceiling
(`HARES_FS_CEILING`).

> This is the strict counterpart to the shell-only example in
> `examples/claude-code/`. Pick one — don't apply both. If you only
> want to contain the "Claude broke my dev box" risk, the shell-only
> example is the gentler default; use this one when you want reads and
> writes confined to the project too.

## Shell execution

Use `mcp__hares__execute_command` for all shell work. It runs under
the bwrap sandbox plus `RLIMIT_AS` / `RLIMIT_CPU` caps and shares one
global concurrency budget with any other Claude session on this box.

Right-size resource asks per call when you can:

- Inspection (`ls`, `git status`): `mem_limit_mb=256 cpu_limit_sec=30`
- Test runs / small builds: `mem_limit_mb=4096 cpu_limit_sec=600`
- Heavy builds / simulators: omit the override (operator default
  `HARES_MEM_LIMIT_MB` applies), or if a command is OOM-killed, retry
  via `mcp__hares__execute_command_high_memory` (asks the user to
  approve a larger allocation).

## File operations

Read, search, and inspect with:

- `mcp__hares__read_file` / `read_text_file` / `read_multiple_files`
- `mcp__hares__list_directory` / `list_directory_with_sizes` /
  `directory_tree`
- `mcp__hares__search_files` — glob search under a path (this replaces
  native Glob/Grep; it matches filenames, not file contents, so for
  content search run `grep`/`rg` via `execute_command`)
- `mcp__hares__get_file_info`

Create and modify with:

- `mcp__hares__write_file` / `edit_file`
- `mcp__hares__create_directory` / `move_file`

Writes outside the active scope are rejected by the kernel with
`EROFS` — a clear, debuggable error. Narrow the writable surface
mid-session with `mcp__hares__restrict_paths`, and inspect the current
scope (and any active grants) with `mcp__hares__get_active_paths`.

## Needing something outside the project

If a task legitimately needs to read or write a path outside the
ceiling (a sibling checkout, a config file in `$HOME`), call
`mcp__hares__request_path_access` with the path, `mode` (`ro` or
`rw`), and a short reason. The user gets a blocking dialog and chooses
**Allow once**, **Allow for the session**, or **Deny**. Don't ask for
broad grants you don't need — request the narrowest path and `ro`
whenever reading is enough.
