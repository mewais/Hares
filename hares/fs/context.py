"""Per-tool-call context bundle for fs operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..path_safety import DenyLists
from ..grants import GrantStore
from .state import ActiveScope


@dataclass(frozen=True)
class OpContext:
    """Per-tool-call context for fs operations: the instance ceiling,
    the current active scope, the resolved in-ceiling deny lists, and
    the runtime request_path_access grant store. Built once per tool
    call at the server dispatch site (ceiling/deny/grants are stable
    for the server's life; scope is re-read per call)."""

    ceiling: Path
    scope: ActiveScope
    deny: Optional[DenyLists] = None
    grants: Optional[GrantStore] = None
