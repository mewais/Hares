"""Drift guards for the two exec-tool input schemas.

``hares.exec_tools`` deliberately keeps SHELL_ and COMBINED_ variants
of the exec input schema and the elicit-decline template verbatim,
because the advertised tool surface was frozen byte-identical during
the server-dedup refactor. Nothing else enforces that the two schemas
stay structurally aligned, so a future contributor who adds a field to
one and not the other would silently diverge the shell and combined
tool surfaces. These tests fail loudly if that happens.
"""

from __future__ import annotations

from hares.exec_tools import (
    SHELL_EXEC_INPUT_SCHEMA,
    COMBINED_EXEC_INPUT_SCHEMA,
)


def test_exec_schemas_have_identical_field_sets():
    shell_props = set(SHELL_EXEC_INPUT_SCHEMA["properties"])
    combined_props = set(COMBINED_EXEC_INPUT_SCHEMA["properties"])
    assert shell_props == combined_props, (
        "shell/combined exec schemas diverged: "
        f"only-shell={shell_props - combined_props}, "
        f"only-combined={combined_props - shell_props}"
    )
    assert (
        SHELL_EXEC_INPUT_SCHEMA["required"]
        == COMBINED_EXEC_INPUT_SCHEMA["required"]
    )


def test_exec_schemas_agree_on_field_types():
    for name, shell_spec in SHELL_EXEC_INPUT_SCHEMA["properties"].items():
        combined_spec = COMBINED_EXEC_INPUT_SCHEMA["properties"][name]
        assert shell_spec.get("type") == combined_spec.get("type"), name


def test_mem_limit_mb_has_positive_minimum():
    """The high-memory approval path relies on the schema rejecting a
    non-positive mem_limit_mb (belt-and-suspenders alongside the
    in-handler guard in _execute_high_memory)."""
    for schema in (SHELL_EXEC_INPUT_SCHEMA, COMBINED_EXEC_INPUT_SCHEMA):
        mem = schema["properties"]["mem_limit_mb"]
        assert mem.get("minimum", 0) >= 1, mem
