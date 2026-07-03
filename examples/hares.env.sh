# Hares environment — sourcable from your shell rc.
#
#   source /path/to/hares/examples/hares.env.sh
#
# ─────────────────────────────────────────────────────────────────────
# IMPORTANT: does the Hares MCP server actually see these?
#
# These exports only reach Hares if the process that launches the MCP
# server inherited them. That happens when you start your editor/agent
# from a terminal whose shell already sourced this file. A GUI-launched
# editor (Dock/Start-menu/desktop icon) typically does NOT source your
# rc, so the server starts without them.
#
# The robust place for per-project config is the "env" block of
# .mcp.json (see examples/claude-code*/.mcp.json) — that is passed to
# the server explicitly by the MCP host and does not depend on how the
# editor was launched. Use this shell-rc file for machine-wide defaults
# you want in every project AND that you always launch from a terminal;
# use .mcp.json for anything that must be reliable.
# ─────────────────────────────────────────────────────────────────────

# Resource caps (per subprocess unless a coordination dir is set).
export HARES_MEM_LIMIT_MB=8000
export HARES_CPU_LIMIT_SEC=1800
export HARES_MAX_CONCURRENT=2

# Share ONE concurrency budget + core pool across every Hares process
# on this box (multiple parallel agent sessions). Omit for per-process
# budgets. The operator is responsible for clearing this directory
# between runs that use different caps.
# export HARES_COORDINATION_DIR="$HOME/.cache/hares/coord"

# Extra mounts OUTSIDE the project ceiling. Colon-separated. Use
# realpath -m so a missing path still expands to an absolute one.
#   RW: things a build legitimately writes (caches, gitconfig).
#   RO: read-only tools/toolchains the agent should see but not modify.
export HARES_SANDBOX_RW="$(realpath -m ~/.gitconfig):$(realpath -m ~/.cache)"
export HARES_SANDBOX_RO="$(realpath -m ~/.local):/opt/toolchains"

# In-ceiling blacklist — carve sensitive paths back OUT of the project.
#   EXCLUDE: hidden entirely (no read, no write).
#   PROTECT: readable but never writable.
# Entries are relative to the ceiling (or absolute) and must resolve
# strictly under it.
export HARES_SANDBOX_EXCLUDE="secrets:.env"
export HARES_SANDBOX_PROTECT="vendor"

# Structured JSONL audit of every tool call. A file path, or "stderr".
# export HARES_AUDIT_LOG="$HOME/.cache/hares/audit.jsonl"
