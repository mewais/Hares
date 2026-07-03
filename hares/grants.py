"""In-memory store for runtime path-access grants.

``request_path_access`` (see :mod:`hares.grant_tools`) is the runtime
escape hatch that lets an agent ask a human to widen access to a path
OUTSIDE the current sandbox scope (ceiling / active scope). On
approval, a :class:`Grant` is recorded here; two other subsystems
consult this store to decide whether to allow something they would
otherwise refuse:

* :mod:`hares.fs.operations` — Python-level path validation for the
  fs tool surface (read/write ops).
* :class:`hares.runner.Runner` — bwrap mount composition for the
  shell tool surface (``execute_command``).

Design constraints (see the feature spec):

* Grants live ONLY in memory. Never persisted to disk, never survive
  a process restart — this module has no serialization code at all,
  by design.
* Two lifetimes: ``"once"`` (consumed after the next covered use)
  and ``"session"`` (lives until the process exits).
* Deny (exclude / protect / system-dir / .git) ALWAYS beats a grant.
  This module does NOT enforce that itself — it has no opinion on
  ceilings or deny lists — callers are expected to run
  :func:`hares.path_safety.validate_grant_target` before adding a
  grant AND again at use time (defense in depth); see the callers
  above.
* ONE store instance is shared across fs + shell in combined mode,
  exactly the way :class:`hares.fs.state.ScopeStateStore` is shared
  today — grants recorded via one subsystem are immediately visible
  to the other because it's the same Python object, not a copy.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from .path_safety import _is_subpath

logger = logging.getLogger(__name__)

Mode = Literal["ro", "rw"]
Lifetime = Literal["once", "session"]


@dataclass(frozen=True)
class Grant:
    """One approved runtime path-access grant.

    ``root`` is the fully resolved (symlink-chased) absolute path the
    human approved. Any path at-or-under it is covered — for a
    directory grant this means the ENTIRE subtree (this is spelled
    out explicitly in the elicitation dialog; see
    :func:`hares.policy.elicit_path_access_approval`).
    """

    root: Path
    mode: Mode
    lifetime: Lifetime


class GrantStore:
    """Per-server-process in-memory registry of active runtime grants.

    A plain ``threading.Lock`` guards the list rather than an
    ``asyncio.Lock``: the shell Runner's bwrap-mount composition and
    the fs operation handlers may in principle be invoked from
    different execution contexts, and a non-async lock is safe to
    take from either without needing an event loop. Every method is
    a handful of list-comprehension-cheap operations — the lock is
    held only across those, never across any I/O or await point.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._grants: list[Grant] = []

    def add(self, root: Path, mode: Mode, lifetime: Lifetime) -> Grant:
        """Record a new grant. Callers MUST have already validated
        ``root`` against the deny tier (see
        :func:`hares.path_safety.validate_grant_target`) — this method
        performs no safety checks of its own."""
        grant = Grant(root=root, mode=mode, lifetime=lifetime)
        with self._lock:
            self._grants.append(grant)
        logger.info(
            "request_path_access: grant added root=%s mode=%s lifetime=%s",
            root, mode, lifetime,
        )
        return grant

    def _find_covering(self, resolved: Path, *, need_write: bool) -> Optional[Grant]:
        """First grant (in insertion order) covering ``resolved`` for
        the requested access level. ``need_write=True`` only matches
        ``rw`` grants; ``need_write=False`` matches ``ro`` or ``rw``."""
        with self._lock:
            for grant in self._grants:
                if need_write and grant.mode != "rw":
                    continue
                if _is_subpath(resolved, grant.root):
                    return grant
        return None

    def covers_read(self, resolved: Path) -> bool:
        """True if some active grant (ro or rw) authorizes reading
        ``resolved``."""
        return self._find_covering(resolved, need_write=False) is not None

    def covers_write(self, resolved: Path) -> bool:
        """True if some active rw grant authorizes writing
        ``resolved``."""
        return self._find_covering(resolved, need_write=True) is not None

    def consume_once(self, resolved: Path, *, need_write: bool) -> None:
        """After a covered access has been authorized, remove the
        covering grant IF its lifetime is ``"once"``. No-op if no
        grant covers ``resolved``, or the covering grant is
        ``"session"``.

        Callers invoke this once per authorized access (see
        ``hares.fs.operations._resolve`` and
        ``Runner.execute``'s single consumption point) — see each
        call site's docstring for exactly when "one use" is deemed
        to have happened.
        """
        with self._lock:
            for i, grant in enumerate(self._grants):
                if need_write and grant.mode != "rw":
                    continue
                if _is_subpath(resolved, grant.root):
                    if grant.lifetime == "once":
                        del self._grants[i]
                        logger.info(
                            "request_path_access: once-grant consumed "
                            "root=%s mode=%s", grant.root, grant.mode,
                        )
                    return

    def consume_all_once(self) -> None:
        """Remove EVERY currently-active ``"once"`` grant, regardless
        of path. Used by the shell Runner: bwrap mounts are rebuilt
        fresh on every ``execute()`` call, so a "once" grant is
        defined to apply to exactly the next ``execute()`` call (the
        mount composition can't easily tell which specific path inside
        the sandboxed command actually got touched) — see
        ``Runner.execute`` for the single call site."""
        with self._lock:
            remaining = [g for g in self._grants if g.lifetime != "once"]
            consumed = len(self._grants) - len(remaining)
            if consumed:
                logger.info(
                    "request_path_access: %d once-grant(s) consumed by "
                    "the next execute_command call", consumed,
                )
            self._grants = remaining

    def list_active(self) -> list[dict]:
        """Snapshot of active grants as plain dicts — used both by
        ``get_active_paths`` (visibility) and by the shell Runner's
        bwrap mount composition."""
        with self._lock:
            return [
                {"path": str(g.root), "mode": g.mode, "lifetime": g.lifetime}
                for g in self._grants
            ]
