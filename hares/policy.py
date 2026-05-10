"""Command policy engine — deny list and suspicious list with elicitation.

Two tiers, evaluated in order:

  DENY    (--deny patterns)     Immediate rejection. No elicitation. The
                                operator has decided this command never runs.

  SUSPECT (--suspect patterns)  Elicitation if a human is in the loop.
                                Claude Code shows a blocking "Allow / Decline"
                                dialog; user decides per-call. If the client
                                does not support elicitation (non-interactive
                                or headless), fails closed → treated as deny.

  ALLOW   (everything else)     Runs immediately, no interaction.

For automated multi-LLM flows (no human): don't use the suspicious tier.
Put anything you want blocked into --deny. Everything else runs.

Pattern matching
----------------
Patterns use fnmatch-style glob syntax matched against the full command
string passed to execute_command. A leading/trailing ``*`` is implied, so
``git push`` matches any command containing the literal string "git push".
Case-sensitive.

Examples:
  --deny="sudo *,rm -rf /*"
  --suspect="*git push*,*-X POST*,*gh pr create*"

Default suspicious patterns
---------------------------
Applied when --suspect is not explicitly set. Covers the most common
operations that write to remote systems and are surprising when an agent
does them without asking. Deliberately narrow — the goal is minimal
interruption, not maximum paranoia.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# ── Canonical decisions ─────────────────────────────────────────────────────

class Decision(Enum):
    ALLOW   = "allow"    # run immediately
    DENY    = "deny"     # reject, return error
    ELICIT  = "elicit"   # ask the user before running


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    matched_pattern: Optional[str] = None   # which pattern triggered this
    tier: Optional[str] = None              # "deny" | "suspect"

    @property
    def message(self) -> str:
        if self.decision is Decision.DENY:
            return (
                f"Command rejected by Hares denylist "
                f"(pattern: {self.matched_pattern!r}). "
                "This operation is not permitted. Use a different approach "
                "or ask the operator to update the deny list."
            )
        if self.decision is Decision.ELICIT:
            return (
                f"Command matches suspicious pattern "
                f"{self.matched_pattern!r}. Awaiting user approval."
            )
        return ""


# ── Default patterns ─────────────────────────────────────────────────────────

# Nothing in the default deny list — operators configure their own.
DEFAULT_DENY_PATTERNS: tuple[str, ...] = ()

# Operations that write to remote systems and are surprising when an agent
# does them silently. Matched as substrings (fnmatch with leading/trailing *).
DEFAULT_SUSPECT_PATTERNS: tuple[str, ...] = (
    # Git remote writes
    "*git push*",
    # HTTP write methods
    "*-X POST*", "*-X PUT*", "*-X DELETE*", "*-X PATCH*",
    "*--request POST*", "*--request PUT*",
    "*--request DELETE*", "*--request PATCH*",
    # GitHub CLI writes
    "*gh pr create*", "*gh pr merge*", "*gh pr close*",
    "*gh issue create*", "*gh issue close*",
    "*gh release create*", "*gh release delete*",
    "*gh repo delete*",
    # NPM / PyPI publish
    "*npm publish*", "*twine upload*", "*poetry publish*",
)


# ── Policy engine ─────────────────────────────────────────────────────────────

class PolicyEngine:
    """Evaluates a command against the deny + suspicious lists.

    Instantiate once at server startup; call ``check(command)`` per
    execute_command invocation.
    """

    def __init__(
        self,
        deny_patterns: tuple[str, ...] = DEFAULT_DENY_PATTERNS,
        suspect_patterns: tuple[str, ...] = DEFAULT_SUSPECT_PATTERNS,
    ) -> None:
        # Normalise: wrap each pattern with * so it matches as a substring
        # unless the operator already included wildcards.
        self._deny = tuple(_normalise(p) for p in deny_patterns)
        self._suspect = tuple(_normalise(p) for p in suspect_patterns)

    @property
    def active(self) -> bool:
        """False when both lists are empty — no policy overhead."""
        return bool(self._deny or self._suspect)

    def check(self, command: str) -> PolicyResult:
        """Return the policy decision for *command*.

        Evaluation order: deny → suspect → allow.
        """
        for pattern in self._deny:
            if fnmatch.fnmatch(command, pattern):
                logger.info("Policy DENY: command=%r pattern=%r", command, pattern)
                return PolicyResult(
                    Decision.DENY, matched_pattern=pattern, tier="deny"
                )

        for pattern in self._suspect:
            if fnmatch.fnmatch(command, pattern):
                logger.info("Policy ELICIT: command=%r pattern=%r", command, pattern)
                return PolicyResult(
                    Decision.ELICIT, matched_pattern=pattern, tier="suspect"
                )

        return PolicyResult(Decision.ALLOW)


# ── Elicitation helper ────────────────────────────────────────────────────────

async def elicit_approval(session, command: str, result: PolicyResult) -> bool:
    """Send an MCP elicitation request and return True if the user approved.

    Falls back to False (deny) when:
      - The client does not support elicitation (AttributeError / Exception).
      - The user declines or cancels.

    The fail-closed fallback means non-interactive clients (headless multi-LLM
    flows) treat the suspicious tier as a deny tier — correct behaviour.
    """
    message = (
        f"**Command requires approval**\n\n"
        f"Pattern matched: `{result.matched_pattern}`\n\n"
        f"```\n{command}\n```\n\n"
        "This command may write to a remote system. Allow it to run?"
    )
    # Empty schema = simple Accept / Decline dialog, no form fields needed.
    schema: dict = {"type": "object", "properties": {}}
    try:
        response = await session.create_elicitation(
            message=message,
            requestedSchema=schema,
        )
        action = getattr(response, "action", "cancel")
        approved = action == "accept"
        logger.info(
            "Elicitation response: command=%r action=%r approved=%s",
            command, action, approved,
        )
        return approved
    except AttributeError:
        # Older SDK without elicitation support.
        logger.debug(
            "Elicitation not available in this MCP SDK version; "
            "treating suspicious command as denied: %r", command,
        )
        return False
    except Exception as exc:
        # Client declined to participate (headless, automated, etc.).
        logger.debug(
            "Elicitation failed (%s); treating suspicious command as denied: %r",
            exc, command,
        )
        return False


# ── Loader from CLI args ──────────────────────────────────────────────────────

def load_policy(
    deny_arg: Optional[str] = None,
    suspect_arg: Optional[str] = None,
) -> Optional[PolicyEngine]:
    """Build a PolicyEngine from CLI --deny / --suspect arguments.

    Returns None when both args are absent AND the defaults are empty —
    callers can skip the policy check entirely for zero overhead.
    Returns an engine with defaults when args are absent but defaults exist.
    """
    if deny_arg is None and suspect_arg is None:
        # Use defaults.
        if not DEFAULT_DENY_PATTERNS and not DEFAULT_SUSPECT_PATTERNS:
            return None
        return PolicyEngine()

    deny = _parse_patterns(deny_arg) if deny_arg is not None else DEFAULT_DENY_PATTERNS
    suspect = _parse_patterns(suspect_arg) if suspect_arg is not None else DEFAULT_SUSPECT_PATTERNS
    return PolicyEngine(deny_patterns=deny, suspect_patterns=suspect)


# ── Internals ─────────────────────────────────────────────────────────────────

def _parse_patterns(raw: str) -> tuple[str, ...]:
    """Split a comma-separated pattern string into individual patterns."""
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _normalise(pattern: str) -> str:
    """Ensure a pattern works as a substring match if no wildcards present."""
    if "*" not in pattern and "?" not in pattern:
        return f"*{pattern}*"
    return pattern
