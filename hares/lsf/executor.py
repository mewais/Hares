"""LSF job executor.

Design decisions:
  - bsub wraps the command in a shell script that redirects stdout
    and stderr to Hares-managed files. This avoids LSF's own output
    file headers (whose format varies by site config and version)
    and gives us a clean exit-code file.
  - _run() is a thin asyncio.create_subprocess_exec wrapper. Tests
    monkeypatch it to simulate bsub/bjobs/bkill without touching the
    real scheduler.
  - All mutable job state is guarded by a single asyncio.Lock so
    multiple concurrent lsf_wait polls don't race.
  - lsf_execute_blocking is a thin wrapper: submit([spec]) + wait([id]).
    No duplicate logic.
  - wait() returns TIMEOUT for jobs that don't finish in time; the
    jobs continue running. Caller must lsf_cancel to stop them.

Security note (documented in module docstring and tool descriptions):
  - Ceiling check on cwd is pre-submission only. The cluster node
    has no Hares process; path enforcement after bsub is impossible.
  - No RLIMIT, no bwrap, no active-scope enforcement for LSF tools.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# LSF terminal statuses — wait loop exits on these.
_TERMINAL = frozenset({"DONE", "EXIT"})

# bsub output: "Job <12345678> is submitted to queue <gpu>."
_BSUB_ID_RE = re.compile(r"Job\s+<(\d+)>")


# ── Config ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LsfConfig:
    """All knobs for LSF submission. Built from HARES_LSF_* env vars."""
    queue: Optional[str]               # HARES_LSF_QUEUE
    default_resource_spec: Optional[str]  # HARES_LSF_DEFAULT_RESOURCE_SPEC
    poll_interval_sec: float           # HARES_LSF_POLL_INTERVAL_SEC
    default_timeout_sec: float         # HARES_LSF_DEFAULT_TIMEOUT_SEC
    output_dir: Path                   # HARES_LSF_OUTPUT_DIR (or tempdir)
    bsub_bin: str                      # HARES_LSF_BSUB_BIN
    bjobs_bin: str                     # HARES_LSF_BJOBS_BIN
    bkill_bin: str                     # HARES_LSF_BKILL_BIN


def load_lsf_config(session_tmp: Optional[Path] = None) -> LsfConfig:
    """Build LsfConfig from HARES_LSF_* environment variables.

    Args:
      session_tmp: Fallback output dir when HARES_LSF_OUTPUT_DIR is
        unset. Caller should pass a per-session tempdir so output
        files have a predictable lifetime.
    """
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
        output_dir = session_tmp or Path(
            tempfile.mkdtemp(prefix="hares-lsf-")
        )
    return LsfConfig(
        queue=queue,
        default_resource_spec=default_resource_spec,
        poll_interval_sec=max(1.0, poll_interval),
        default_timeout_sec=max(1.0, default_timeout),
        output_dir=output_dir,
        bsub_bin=os.environ.get("HARES_LSF_BSUB_BIN", "bsub"),
        bjobs_bin=os.environ.get("HARES_LSF_BJOBS_BIN", "bjobs"),
        bkill_bin=os.environ.get("HARES_LSF_BKILL_BIN", "bkill"),
    )


# ── Job data ─────────────────────────────────────────────────────────────────

@dataclass
class JobSpec:
    """Caller-supplied description of one LSF job."""
    command: str
    resource_spec: Optional[str] = None
    name: Optional[str] = None
    cwd: Optional[str] = None
    env: Optional[dict[str, str]] = None


class JobRecord:
    """Mutable per-job state tracked by LsfExecutor."""

    __slots__ = (
        "job_id", "name", "command", "submitted_at",
        "stdout_file", "stderr_file", "exitcode_file",
        "status", "result",
    )

    def __init__(
        self,
        job_id: str,
        name: str,
        command: str,
        submitted_at: str,
        stdout_file: str,
        stderr_file: str,
        exitcode_file: str,
    ) -> None:
        self.job_id = job_id
        self.name = name
        self.command = command
        self.submitted_at = submitted_at
        self.stdout_file = stdout_file
        self.stderr_file = stderr_file
        self.exitcode_file = exitcode_file
        self.status = "PEND"
        self.result: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "job_id": self.job_id,
            "name": self.name,
            "command": self.command,
            "submitted_at": self.submitted_at,
            "status": self.status,
        }
        if self.result is not None:
            d["result"] = self.result
        return d


# ── Executor ─────────────────────────────────────────────────────────────────

class LsfExecutor:
    """Session-scoped LSF job manager.

    One instance per hares-mcp process. Tracks all jobs submitted in
    this session (in-memory only — not persisted). Thread/task safe
    via an asyncio.Lock on the jobs dict.

    Security: no bwrap, no RLIMIT, no active-scope enforcement. The
    cluster node runs the job with the submitting user's permissions.
    The only guard applied here is a pre-submission ceiling check on
    the job's working directory (best-effort, not kernel-enforced).
    """

    def __init__(
        self,
        cfg: LsfConfig,
        ceiling: Optional[Path] = None,
    ) -> None:
        self._cfg = cfg
        self._ceiling = ceiling
        self._lock = asyncio.Lock()
        self._jobs: dict[str, JobRecord] = {}
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Internal helpers ────────────────────────────────────────────────────

    async def _run(
        self,
        argv: list[str],
    ) -> tuple[int, str, str]:
        """Run an LSF CLI command, return (returncode, stdout, stderr).

        Separated for testability — tests monkeypatch this method to
        simulate bsub/bjobs/bkill responses without a real scheduler.

        Returns (1, "", error_message) instead of raising when the
        binary is not found or exec fails — callers treat non-zero
        returncode as a command failure and surface it as a structured
        error, not an unhandled exception.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            binary = argv[0] if argv else "?"
            return 1, "", f"{binary}: {exc}"
        stdout_b, stderr_b = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )

    def _output_paths(self, uid: str) -> tuple[Path, Path, Path]:
        """Return (stdout_file, stderr_file, exitcode_file) for a job uid."""
        base = self._cfg.output_dir / uid
        return (
            base.with_suffix(".out"),
            base.with_suffix(".err"),
            base.with_suffix(".exit"),
        )

    def _validate_cwd(self, cwd: Optional[str]) -> None:
        """Best-effort ceiling check on the job's working directory.

        This is a pre-submission sanity guard — NOT a security
        guarantee. The cluster node runs the job unrestricted;
        a command that computes paths at runtime can still access
        any path the user has cluster-level permission to.
        """
        if cwd is None or self._ceiling is None:
            return
        from ..path_safety import _is_subpath
        resolved = Path(cwd).resolve(strict=False)
        if not _is_subpath(resolved, self._ceiling):
            raise ValueError(
                f"Job cwd {cwd!r} (resolved: {resolved}) is outside "
                f"the ceiling {self._ceiling}. "
                "NOTE: this is a pre-submission check only — no "
                "kernel-level enforcement applies on the cluster node."
            )

    def _build_bsub_argv(
        self,
        spec: JobSpec,
        stdout_file: Path,
        stderr_file: Path,
        exitcode_file: Path,
        job_name: str,
    ) -> list[str]:
        """Build the bsub argv for one JobSpec.

        stdout/stderr/exitcode are captured by the inner shell script
        rather than by bsub's -o/-e flags. This avoids LSF's output
        file header (format varies by site config and LSF version).
        The inner script is:

            export K=V; ...; COMMAND >STDOUT 2>STDERR; echo $? >EXIT

        bsub's own stdout (submission acknowledgment) is captured by
        _run(); bsub's stderr goes to its own stderr (also captured).
        """
        argv: list[str] = [self._cfg.bsub_bin]

        if self._cfg.queue:
            argv += ["-q", self._cfg.queue]

        resource_spec = spec.resource_spec or self._cfg.default_resource_spec
        if resource_spec:
            argv += ["-R", resource_spec]

        argv += ["-J", job_name]

        if spec.cwd:
            argv += ["-cwd", spec.cwd]

        # Build the inner shell script.
        env_prefix = ""
        if spec.env:
            exports = "; ".join(
                f"export {k}={_shell_quote(v)}"
                for k, v in spec.env.items()
            )
            env_prefix = exports + "; "

        inner = (
            f"{env_prefix}"
            f"{spec.command} "
            f">{_shell_quote(str(stdout_file))} "
            f"2>{_shell_quote(str(stderr_file))}; "
            f"echo $? >{_shell_quote(str(exitcode_file))}"
        )
        argv += ["/bin/sh", "-c", inner]
        return argv

    async def _poll_status(self, job_id: str) -> str:
        """Query bjobs for one job. Returns an LSF status string.

        LSF statuses: PEND, RUN, DONE, EXIT, SSUSP, USUSP, PSUSP,
        WAIT, ZOMBI, UNKWN.

        bjobs exits non-zero and prints "is not found" when the job
        has passed LSF's completion-record retention period. We treat
        that as DONE (conservative — if it's gone, it finished).
        """
        rc, stdout, stderr = await self._run([
            self._cfg.bjobs_bin, "-noheader", job_id,
        ])
        combined = (stdout + stderr).lower()
        if rc != 0:
            if "not found" in combined or "no unfinished job found" in combined:
                return "DONE"
            return "UNKWN"
        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        if not lines:
            return "UNKWN"
        # bjobs -noheader columns: JOBID USER STAT QUEUE FROM_HOST EXEC_HOST ...
        parts = lines[0].split()
        if len(parts) < 3:
            return "UNKWN"
        return parts[2].upper()

    async def _collect_result(self, record: JobRecord, status: str) -> dict[str, Any]:
        """Read stdout/stderr/exitcode files after a job reaches terminal state."""
        stdout_content = _read_file_safe(Path(record.stdout_file))
        stderr_content = _read_file_safe(Path(record.stderr_file))
        exit_raw = _read_file_safe(Path(record.exitcode_file)).strip()
        try:
            exit_code = int(exit_raw)
        except (ValueError, TypeError):
            # Exitcode file missing or malformed: job likely killed by LSF.
            exit_code = -1 if status == "EXIT" else 0

        return {
            "job_id": record.job_id,
            "name": record.name,
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout_content,
            "stderr": stderr_content,
        }

    # ── Public API ──────────────────────────────────────────────────────────

    async def submit(self, specs: list[JobSpec]) -> list[dict[str, Any]]:
        """Submit one or more jobs to LSF.

        Returns a list of dicts (one per spec). Each dict contains
        either a ``job_id`` and metadata, or an ``error`` string if
        submission failed. Failed specs do not block other specs.
        """
        results: list[dict[str, Any]] = []
        for spec in specs:
            if not spec.command:
                results.append({"error": "command must be non-empty", "job_id": None})
                continue

            try:
                self._validate_cwd(spec.cwd)
            except ValueError as exc:
                results.append({"error": str(exc), "job_id": None})
                continue

            uid = uuid.uuid4().hex[:12]
            stdout_file, stderr_file, exitcode_file = self._output_paths(uid)
            job_name = (spec.name or f"hares-{uid}").strip()

            argv = self._build_bsub_argv(
                spec, stdout_file, stderr_file, exitcode_file, job_name,
            )
            logger.info("bsub: %s", " ".join(argv))
            rc, stdout, stderr = await self._run(argv)

            if rc != 0:
                results.append({
                    "error": (
                        f"bsub failed (exit {rc}): "
                        f"{(stderr.strip() or stdout.strip())!r}"
                    ),
                    "job_id": None,
                    "name": job_name,
                })
                continue

            m = _BSUB_ID_RE.search(stdout)
            if not m:
                results.append({
                    "error": (
                        f"bsub succeeded but job ID not found in output: "
                        f"{stdout.strip()!r}"
                    ),
                    "job_id": None,
                    "name": job_name,
                })
                continue

            job_id = m.group(1)
            now = datetime.now(timezone.utc).isoformat()
            record = JobRecord(
                job_id=job_id,
                name=job_name,
                command=spec.command,
                submitted_at=now,
                stdout_file=str(stdout_file),
                stderr_file=str(stderr_file),
                exitcode_file=str(exitcode_file),
            )
            async with self._lock:
                self._jobs[job_id] = record

            logger.info("LSF job %s submitted (%s)", job_id, job_name)
            results.append({
                "job_id": job_id,
                "name": job_name,
                "status": "PEND",
                "submitted_at": now,
            })

        return results

    async def wait(
        self,
        job_ids: list[str],
        timeout_sec: float,
    ) -> dict[str, dict[str, Any]]:
        """Wait for all listed jobs to reach a terminal state (DONE or EXIT).

        Polls bjobs every HARES_LSF_POLL_INTERVAL_SEC seconds. Returns
        as soon as all jobs finish or timeout_sec elapses, whichever
        comes first.

        Return value: mapping job_id → result dict. Each result dict
        contains: job_id, name, status, exit_code, stdout, stderr.
        Jobs that time out have status=TIMEOUT; they keep running on
        the cluster — call lsf_cancel to stop them.

        Jobs not known to this session return an ERROR entry. They may
        have been submitted by a different hares-mcp process.
        """
        pending: set[str] = set()
        results: dict[str, dict[str, Any]] = {}

        # Separate known from unknown; short-circuit already-collected.
        async with self._lock:
            for job_id in job_ids:
                if job_id not in self._jobs:
                    results[job_id] = {
                        "job_id": job_id,
                        "status": "ERROR",
                        "error": (
                            f"Job {job_id!r} is not known to this Hares session. "
                            "It may have been submitted by a different process. "
                            "Use lsf_jobs to list jobs submitted in this session."
                        ),
                    }
                    continue
                rec = self._jobs[job_id]
                if rec.result is not None:
                    results[job_id] = rec.result
                else:
                    pending.add(job_id)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_sec

        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                for job_id in pending:
                    results[job_id] = {
                        "job_id": job_id,
                        "status": "TIMEOUT",
                        "error": (
                            f"Timed out after {timeout_sec:.0f}s waiting for "
                            f"job {job_id}. The job is still running on the "
                            "cluster. Call lsf_cancel to stop it, or call "
                            "lsf_wait again with a longer timeout."
                        ),
                    }
                break

            # Concurrent poll of all pending jobs.
            pending_list = list(pending)
            poll_results = await asyncio.gather(
                *[self._poll_status(jid) for jid in pending_list],
                return_exceptions=True,
            )

            for job_id, status_or_exc in zip(pending_list, poll_results):
                if isinstance(status_or_exc, BaseException):
                    logger.warning("Error polling job %s: %s", job_id, status_or_exc)
                    continue
                status: str = status_or_exc

                # Update record status.
                async with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id].status = status

                if status in _TERMINAL:
                    async with self._lock:
                        rec = self._jobs.get(job_id)
                    if rec is not None:
                        result = await self._collect_result(rec, status)
                        async with self._lock:
                            self._jobs[job_id].result = result
                        results[job_id] = result
                    pending.discard(job_id)
                    logger.info("LSF job %s reached %s", job_id, status)

            if pending:
                sleep_for = min(
                    self._cfg.poll_interval_sec,
                    max(0.0, deadline - loop.time()),
                )
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)

        return results

    async def cancel(self, job_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Cancel jobs via bkill.

        Returns a mapping job_id → {job_id, cancelled, message}.
        bkill exit code determines ``cancelled``. The session record
        is updated to CANCELED regardless (bkill may report success
        even for already-finished jobs).
        """
        results: dict[str, dict[str, Any]] = {}
        for job_id in job_ids:
            rc, stdout, stderr = await self._run(
                [self._cfg.bkill_bin, job_id]
            )
            cancelled = rc == 0
            msg = (stdout.strip() or stderr.strip()) or (
                "OK" if cancelled else f"bkill exit {rc}"
            )
            async with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id].status = "CANCELED"
            results[job_id] = {
                "job_id": job_id,
                "cancelled": cancelled,
                "message": msg,
            }
            logger.info("bkill %s: rc=%d msg=%r", job_id, rc, msg)
        return results

    async def jobs(self) -> list[dict[str, Any]]:
        """Return all jobs submitted in this session with their current status."""
        async with self._lock:
            return [r.to_dict() for r in self._jobs.values()]

    async def execute_blocking(
        self,
        spec: JobSpec,
        timeout_sec: float,
    ) -> dict[str, Any]:
        """Submit one job and wait for it synchronously.

        Thin wrapper around submit([spec]) + wait([job_id], timeout_sec).
        Use this for single sequential jobs. Use lsf_submit + lsf_wait
        when you have multiple independent jobs to run in parallel.
        """
        submitted = await self.submit([spec])
        sub = submitted[0]
        if sub.get("error") or not sub.get("job_id"):
            return {
                "status": "ERROR",
                "error": sub.get("error", "Unknown submission error"),
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
            }
        results = await self.wait([sub["job_id"]], timeout_sec)
        return results[sub["job_id"]]


# ── Utilities ────────────────────────────────────────────────────────────────

def _read_file_safe(path: Path) -> str:
    """Read a file, returning empty string on any error."""
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _shell_quote(s: str) -> str:
    """Minimal single-quote escaping for embedding in /bin/sh -c scripts."""
    return "'" + s.replace("'", "'\\''") + "'"
