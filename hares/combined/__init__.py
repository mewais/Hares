"""Combined fs+shell tool family — one MCP server instance exposing
both filesystem operations and ``execute_command`` under a SHARED
``--scope-id`` prefix and SHARED active scope.

A single ``restrict_paths`` call narrows BOTH the fs path validation
AND the shell bwrap mounts in one shot. Useful for agents that need
both write access AND subprocess execution bounded to the same scope
(e.g. a code-edit-then-run workflow that writes source files AND
runs tests under the same restriction).
"""

from .server import serve  # noqa: F401

__all__ = ["serve"]
