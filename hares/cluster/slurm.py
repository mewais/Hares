"""SLURM backend.

Status-query strategy: ``squeue --noheader -j ID -o '%T'`` first.
While the job is in the queue (queued, running, completing),
squeue prints its native state. Once the job leaves the queue,
squeue returns empty — at that point we read the exitcode file
written by the inner shell script. A non-zero exit code means EXIT;
zero means DONE; missing/malformed file means UNKWN (the inner
shell never got to write it, e.g. job was killed by SLURM).

Why not sacct: sacct requires SLURM accounting (slurmdbd) to be
configured, which not every cluster has. The exitcode-file fallback
is portable and matches what the inner shell actually wrote.

resource_spec semantics: free-form string of additional sbatch flags,
shlex-split into argv. Example: ``--mem=8192 --cpus-per-task=4
--time=01:00:00 --gres=gpu:1``. SLURM doesn't have a single ``-R``
equivalent, so we let the caller compose flags directly.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .base import (
    DONE,
    EXIT,
    PEND,
    RUN,
    UNKWN,
    ClusterConfigBase,
    ClusterExecutor,
    JobSpec,
    build_inner_shell,
    default_output_dir,
)


@dataclass(frozen=True)
class SlurmConfig(ClusterConfigBase):
    """All knobs for SLURM submission. Built from HARES_SLURM_* env vars."""
    partition: Optional[str]
    account: Optional[str]
    default_resource_spec: Optional[str]
    sbatch_bin: str
    squeue_bin: str
    scancel_bin: str


def load_slurm_config(session_tmp: Optional[Path] = None) -> SlurmConfig:
    """Build SlurmConfig from HARES_SLURM_* environment variables."""
    partition = os.environ.get("HARES_SLURM_PARTITION", "").strip() or None
    account = os.environ.get("HARES_SLURM_ACCOUNT", "").strip() or None
    default_resource_spec = (
        os.environ.get("HARES_SLURM_DEFAULT_RESOURCE_SPEC", "").strip() or None
    )
    poll_interval = float(os.environ.get("HARES_SLURM_POLL_INTERVAL_SEC", "10"))
    default_timeout = float(
        os.environ.get("HARES_SLURM_DEFAULT_TIMEOUT_SEC", "86400")
    )
    out_raw = os.environ.get("HARES_SLURM_OUTPUT_DIR", "").strip()
    if out_raw:
        output_dir = Path(os.path.expandvars(os.path.expanduser(out_raw))).resolve()
    else:
        output_dir = default_output_dir("slurm", session_tmp)
    return SlurmConfig(
        poll_interval_sec=max(1.0, poll_interval),
        default_timeout_sec=max(1.0, default_timeout),
        output_dir=output_dir,
        partition=partition,
        account=account,
        default_resource_spec=default_resource_spec,
        sbatch_bin=os.environ.get("HARES_SLURM_SBATCH_BIN", "sbatch"),
        squeue_bin=os.environ.get("HARES_SLURM_SQUEUE_BIN", "squeue"),
        scancel_bin=os.environ.get("HARES_SLURM_SCANCEL_BIN", "scancel"),
    )


# SLURM native job-state strings (long form, as %T returns).
# Reference: https://slurm.schedmd.com/squeue.html#lbAG
_SLURM_PENDING = {"PENDING", "CONFIGURING", "REQUEUED", "RESV_DEL_HOLD", "REQUEUE_FED",
                  "REQUEUE_HOLD", "RESIZING", "SIGNALING", "SPECIAL_EXIT", "STAGE_OUT",
                  "STOPPED", "SUSPENDED"}
_SLURM_RUNNING = {"RUNNING", "COMPLETING"}
_SLURM_DONE = {"COMPLETED"}
_SLURM_FAILED = {
    "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "PREEMPTED",
    "BOOT_FAIL", "DEADLINE", "OUT_OF_MEMORY", "REVOKED",
}


class SlurmExecutor(ClusterExecutor):
    """Session-scoped SLURM job manager."""

    SCHEDULER_NAME = "slurm"

    def __init__(
        self,
        cfg: SlurmConfig,
        ceiling: Optional[Path] = None,
    ) -> None:
        super().__init__(cfg, ceiling)
        self._cfg: SlurmConfig = cfg

    def _build_submit_argv(
        self,
        spec: JobSpec,
        stdout_file: Path,
        stderr_file: Path,
        exitcode_file: Path,
        job_name: str,
    ) -> list[str]:
        # --parsable returns just the job_id on stdout (or "JOBID;CLUSTER"
        # in federated mode). Much easier to parse than the default
        # "Submitted batch job 12345" line.
        argv: list[str] = [self._cfg.sbatch_bin, "--parsable"]
        if self._cfg.partition:
            argv += ["--partition", self._cfg.partition]
        if self._cfg.account:
            argv += ["--account", self._cfg.account]
        argv += ["--job-name", job_name]
        if spec.cwd:
            argv += [f"--chdir={spec.cwd}"]
        # Suppress sbatch's own output files — the inner shell script
        # captures stdout/stderr to our chosen paths.
        argv += ["--output=/dev/null", "--error=/dev/null"]

        # Resource flags: caller spec overrides cluster default.
        rs = spec.resource_spec or self._cfg.default_resource_spec
        if rs:
            argv += shlex.split(rs)

        # --wrap takes the inner script as a single argument; sbatch
        # writes a trivial wrapper around it and submits that.
        inner = build_inner_shell(spec, stdout_file, stderr_file, exitcode_file)
        argv += ["--wrap", inner]
        return argv

    def _parse_submit_output(self, stdout: str) -> Optional[str]:
        # --parsable output: "12345" or "12345;cluster_name".
        first = stdout.strip().splitlines()[0] if stdout.strip() else ""
        if not first:
            return None
        job_id = first.split(";", 1)[0].strip()
        return job_id or None

    def _build_status_argv(self, job_id: str) -> list[str]:
        # squeue prints %T (long-form state) for active jobs. -h omits
        # the header line. Empty output ⇒ job left the queue.
        return [self._cfg.squeue_bin, "--noheader", "-j", job_id, "-o", "%T"]

    def _parse_status(
        self,
        job_id: str,
        rc: int,
        stdout: str,
        stderr: str,
    ) -> str:
        # squeue exits non-zero ("Invalid job id specified") once the
        # job has left the queue — that's our signal to read the
        # exitcode file. Some squeue versions exit 0 with empty output
        # in the same situation; handle both.
        combined = (stdout + stderr).lower()
        if rc != 0:
            if "invalid job id" in combined or "not found" in combined:
                return self._terminal_from_exitcode(job_id)
            return UNKWN

        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        if not lines:
            return self._terminal_from_exitcode(job_id)

        native = lines[0].upper()
        if native in _SLURM_PENDING:
            return PEND
        if native in _SLURM_RUNNING:
            return RUN
        if native in _SLURM_DONE:
            return DONE
        if native in _SLURM_FAILED:
            return EXIT
        return UNKWN

    def _terminal_from_exitcode(self, job_id: str) -> str:
        """Job is no longer in the queue. Read its exitcode file to
        decide DONE vs EXIT."""
        rec = self._jobs.get(job_id)
        if rec is None:
            return UNKWN
        return self._exitcode_file_terminal_status(Path(rec.exitcode_file))

    def _build_cancel_argv(self, job_id: str) -> list[str]:
        return [self._cfg.scancel_bin, job_id]
