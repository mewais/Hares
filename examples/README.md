# Hares examples

Working configurations and code snippets for the integrations the
README pitches. Each example is small enough to read in one sitting
and copy into your own project with one command.

## What's here

```
examples/
├── claude-code/            ← MCP-host integration: drop-in for Claude Code
│   ├── .mcp.json           ← register Hares as a project-level MCP server
│   ├── .claude/
│   │   └── settings.json   ← deny native Bash, allow Hares' MCP tools
│   └── CLAUDE.md           ← prompt the model to use the right tool
└── python-library/         ← non-MCP usage: import the engine directly
    ├── runner_basic.py     ← Runner: sandboxed local subprocess
    └── cluster_slurm.py    ← SlurmExecutor: HPC job submission
```

## How to use

### Claude Code

Copy `examples/claude-code/` into your project root (preserving the
`.claude/` subdirectory). Edit `.mcp.json` to set
`HARES_FS_CEILING` to your project path. That's it — restart Claude
Code and `mcp__hares__hares_execute_command` is the new shell tool.

The README's [Use with Claude Code section](../README.md#claude-code-lose-nothing-gain-a-lot)
explains what you gain by routing shell through Hares while keeping
the native Read/Write/Edit/Glob/Grep tools.

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
