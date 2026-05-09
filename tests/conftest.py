"""Shared pytest fixtures.

The autouse ``_clean_hares_env`` fixture below wipes every ``HARES_*``
environment variable before each test runs, so tests start from a
known-empty state regardless of what the developer's shell or the CI
environment exports.

Why this exists: tests that exercise an env-driven loader (sandbox
config, audit, doctor, cluster configs, etc.) are sensitive to outer
state. The most common case is a contributor following the
CONTRIBUTING.md guidance to ``HARES_SANDBOX_DISABLED=1 pytest`` on a
host without bwrap — that flag silently overrides the loader and
breaks every test that wanted to exercise the bwrap-enabled path.

Policy this fixture enforces:

  - Tests start with NO ``HARES_*`` env var set, even if the
    developer's shell exports some.
  - Tests that DEPEND on an env var must set it explicitly via
    ``monkeypatch.setenv(...)``. The fixture wiping happens first;
    the test's setenv takes effect afterwards.
  - Adding a new ``HARES_*`` env var requires no test-config update —
    the prefix wipe catches it automatically.

If you ever need a test that VERIFIES env-leak behavior (e.g.,
"what does the loader do when HARES_FOO is set in the parent shell"),
override the fixture locally for that test by re-setting the var
inside the test body — not by disabling the fixture.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_hares_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe all HARES_* env vars before each test.

    monkeypatch.delenv with raising=False is a no-op for unset vars,
    so this is cheap when the env is already clean.
    """
    for var in [k for k in os.environ if k.startswith("HARES_")]:
        monkeypatch.delenv(var, raising=False)
