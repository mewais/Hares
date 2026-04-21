"""Pre-flight command inspector / rewriter.

Catches known overcommit patterns BEFORE spawning the subprocess and
rewrites them to safe values. Saves a kill-cycle for the most common
mistake (pytest -n auto on a 2-core box).

Generic rule: anywhere a tool accepts a "number of workers" flag and
the value is `auto`, missing, or larger than the per-command worker
budget, clamp it down.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Rewrite:
    """A single token-level edit applied during inspection."""
    kind: str             # e.g. "pytest_xdist", "make_jobs", "cargo_jobs"
    original: str         # The exact substring that was rewritten
    replacement: str      # What it became
    reason: str           # Human-readable explanation


@dataclass
class InspectionResult:
    """Output of inspect_command()."""
    command: str               # Possibly-rewritten command string
    rewrites: list[Rewrite]    # Edits applied (empty if none needed)


# Tools that accept a "-jN" / "-n auto" / "--jobs N" worker-count flag.
# Each entry maps to its pattern handler in _rewrite_tokens.
_PYTEST_BINARIES = ("pytest", "py.test")
_NUMERIC_RE = re.compile(r"^-?\d+$")


def _budget_for(weight: int, max_concurrent: int) -> int:
    """How many workers a single command is allowed to spawn.

    A command with `weight=W` is occupying W of `max_concurrent`
    semaphore slots; we let it spawn up to W internal workers. This
    matches the cgroup-style invariant: total parallel workers across
    all live commands ≤ max_concurrent.
    """
    return max(1, min(weight, max_concurrent))


def _looks_like_pytest_invocation(tokens: list[str], i: int) -> bool:
    """Return True if tokens[i] is the pytest binary in a recognisable
    invocation. Handles `pytest`, `py.test`, `python -m pytest`,
    `python3 -m pytest`, `uv run pytest`, `.venv/bin/pytest`."""
    tok = tokens[i]
    base = tok.rsplit("/", 1)[-1]
    if base in _PYTEST_BINARIES:
        return True
    if (base in ("pytest",)) or (i >= 2 and tokens[i - 1] == "pytest"):
        return True
    return False


def _is_pytest_command(tokens: list[str]) -> bool:
    """Return True if any token in this command refers to pytest.

    We do this loosely on purpose: `python -m pytest`, `uv run pytest`,
    `.venv/bin/python -m pytest`, etc. all look like pytest commands.
    """
    for tok in tokens:
        base = tok.rsplit("/", 1)[-1]
        if base in _PYTEST_BINARIES:
            return True
    return False


def _is_make_command(tokens: list[str]) -> bool:
    for tok in tokens:
        base = tok.rsplit("/", 1)[-1]
        if base in ("make", "gmake", "bmake"):
            return True
    return False


def _is_cargo_command(tokens: list[str]) -> bool:
    for tok in tokens:
        base = tok.rsplit("/", 1)[-1]
        if base == "cargo":
            return True
    return False


def _is_ninja_command(tokens: list[str]) -> bool:
    for tok in tokens:
        base = tok.rsplit("/", 1)[-1]
        if base == "ninja":
            return True
    return False


def _rewrite_pytest_xdist(tokens: list[str], budget: int) -> list[Rewrite]:
    """In-place rewrite of `-n auto`, `-n N`, `--numprocesses=...` to
    fit `budget` workers. Returns the list of edits applied."""
    rewrites: list[Rewrite] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # `-n auto` / `-n N` / `-nauto` / `-nN`
        if tok == "-n" and i + 1 < len(tokens):
            val = tokens[i + 1]
            if val == "auto" or (_NUMERIC_RE.match(val) and int(val) > budget):
                rewrites.append(Rewrite(
                    kind="pytest_xdist",
                    original=f"-n {val}",
                    replacement=f"-n {budget}",
                    reason=f"pytest-xdist requested {val} workers; clamped to {budget} to fit Hares cap.",
                ))
                tokens[i + 1] = str(budget)
            i += 2
            continue
        if tok.startswith("-n") and tok != "-n" and not tok.startswith("--"):
            # -nauto, -n4, etc.
            val = tok[2:]
            if val == "auto" or (_NUMERIC_RE.match(val) and int(val) > budget):
                rewrites.append(Rewrite(
                    kind="pytest_xdist",
                    original=tok,
                    replacement=f"-n{budget}",
                    reason=f"pytest-xdist requested {val} workers; clamped to {budget}.",
                ))
                tokens[i] = f"-n{budget}"
        # --numprocesses=auto / --numprocesses=N
        if tok.startswith("--numprocesses="):
            val = tok.split("=", 1)[1]
            if val == "auto" or (_NUMERIC_RE.match(val) and int(val) > budget):
                rewrites.append(Rewrite(
                    kind="pytest_xdist",
                    original=tok,
                    replacement=f"--numprocesses={budget}",
                    reason=f"pytest-xdist requested {val} workers; clamped to {budget}.",
                ))
                tokens[i] = f"--numprocesses={budget}"
        if tok == "--numprocesses" and i + 1 < len(tokens):
            val = tokens[i + 1]
            if val == "auto" or (_NUMERIC_RE.match(val) and int(val) > budget):
                rewrites.append(Rewrite(
                    kind="pytest_xdist",
                    original=f"--numprocesses {val}",
                    replacement=f"--numprocesses {budget}",
                    reason=f"pytest-xdist requested {val} workers; clamped to {budget}.",
                ))
                tokens[i + 1] = str(budget)
            i += 2
            continue
        i += 1
    return rewrites


def _rewrite_jobs_flag(
    tokens: list[str],
    budget: int,
    *,
    flag_short: str | None,
    flag_long: str | None,
    kind: str,
    tool_label: str,
) -> list[Rewrite]:
    """Generic `-jN` / `--jobs=N` rewriter for make/ninja/cargo.

    `flag_short` is e.g. `-j` (None to skip). `flag_long` is e.g.
    `--jobs` (None to skip). `kind` is the Rewrite.kind tag.
    """
    rewrites: list[Rewrite] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Bare `-j` (no count → unbounded parallelism in make/ninja)
        if flag_short and tok == flag_short:
            # Look ahead: is the next token a number?
            if i + 1 < len(tokens) and _NUMERIC_RE.match(tokens[i + 1]):
                val = int(tokens[i + 1])
                if val > budget:
                    rewrites.append(Rewrite(
                        kind=kind,
                        original=f"{flag_short} {val}",
                        replacement=f"{flag_short} {budget}",
                        reason=f"{tool_label} requested {val} jobs; clamped to {budget}.",
                    ))
                    tokens[i + 1] = str(budget)
                i += 2
                continue
            else:
                # Bare -j with no number → unbounded; insert budget as next token
                rewrites.append(Rewrite(
                    kind=kind,
                    original=flag_short,
                    replacement=f"{flag_short}{budget}",
                    reason=f"{tool_label} bare {flag_short} (unbounded jobs); clamped to {budget}.",
                ))
                tokens[i] = f"{flag_short}{budget}"
                i += 1
                continue
        # -jN attached
        if flag_short and tok.startswith(flag_short) and tok != flag_short and tok[len(flag_short):].isdigit():
            val = int(tok[len(flag_short):])
            if val > budget:
                rewrites.append(Rewrite(
                    kind=kind,
                    original=tok,
                    replacement=f"{flag_short}{budget}",
                    reason=f"{tool_label} requested {val} jobs; clamped to {budget}.",
                ))
                tokens[i] = f"{flag_short}{budget}"
        # --jobs=N
        if flag_long and tok.startswith(f"{flag_long}="):
            val_str = tok.split("=", 1)[1]
            if _NUMERIC_RE.match(val_str) and int(val_str) > budget:
                rewrites.append(Rewrite(
                    kind=kind,
                    original=tok,
                    replacement=f"{flag_long}={budget}",
                    reason=f"{tool_label} requested {val_str} jobs; clamped to {budget}.",
                ))
                tokens[i] = f"{flag_long}={budget}"
        # --jobs N
        if flag_long and tok == flag_long and i + 1 < len(tokens) and _NUMERIC_RE.match(tokens[i + 1]):
            val = int(tokens[i + 1])
            if val > budget:
                rewrites.append(Rewrite(
                    kind=kind,
                    original=f"{flag_long} {val}",
                    replacement=f"{flag_long} {budget}",
                    reason=f"{tool_label} requested {val} jobs; clamped to {budget}.",
                ))
                tokens[i + 1] = str(budget)
            i += 2
            continue
        i += 1
    return rewrites


def inspect_command(command: str, weight: int, max_concurrent: int) -> InspectionResult:
    """Pre-flight inspection. Returns a (possibly-rewritten) command +
    a list of edits applied.

    The rewrites are intentionally conservative: only known overcommit
    patterns. Anything we don't recognise passes through untouched.
    """
    budget = _budget_for(weight, max_concurrent)

    # Tokenize for safe inspection. Fall back to no-op on parse error
    # (e.g., command containing complex shell substitutions we can't
    # parse — let the shell handle it).
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return InspectionResult(command=command, rewrites=[])

    rewrites: list[Rewrite] = []

    if _is_pytest_command(tokens):
        rewrites += _rewrite_pytest_xdist(tokens, budget)

    if _is_make_command(tokens):
        rewrites += _rewrite_jobs_flag(
            tokens, budget,
            flag_short="-j", flag_long="--jobs",
            kind="make_jobs", tool_label="make",
        )

    if _is_ninja_command(tokens):
        rewrites += _rewrite_jobs_flag(
            tokens, budget,
            flag_short="-j", flag_long="--jobs",
            kind="ninja_jobs", tool_label="ninja",
        )

    if _is_cargo_command(tokens):
        rewrites += _rewrite_jobs_flag(
            tokens, budget,
            flag_short=None, flag_long="--jobs",
            kind="cargo_jobs", tool_label="cargo",
        )

    if not rewrites:
        return InspectionResult(command=command, rewrites=[])

    # Re-stringify with shlex.join (Python 3.8+). This may slightly
    # differ from the user's original quoting but is shell-equivalent.
    return InspectionResult(command=shlex.join(tokens), rewrites=rewrites)


def format_rewrite_notice(rewrites: Iterable[Rewrite]) -> str:
    """Human-readable summary the runner prepends to stdout when
    rewrites were applied. Helps the agent learn to ask for the right
    thing next time."""
    lines = ["[hares: pre-flight rewrote command to fit resource caps]"]
    for r in rewrites:
        lines.append(f"  • {r.original!r} → {r.replacement!r}  ({r.reason})")
    return "\n".join(lines) + "\n"
