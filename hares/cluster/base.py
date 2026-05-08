"""Shared base for cluster-job executors (LSF, SLURM, future backends).

Design:
  - ClusterExecutor is an abstract base. It owns the parts that don't
    care which scheduler is on the other end: the async poll loop,
    timeout enforcement, output capture via inner shell redirect,
    session-job map, cancel bookkeeping.
  - Backends override five small methods: argv builders for submit /
    status / cancel, plus parsers for submit-output and status-output.
  - All backend status strings are normalized to a canonical set:
    PEND, RUN, DONE, EXIT, UNKWN. The wait loop only needs to know
    DONE and EXIT are terminal; backends do the translation.

Security model is identical across all backends and identical to the
pre-refactor LSF docstring: cluster nodes run jobs with the submitting
user's full filesystem permissions. bwrap, RLIMIT, and active-scope
enforcement do not extend to them. The only guard applied here is a
pre-submission ceiling check on the job's working directory.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Canonical statuses every backend must map onto. Wait loop exits on
# anything in TERMINAL.
PEND = "PEND"
RUN = "RUN"
DONE = "DONE"
EXIT = "EXIT"
UNKWN = "UNKWN"

TERMINAL = frozenset({DONE, EXIT})

# In-memory job-record retention cap. Once exceeded, oldest TERMINAL
# records are evicted FIFO; PEND/RUN are never evicted. Module-level
# constant — not exposed as an env var until someone files an issue
# saying 1000 is the wrong number for their workload.
MAX_RETAINED_JOBS = 1000


@dataclass
class JobSpec:
    """Caller-supplied description of one cluster job.

    Fields are deliberately scheduler-agnostic. ``resource_spec`` is
    free-form text passed verbatim to the backend's submit command:
      - LSF: contents of ``-R`` (e.g. ``rusage[mem=8192]``).
      - SLURM: extra ``sbatch`` flags (e.g. ``--mem=8192 --time=01:00:00``).
    """
    command: str
    resource_spec: Optional[str] = None
    name: Optional[str] = None
    cwd: Optional[str] = None
    env: Optional[dict[str, str]] = None


class JobRecord:
    """Mutable per-job state tracked by an executor."""

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
        self.status = PEND
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


@dataclass(frozen=True)
class ClusterConfigBase:
    """Common knobs every backend's config inherits from.

    Backend-specific dataclasses (LsfConfig, SlurmConfig) extend this
    with binary paths and scheduler-specific defaults.
    """
    poll_interval_sec: float
    default_timeout_sec: float
    output_dir: Path


class ClusterExecutor(abc.ABC):
    """Session-scoped cluster job manager — abstract base.

    One instance per hares-mcp process. Tracks all jobs submitted in
    this session in-memory only (not persisted across restarts).
    Thread/task safe via an asyncio.Lock on the jobs dict.

    Backends supply argv builders + status parsers; the base owns
    submit/wait/cancel/jobs and the polling loop.
    """

    # Human-readable scheduler name used in error messages and logs.
    # Backends override.
    SCHEDULER_NAME = "cluster"

    def __init__(
        self,
        cfg: ClusterConfigBase,
        ceiling: Optional[Path] = None,
    ) -> None:
        self._cfg = cfg
        self._ceiling = ceiling
        self._lock = asyncio.Lock()
        self._jobs: dict[str, JobRecord] = {}
        # Tracks job_ids that were retained-job-cap evicted, so wait()
        # can return a useful "this finished but we threw away the
        # result" entry instead of "never heard of it".
        self._evicted_ids: set[str] = set()
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Hooks backends MUST implement ───────────────────────────────────────

    @abc.abstractmethod
    def _build_submit_argv(
        self,
        spec: JobSpec,
        stdout_file: Path,
        stderr_file: Path,
        exitcode_file: Path,
        job_name: str,
    ) -> list[str]:
        """argv for one submit invocation. The inner shell script
        captures stdout/stderr/exitcode; backends should not pass the
        scheduler's own ``-o``/``-e`` flags."""

    @abc.abstractmethod
    def _parse_submit_output(self, stdout: str) -> Optional[str]:
        """Extract the job_id from submit-command stdout. Return None
        on parse failure — caller surfaces a structured error."""

    @abc.abstractmethod
    def _build_status_argv(self, job_id: str) -> list[str]:
        """argv for querying one job's current status."""

    @abc.abstractmethod
    def _parse_status(
        self,
        job_id: str,
        rc: int,
        stdout: str,
        stderr: str,
    ) -> str:
        """Parse status-query output and return one of PEND/RUN/DONE/EXIT/UNKWN."""

    @abc.abstractmethod
    def _build_cancel_argv(self, job_id: str) -> list[str]:
        """argv for cancelling one job."""

    # ── Internal helpers (shared) ───────────────────────────────────────────

    async def _run(self, argv: list[str]) -> tuple[int, str, str]:
        """Run a scheduler CLI command, return (returncode, stdout, stderr).

        Separated for testability — tests monkeypatch this method to
        simulate the scheduler without bsub/squeue/etc on PATH.

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

        Pre-submission sanity guard — NOT a security guarantee. The
        cluster node runs the job unrestricted; a command that
        computes paths at runtime can still access any path the user
        has cluster-level permission to.
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

    async def _collect_result(
        self, record: JobRecord, status: str,
    ) -> dict[str, Any]:
        """Read stdout/stderr/exitcode files after a job reaches terminal state."""
        stdout_content = _read_file_safe(Path(record.stdout_file))
        stderr_content = _read_file_safe(Path(record.stderr_file))
        exit_raw = _read_file_safe(Path(record.exitcode_file)).strip()
        try:
            exit_code = int(exit_raw)
        except (ValueError, TypeError):
            # Exitcode file missing or malformed: job likely killed by scheduler.
            exit_code = -1 if status == EXIT else 0

        return {
            "job_id": record.job_id,
            "name": record.name,
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout_content,
            "stderr": stderr_content,
        }

    def _maybe_evict_terminal(self) -> None:
        """Evict oldest terminal job records when retention cap is exceeded.

        Caller MUST hold ``self._lock``. Only TERMINAL records (DONE,
        EXIT, CANCELED) are eligible — PEND/RUN are never evicted, so
        a job stuck in queue forever can't be silently dropped from
        the in-memory map.

        Eviction order is FIFO of insertion (Python dict preserves it
        from 3.7+). The evicted job_ids are remembered in
        ``self._evicted_ids`` so a later wait() call can return a
        useful "EVICTED" entry instead of "never heard of it".
        """
        cap = MAX_RETAINED_JOBS
        if cap <= 0 or len(self._jobs) <= cap:
            return
        eligible = ("CANCELED",)  # in addition to the TERMINAL set
        to_remove: list[str] = []
        target_evictions = len(self._jobs) - cap
        for jid, rec in self._jobs.items():
            if rec.status in TERMINAL or rec.status in eligible:
                to_remove.append(jid)
                if len(to_remove) >= target_evictions:
                    break
        for jid in to_remove:
            del self._jobs[jid]
            self._evicted_ids.add(jid)
        if to_remove:
            logger.info(
                "%s: evicted %d terminal job records "
                "(cap=%d, retained=%d, evicted_total=%d)",
                self.SCHEDULER_NAME, len(to_remove), cap,
                len(self._jobs), len(self._evicted_ids),
            )

    def _exitcode_file_terminal_status(self, exitcode_file: Path) -> str:
        """Helper for backends whose status query returns 'job no longer
        in queue'. Reads the exitcode file and returns DONE or EXIT.
        Returns UNKWN if the file is missing/malformed (job may have
        been killed before the inner shell wrote it).
        """
        raw = _read_file_safe(exitcode_file).strip()
        if not raw:
            return UNKWN
        try:
            return DONE if int(raw) == 0 else EXIT
        except ValueError:
            return UNKWN

    # ── Public API ──────────────────────────────────────────────────────────

    async def submit(self, specs: list[JobSpec]) -> list[dict[str, Any]]:
        """Submit one or more jobs.

        Returns one dict per spec. Each dict has either a ``job_id``
        and metadata, or an ``error`` string if submission failed.
        Failed specs do not block other specs.
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

            argv = self._build_submit_argv(
                spec, stdout_file, stderr_file, exitcode_file, job_name,
            )
            logger.info("%s submit: %s", self.SCHEDULER_NAME, " ".join(argv))
            rc, stdout, stderr = await self._run(argv)

            if rc != 0:
                results.append({
                    "error": (
                        f"{self.SCHEDULER_NAME} submit failed (exit {rc}): "
                        f"{(stderr.strip() or stdout.strip())!r}"
                    ),
                    "job_id": None,
                    "name": job_name,
                })
                continue

            job_id = self._parse_submit_output(stdout)
            if not job_id:
                results.append({
                    "error": (
                        f"{self.SCHEDULER_NAME} submit succeeded but job ID "
                        f"not found in output: {stdout.strip()!r}"
                    ),
                    "job_id": None,
                    "name": job_name,
                })
                continue

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

            logger.info(
                "%s job %s submitted (%s)",
                self.SCHEDULER_NAME, job_id, job_name,
            )
            results.append({
                "job_id": job_id,
                "name": job_name,
                "status": PEND,
                "submitted_at": now,
            })

        return results

    async def wait(
        self,
        job_ids: list[str],
        timeout_sec: float,
    ) -> dict[str, dict[str, Any]]:
        """Wait for all listed jobs to reach a terminal state (DONE or EXIT).

        Polls every ``poll_interval_sec``. Returns when all jobs finish
        or ``timeout_sec`` elapses. Timed-out jobs keep running on the
        cluster — call ``cancel`` to stop them.

        Return value: mapping job_id → result dict. Each result has
        job_id, name, status, exit_code, stdout, stderr. Jobs not
        known to this session return an ERROR entry.
        """
        pending: set[str] = set()
        results: dict[str, dict[str, Any]] = {}

        # Separate known from unknown; short-circuit already-collected.
        async with self._lock:
            for job_id in job_ids:
                if job_id not in self._jobs:
                    if job_id in self._evicted_ids:
                        results[job_id] = {
                            "job_id": job_id,
                            "status": "EVICTED",
                            "error": (
                                f"Job {job_id!r} completed in this session but "
                                f"its record was evicted to keep memory usage "
                                f"bounded (retention cap: "
                                f"{MAX_RETAINED_JOBS} jobs). Query the scheduler "
                                f"directly for the final state."
                            ),
                        }
                        continue
                    results[job_id] = {
                        "job_id": job_id,
                        "status": "ERROR",
                        "error": (
                            f"Job {job_id!r} is not known to this Hares session. "
                            "It may have been submitted by a different process. "
                            f"Use {self.SCHEDULER_NAME}_jobs to list jobs "
                            "submitted in this session."
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
                            f"cluster. Call {self.SCHEDULER_NAME}_cancel to "
                            f"stop it, or call {self.SCHEDULER_NAME}_wait "
                            "again with a longer timeout."
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

                async with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id].status = status

                if status in TERMINAL:
                    async with self._lock:
                        rec = self._jobs.get(job_id)
                    if rec is not None:
                        result = await self._collect_result(rec, status)
                        async with self._lock:
                            self._jobs[job_id].result = result
                        results[job_id] = result
                    pending.discard(job_id)
                    logger.info(
                        "%s job %s reached %s",
                        self.SCHEDULER_NAME, job_id, status,
                    )

            if pending:
                sleep_for = min(
                    self._cfg.poll_interval_sec,
                    max(0.0, deadline - loop.time()),
                )
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)

        # End-of-wait eviction: now that this batch's results are
        # collected, drop the oldest terminal records if we're over
        # cap. Doing it here (vs per-completion inside the loop)
        # ensures eviction picks the truly oldest by submission order
        # rather than the first one to complete.
        async with self._lock:
            self._maybe_evict_terminal()

        return results

    async def _poll_status(self, job_id: str) -> str:
        """Run the backend's status query and parse the result."""
        argv = self._build_status_argv(job_id)
        rc, stdout, stderr = await self._run(argv)
        return self._parse_status(job_id, rc, stdout, stderr)

    async def cancel(self, job_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Cancel jobs.

        Returns a mapping job_id → {job_id, cancelled, message}. The
        session record is updated to CANCELED regardless of the
        backend's response (the cancel binary may report success even
        for already-finished jobs).
        """
        results: dict[str, dict[str, Any]] = {}
        for job_id in job_ids:
            argv = self._build_cancel_argv(job_id)
            rc, stdout, stderr = await self._run(argv)
            cancelled = rc == 0
            msg = (stdout.strip() or stderr.strip()) or (
                "OK" if cancelled else f"cancel exit {rc}"
            )
            async with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id].status = "CANCELED"
            results[job_id] = {
                "job_id": job_id,
                "cancelled": cancelled,
                "message": msg,
            }
            logger.info(
                "%s cancel %s: rc=%d msg=%r",
                self.SCHEDULER_NAME, job_id, rc, msg,
            )
        # End-of-cancel eviction (mirrors wait()): pick the oldest
        # terminals by submission order instead of letting cancel order
        # influence which records survive.
        async with self._lock:
            self._maybe_evict_terminal()
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


# ── Utilities (shared by backends) ─────────────────────────────────────────

def _read_file_safe(path: Path) -> str:
    """Read a file, returning empty string on any error."""
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def shell_quote(s: str) -> str:
    """Single-quote escaping for embedding in /bin/sh -c scripts."""
    return "'" + s.replace("'", "'\\''") + "'"


def build_inner_shell(
    spec: JobSpec,
    stdout_file: Path,
    stderr_file: Path,
    exitcode_file: Path,
) -> str:
    """Build the inner shell script that captures stdout/stderr/exitcode.

    Same pattern across all backends: explicit redirect + ``echo $?``
    to a separate file. Avoids depending on the scheduler's own output
    capture (whose format varies by site config and version).
    """
    env_prefix = ""
    if spec.env:
        exports = "; ".join(
            f"export {k}={shell_quote(v)}"
            for k, v in spec.env.items()
        )
        env_prefix = exports + "; "
    return (
        f"{env_prefix}"
        f"{spec.command} "
        f">{shell_quote(str(stdout_file))} "
        f"2>{shell_quote(str(stderr_file))}; "
        f"echo $? >{shell_quote(str(exitcode_file))}"
    )


def default_output_dir(scheduler: str, session_tmp: Optional[Path]) -> Path:
    """Pick a default output dir for a backend's load_*_config function."""
    if session_tmp is not None:
        return session_tmp
    return Path(tempfile.mkdtemp(prefix=f"hares-{scheduler}-"))
