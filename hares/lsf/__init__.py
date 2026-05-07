"""Hares LSF — cluster job execution via IBM Platform LSF.

Exposes five MCP tools for submitting, monitoring, and cancelling
cluster jobs. No bwrap, no RLIMIT, no active-scope enforcement —
those guarantees require control over the execution environment and
do not extend to cluster nodes. The only path-safety measure applied
is a best-effort pre-submission ceiling check on the job's working
directory.

Resource governance is delegated entirely to LSF via the
``resource_spec`` argument (e.g. ``rusage[mem=8192] span[hosts=1]``)
and LSF's own scheduler policy.
"""

from .executor import JobSpec, LsfConfig, LsfExecutor, load_lsf_config

__all__ = ["JobSpec", "LsfConfig", "LsfExecutor", "load_lsf_config"]
