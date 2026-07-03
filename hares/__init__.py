"""Hares (حارس) — resource-aware command-execution MCP server.

Guards the host from runaway subprocesses by enforcing per-process
memory caps (RLIMIT_AS), CPU time caps (RLIMIT_CPU), wall-clock
timeouts, and a global concurrency semaphore. Designed to be plugged
into any MCP-aware client as a drop-in replacement for an
unconstrained shell tool.
"""

__version__ = "0.5.0"

from .runner import Runner

__all__ = ["Runner", "__version__"]
