"""Tests for the declared tool-argument schema type.

Each case pins a mistake the type exists to catch at DEFINITION — a schema is
built at import time, so a refusal here fails the process rather than surfacing
as a tool the model can never call correctly.
"""

from __future__ import annotations

import pytest
from mewbo_core.tooling.tool_registry import SHELL_SCHEMA, SHELL_SESSION_SCHEMA
from mewbo_core.tooling.tool_schema import ToolParameter, ToolSchema


def test_required_must_name_a_declared_property():
    """A required argument the schema never declares can never be satisfied."""
    with pytest.raises(ValueError, match="not declared properties"):
        ToolSchema(
            properties={"a": ToolParameter(type="string", description="x")},
            required=("b",),
        )


def test_numeric_bounds_are_refused_on_a_non_numeric_parameter():
    """minimum/maximum on a string say nothing and would mislead a caller."""
    with pytest.raises(ValueError, match="meaningless"):
        ToolParameter(type="string", description="x", minimum=0)


def test_inverted_bounds_are_refused():
    """A range no value can fall inside is a typo, not a constraint."""
    with pytest.raises(ValueError, match="exceeds maximum"):
        ToolParameter(type="integer", description="x", minimum=10, maximum=1)


def test_enum_is_refused_on_a_non_string_parameter():
    """A closed set is spelled with strings; anywhere else it is a mistake."""
    with pytest.raises(ValueError, match="only meaningful on a string"):
        ToolParameter(type="integer", description="x", enum=("a",))


def test_a_description_is_mandatory():
    """An undescribed parameter is one the caller has to guess at."""
    with pytest.raises(ValueError):
        ToolParameter(type="string", description="")


def test_unknown_fields_are_forbidden():
    """A misspelled key must fail loudly, not vanish into the schema."""
    with pytest.raises(ValueError):
        ToolParameter(type="string", description="x", maximumm=3)


def test_render_emits_declared_bounds_and_omits_absent_ones():
    """Only what was declared reaches the wire — no null defaults, no noise."""
    bounded = ToolParameter(
        type="integer", description="ms to wait", default=0, minimum=0, maximum=30000
    ).as_json_schema()
    assert bounded == {
        "type": "integer",
        "description": "ms to wait",
        "default": 0,
        "minimum": 0,
        "maximum": 30000,
    }
    plain = ToolParameter(type="string", description="a path").as_json_schema()
    assert plain == {"type": "string", "description": "a path"}


def test_render_produces_a_json_schema_object():
    """The rendered shape is what a provider binds."""
    rendered = ToolSchema(
        properties={"a": ToolParameter(type="string", description="x")},
        required=("a",),
    ).as_json_schema()
    assert rendered["type"] == "object"
    assert rendered["required"] == ["a"]
    assert rendered["properties"] == {"a": {"type": "string", "description": "x"}}


# -- the shipped declarations ---------------------------------------------


@pytest.mark.parametrize("schema", [SHELL_SCHEMA, SHELL_SESSION_SCHEMA])
def test_shipped_schemas_describe_every_argument(schema):
    """Every argument the model can pass carries prose explaining it."""
    for name, parameter in schema.properties.items():
        assert parameter.description.strip(), f"{name} has no description"


def test_shell_session_bounds_match_what_the_code_enforces():
    """A declared bound that disagrees with the implementation is worse than none."""
    from mewbo_tools.integration.shell_session import MAX_READ_WAIT_MS

    wait_ms = SHELL_SESSION_SCHEMA.properties["wait_ms"]
    assert wait_ms.maximum == MAX_READ_WAIT_MS


def test_shell_timeout_default_matches_the_implementation():
    """The advertised default is the one a caller actually gets."""
    from mewbo_tools.integration.shell_session import DEFAULT_TIMEOUT_S

    assert SHELL_SCHEMA.properties["timeout"].default == DEFAULT_TIMEOUT_S


def test_shell_session_operations_match_the_validated_set():
    """The schema's enum and the argument model cannot drift apart."""
    import typing

    from mewbo_tools.integration.shell_session import ShellSessionArgs

    declared = set(SHELL_SESSION_SCHEMA.properties["operation"].enum)
    accepted = set(typing.get_args(ShellSessionArgs.model_fields["operation"].annotation))
    assert declared == accepted
