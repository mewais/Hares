"""Allow ``python -m hares`` to start the MCP server.

Delegates to the CLI dispatcher in :mod:`hares.cli` once it lands;
until then, falls back to the bare 0.1-compat shell-only path.
"""

try:
    from .cli import main  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover — pre-cli refactor stage
    from .shell.server import main  # type: ignore[no-redef]


if __name__ == "__main__":
    main()
