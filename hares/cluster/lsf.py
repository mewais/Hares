"""IBM Platform LSF backend.

LSF native statuses already match the canonical PEND/RUN/DONE/EXIT
set, so the status parser is a near-passthrough. The only translation
is bucketing the suspended/wait/zombie statuses into RUN (caller
doesn't care about the suspended sub-state) and treating "not found"
from bjobs as DONE (jobs leave the system after LSF's retention
period).
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .base import (
    DONE,
    PEND,
    RUN,
    UNKWN,
    ClusterConfigBase,
    ClusterExecutor,
    JobSpec,
    build_inner_shell,
    default_output_dir,
)

# bsub output: "Job <12345678> is submitted to queue <gpu>."
_BSUB_ID_RE = re.compile(r"Job\s+<(\d+)>")


@dataclass(frozen=True)
class LsfConfig(ClusterConfigBase):
    """All knobs for LSF submission. Built from HARES_LSF_* env vars."""
    queue: Optional[str]
    default_resource_spec: Optional[str]
    bsub_bin: str
    bjobs_bin: str
    bkill_bin: str


def load_lsf_config(session_tmp: Optional[Path] = None) -> LsfConfig:
    """Build LsfConfig from HARES_LSF_* environment variables."""
    queue = os.environ.get("HARES_LSF_QUEUE", "").strip() or None
    default_resource_spec = (
        os.environ.get("HARES_LSF_DEFAULT_RESOURCE_SPEC", "").strip() or None
    )
    poll_interval = float(os.environ.get("HARES_LSF_POLL_INTERVAL_SEC", "10"))
    default_timeout = float(
        os.environ.get("HARES_LSF_DEFAULT_TIMEOUT_SEC", "86400")
    )
    out_raw = os.environ.get("HARES_LSF_OUTPUT_DIR", "").strip()
    if out_raw:
        output_dir = Path(os.path.expandvars(os.path.expanduser(out_raw))).resolve()
    else:
        output_dir = default_output_dir("lsf", session_tmp)
    return LsfConfig(
        poll_interval_sec=max(1.0, poll_interval),
        default_timeout_sec=max(1.0, default_timeout),
        output_dir=output_dir,
        queue=queue,
        default_resource_spec=default_resource_spec,
        bsub_bin=os.environ.get("HARES_LSF_BSUB_BIN", "bsub"),
        bjobs_bin=os.environ.get("HARES_LSF_BJOBS_BIN", "bjobs"),
        bkill_bin=os.environ.get("HARES_LSF_BKILL_BIN", "bkill"),
    )


class LsfExecutor(ClusterExecutor):
    """Session-scoped LSF job manager."""

    SCHEDULER_NAME = "lsf"
    # LSF uses %I as the array index placeholder in -o filenames; the
    # per-task env var is $LSB_JOBINDEX (analog of SLURM's %a /
    # $SLURM_ARRAY_TASK_ID).
    ARRAY_TASK_PLACEHOLDER = "%I"

    def __init__(
        self,
        cfg: LsfConfig,
        ceiling: Optional[Path] = None,
    ) -> None:
        super().__init__(cfg, ceiling)
        self._cfg: LsfConfig = cfg  # narrow type for backend code

    def _build_submit_argv(
        self,
        spec: JobSpec,
        stdout_file: Path,
        stderr_file: Path,
        exitcode_file: Path,
        job_name: str,
    ) -> list[str]:
        argv: list[str] = [self._cfg.bsub_bin]
        if self._cfg.queue:
            argv += ["-q", self._cfg.queue]
        resource_spec = spec.resource_spec or self._cfg.default_resource_spec
        if resource_spec:
            argv += ["-R", resource_spec]
        # Array submission: bsub uses -J 'name[<spec>]' (the [...] is
        # part of the job-name argument, NOT a separate flag). Base
        # submit() has already appended .%I to the file paths.
        if spec.array is not None:
            argv += ["-J", f"{job_name}[{spec.array}]"]
        else:
            argv += ["-J", job_name]
        if spec.cwd:
            argv += ["-cwd", spec.cwd]
        if spec.array is not None:
            inner = self._build_array_inner_shell(
                spec, stdout_file, stderr_file, exitcode_file,
            )
        else:
            inner = build_inner_shell(spec, stdout_file, stderr_file, exitcode_file)
        argv += ["/bin/sh", "-c", inner]
        return argv

    @staticmethod
    def _build_array_inner_shell(
        spec: JobSpec,
        stdout_file: Path,
        stderr_file: Path,
        exitcode_file: Path,
    ) -> str:
        """Per-task variant of build_inner_shell for LSF arrays.

        Substitutes ``%I`` placeholders in the path templates with
        ``${LSB_JOBINDEX}`` so the shell expands them per task.
        Mirrors SlurmExecutor._build_array_inner_shell — same idea,
        different env-var name.
        """
        from .base import shell_quote
        env_prefix = ""
        if spec.env:
            env_prefix = "; ".join(
                f"export {k}={shell_quote(v)}"
                for k, v in spec.env.items()
            ) + "; "
        def _per_task(p: Path) -> str:
            s = str(p)
            if "%I" not in s:
                return shell_quote(s)
            base, _, after = s.partition("%I")
            return shell_quote(base) + '"${LSB_JOBINDEX}"' + (
                shell_quote(after) if after else ""
            )
        return (
            f"{env_prefix}"
            f"{spec.command} "
            f">{_per_task(stdout_file)} "
            f"2>{_per_task(stderr_file)}; "
            f"echo $? >{_per_task(exitcode_file)}"
        )

    def _parse_submit_output(self, stdout: str) -> Optional[str]:
        m = _BSUB_ID_RE.search(stdout)
        return m.group(1) if m else None

    def _build_status_argv(self, job_id: str) -> list[str]:
        return [self._cfg.bjobs_bin, "-noheader", job_id]

    def _parse_status(
        self,
        job_id: str,
        rc: int,
        stdout: str,
        stderr: str,
    ) -> str:
        combined = (stdout + stderr).lower()
        if rc != 0:
            if "not found" in combined or "no unfinished job found" in combined:
                # Job left LSF's retention window — conservatively treat as DONE.
                return DONE
            return UNKWN
        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        if not lines:
            return UNKWN

        # For array jobs, bjobs prints one line per task — aggregate
        # the same way SLURM does: any RUN ⇒ array RUN; else any PEND
        # ⇒ array PEND; otherwise the array has terminated and the
        # exitcode-file glob in _collect_array_result will roll up the
        # actual outcome.
        rec = self._jobs.get(job_id)
        if rec is not None and rec.array_spec is not None:
            saw_pending = saw_running = False
            running_set = {"RUN", "SSUSP", "USUSP", "PSUSP", "WAIT", "ZOMBI"}
            for ln in lines:
                parts = ln.split()
                if len(parts) < 3:
                    continue
                native = parts[2].upper()
                if native in running_set:
                    saw_running = True
                elif native == "PEND":
                    saw_pending = True
            if saw_running:
                return RUN
            if saw_pending:
                return PEND
            return DONE  # terminal; _collect_array_result reads per-task files

        # bjobs -noheader columns: JOBID USER STAT QUEUE FROM_HOST EXEC_HOST ...
        parts = lines[0].split()
        if len(parts) < 3:
            return UNKWN
        native = parts[2].upper()
        # LSF native PEND/RUN/DONE/EXIT match canonical exactly.
        # Bucket suspended/wait/zombie variants into RUN (the caller
        # doesn't need to act on the sub-state).
        if native in {"PEND"}:
            return PEND
        if native in {"RUN", "SSUSP", "USUSP", "PSUSP", "WAIT", "ZOMBI"}:
            return RUN
        if native == "DONE":
            return DONE
        if native == "EXIT":
            # EXIT is terminal; map to canonical EXIT.
            return "EXIT"
        return UNKWN

    def _build_cancel_argv(self, job_id: str) -> list[str]:
        return [self._cfg.bkill_bin, job_id]
