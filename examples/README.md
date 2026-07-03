# Hares examples

Working configurations and code snippets for the integrations the
README pitches. Each example is small enough to read in one sitting
and copy into your own project with one command.

## What's here

```
examples/
├── claude-code/            ← posture 1 (shell-only): route shell through Hares,
│   ├── .mcp.json           ←   keep native Read/Write/Edit
│   ├── .claude/
│   │   └── settings.json   ← deny native Bash, allow Hares' MCP tools
│   └── CLAUDE.md           ← prompt the model to use the right tool
├── claude-code-locked/     ← posture 2 (full lockdown): route shell AND file
│   ├── .mcp.json           ←   ops through Hares (--enable=fs+shell)
│   ├── .claude/
│   │   └── settings.json   ← deny Bash + Read/Write/Edit/Glob/Grep
│   └── CLAUDE.md           ← point the model at the Hares fs tools
├── hares.env.sh            ← sourcable shell-rc defaults (with the env-
│                           ←   inheritance caveat documented inline)
└── python-library/         ← non-MCP usage: import the engine directly
    ├── runner_basic.py     ← Runner: sandboxed local subprocess
    └── cluster_slurm.py    ← SlurmExecutor: HPC job submission
```

### Two postures — pick one, don't stack them

| | `claude-code/` (shell-only) | `claude-code-locked/` (full lockdown) |
|---|---|---|
| `--enable` | `shell` | `fs+shell` |
| Denied native tools | `Bash` | `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep` |
| Reads/writes confined to the project | ✗ (native tools unrestricted) | ✓ (kernel-enforced) |
| Search quality | native ripgrep-backed `Grep` | Hares `search_files` (filename glob; content search via `grep`/`rg` in a shell command) |
| Good default when | you only want to contain the "broke my dev box" shell risk | you want file reads/writes bounded to the project too |

The lockdown posture trades native content-search convenience for
read confinement — that's the real decision, so it's called out in
`claude-code-locked/CLAUDE.md` rather than chosen silently.

## How to use

### Claude Code

Copy either `examples/claude-code/` (shell-only) or
`examples/claude-code-locked/` (full lockdown) into your project root,
preserving the `.claude/` subdirectory. Edit `.mcp.json` to set
`HARES_FS_CEILING` to your project path (and, for the locked example,
adjust the `HARES_SANDBOX_*` paths). Restart Claude Code.

- Shell-only: `mcp__hares__hares_execute_command` becomes the shell
  tool; native Read/Write/Edit/Glob/Grep are unchanged.
- Full lockdown: every file op goes through `mcp__hares__*` too — see
  the tool list in `claude-code-locked/CLAUDE.md`.

The README's [Claude Code section](../README.md#claude-code)
explains what you gain in each case.

### Shell-rc defaults (`hares.env.sh`)

`hares.env.sh` collects the machine-wide `HARES_*` env vars (resource
caps, extra mounts, in-ceiling blacklist, audit log) in one sourcable
file. Read the caveat at the top first: these exports only reach the
Hares server if your editor was launched from a shell that sourced
them — for config that must be reliable regardless of how the editor
starts, put it in `.mcp.json`'s `env` block instead.

### Python library

`runner_basic.py` and `cluster_slurm.py` are runnable scripts:

```sh
python examples/python-library/runner_basic.py
python examples/python-library/cluster_slurm.py    # requires sbatch on PATH
```

They demonstrate the same engine that powers the MCP server,
embedded directly — useful for test runners, CI scripts, or any
framework code that wants kernel-enforced caps without going through
JSON-RPC.

## Other MCP hosts (Cline, Roo Code, Continue, OpenHands, custom SDK apps)

These hosts use the same `.mcp.json` schema as Claude Code, so the
config in `claude-code/.mcp.json` works as-is when copied into the
host's expected location:

| Host | Where to put `.mcp.json` |
|---|---|
| Claude Code | `<project>/.mcp.json` (or `~/.claude.json` for user-wide) |
| Cline | `<project>/.cline/mcp_settings.json` (use the `mcpServers` block) |
| Continue | `~/.continue/config.json` (`mcpServers` key) |
| Custom Anthropic SDK | wherever you load it; the MCP Python SDK accepts the same schema |

The native-tool denial pattern (`.claude/settings.json`) is
Claude-Code-specific. Other hosts have their own permission UX —
check their docs. Even without per-tool denial, registering Hares
gives you the kernel-enforced sandbox + resource caps for any tool
the agent chooses to invoke from it.
