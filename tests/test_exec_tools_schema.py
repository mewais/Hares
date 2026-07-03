"""Guards for the shared exec-tool input schema.

execute_command and execute_command_high_memory share ONE input schema
(`EXEC_INPUT_SCHEMA`) across both the shell and combined servers, so the
agent-facing parameter guidance can't silently diverge between modes.
These tests fail loudly if a future change drops a field, a per-param
description, or the mem_limit_mb floor, or if the two descriptor builders
stop advertising the same schema.
"""

from __future__ import annotations

from hares.exec_tools import (
    EXEC_INPUT_SCHEMA,
    shell_exec_tool_descriptors,
    combined_exec_tool_descriptors,
)


def test_every_param_has_a_description():
    """No bare parameters — each one carries agent-facing guidance."""
    for name, spec in EXEC_INPUT_SCHEMA["properties"].items():
        assert spec.get("description", "").strip(), f"{name} has no description"


def test_mem_limit_mb_has_positive_minimum():
    """The high-memory approval path relies on the schema rejecting a
    non-positive mem_limit_mb (belt-and-suspenders alongside the
    in-handler guard in _execute_high_memory)."""
    assert EXEC_INPUT_SCHEMA["properties"]["mem_limit_mb"].get("minimum", 0) >= 1


def test_shell_and_combined_advertise_the_same_schema():
    """Both servers must expose identical exec input schemas — only the
    top-level tool description is allowed to differ (bwrap framing)."""
    shell = shell_exec_tool_descriptors(None, mem_limit_mb=7168, mem_limit_max_mb=64000)
    combined = combined_exec_tool_descriptors(None, mem_limit_mb=7168, mem_limit_max_mb=64000)
    for s_tool, c_tool in zip(shell, combined):
        assert s_tool.name == c_tool.name
        assert s_tool.inputSchema == c_tool.inputSchema
