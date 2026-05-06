"""Hares filesystem tool family — read/write/list/etc. with active-
scope validation, plus the shared ``restrict_paths`` runtime-
narrowing tools.

Per-instance configuration is set by the CLI entry point
(:mod:`hares.cli`) and dispatched via :func:`hares.fs.server.serve`.
"""

from .server import serve  # noqa: F401

__all__ = ["serve"]
