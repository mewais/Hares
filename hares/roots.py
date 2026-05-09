"""Derive the bwrap ceiling from MCP roots declared by the client.

When no explicit ``--ceiling`` or ``$HARES_FS_CEILING`` is configured,
Hares defaults to ``$PWD`` at server spawn time.  That default is wrong
when Claude Code (or another MCP client) spawns the server with a
different working directory than the project the user has open.

The fix: after the MCP session is initialised, the server requests the
client's declared roots (workspace directories) via ``roots/list``.
Claude Code returns the project root(s) currently open.  Hares derives
a ceiling from those roots and applies it to the Runner, replacing the
``$PWD`` guess with something accurate.

Resolution order (unchanged):
  --ceiling / $HARES_FS_CEILING  →  explicit; roots never override
  roots (this module)            →  used only when the above are absent
  $PWD                           →  fallback when roots are empty/invalid

Design decisions
----------------
* Multiple roots → common ancestor.  If the user has two workspace
  folders open under the same tree (``/proj/a`` and ``/proj/b``), the
  common ancestor ``/proj`` becomes the ceiling.  The result is validated
  through ``validate_ceiling()``; if it's too broad or otherwise invalid
  the fallback ($PWD) is kept.

* Roots only applied at ``initialize`` time.  Dynamic changes via
  ``notifications/roots/list_changed`` are ignored for now: the ceiling
  is fixed once the session is live.  Add dynamic support later if needed.

* Failures are always silent and graceful.  If the client does not
  support roots, returns an empty list, or the derived ceiling is invalid,
  the existing ceiling is kept unchanged.  Nothing ever breaks.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


async def derive_ceiling_from_roots(session) -> Optional[Path]:
    """Request roots from the MCP client and return a derived ceiling path.

    Returns ``None`` if roots are unavailable, empty, or result in an
    invalid ceiling.  Caller keeps the existing ceiling in that case.

    ``session`` is the ``mcp.server.session.ServerSession`` instance
    available via ``mcp.server.request_context.get().session`` inside
    any MCP handler.
    """
    try:
        result = await session.list_roots()
    except Exception as exc:
        logger.debug(
            "list_roots failed (client may not support roots): %s", exc,
        )
        return None

    roots = getattr(result, "roots", None) or []
    if not roots:
        logger.debug("Client returned empty roots list; keeping existing ceiling.")
        return None

    paths: list[Path] = []
    for root in roots:
        uri: str = getattr(root, "uri", "") or ""
        if not uri.startswith("file://"):
            logger.debug("Ignoring non-file root URI: %r", uri)
            continue
        # file:///path/to/dir → /path/to/dir
        path = Path(uri[7:]).resolve(strict=False)
        paths.append(path)

    if not paths:
        logger.debug("No usable file:// roots; keeping existing ceiling.")
        return None

    if len(paths) == 1:
        candidate = paths[0]
    else:
        try:
            candidate = Path(os.path.commonpath([str(p) for p in paths]))
        except ValueError:
            # Different drives (Windows) or other edge case.
            logger.debug("Could not find common path for roots; keeping existing ceiling.")
            return None

    # Validate through the same path-safety gate the CLI uses.  This
    # rejects .git/ roots, over-broad system dirs, etc.
    try:
        from .path_safety import validate_ceiling
        validate_ceiling(candidate)
    except Exception as exc:
        logger.info(
            "Roots-derived ceiling %s failed validation (%s); "
            "keeping existing ceiling.",
            candidate, exc,
        )
        return None

    return candidate
