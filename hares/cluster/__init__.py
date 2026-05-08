"""Hares cluster — cluster-job execution for HPC schedulers.

Two backends share one async core: ClusterExecutor owns the poll
loop, output capture, timeout enforcement, and session-job map;
LsfExecutor and SlurmExecutor supply argv builders and status parsers.

Both backends expose five MCP tools (PFX_execute_blocking, PFX_submit,
PFX_wait, PFX_cancel, PFX_jobs where PFX is ``lsf`` or ``slurm``).
No bwrap, no RLIMIT, no active-scope enforcement extends to cluster
nodes — the only path-safety measure is a best-effort pre-submission
ceiling check on cwd. Resource governance is delegated to the
scheduler via per-job ``resource_spec`` strings.

Library usage:

    from hares.cluster import LsfExecutor, SlurmExecutor, JobSpec
    from hares.cluster.lsf import load_lsf_config
    from hares.cluster.slurm import load_slurm_config
"""

from .base import ClusterConfigBase, ClusterExecutor, JobRecord, JobSpec
from .lsf import LsfConfig, LsfExecutor, load_lsf_config
from .slurm import SlurmConfig, SlurmExecutor, load_slurm_config

__all__ = [
    "ClusterConfigBase",
    "ClusterExecutor",
    "JobRecord",
    "JobSpec",
    "LsfConfig",
    "LsfExecutor",
    "load_lsf_config",
    "SlurmConfig",
    "SlurmExecutor",
    "load_slurm_config",
]
