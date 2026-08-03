"""A JSON-STRING argument for a declared list field must still validate.

Every case here is built from a payload a real model actually sent, recorded on
this deployment's own transcripts — not from a hand-typed guess at the shape.
That distinction matters for this defect specifically: the whole failure is a
model composing an argument DIFFERENTLY from how the schema author imagined it,
so a fixture written by the schema author's own intuition is exactly the fixture
that cannot reproduce it.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker
from mewbo_graph.plugins.wiki._base import WikiSessionTool
from pydantic import BaseModel, ConfigDict, Field

# ── Fixtures mirroring the real args models ────────────────────────────────────


class _BlocksArgs(BaseModel):
    """Mirrors ``WikiEmitAnswerArgs`` — a required list field."""

    model_config = ConfigDict(extra="forbid")
    blocks: list[dict[str, Any]] = Field(min_length=2)


class _OptionalListArgs(BaseModel):
    """Mirrors ``WikiCodeSearchArgs`` — an OPTIONAL list behind a union."""

    model_config = ConfigDict(extra="forbid")
    query: str
    types: list[str] | None = None


def _step(tool_input: Any) -> ActionStep:
    return ActionStep(
        tool_id="wiki_emit_answer", operation="set", tool_input=tool_input
    )


# The shape recorded verbatim in the transcript: a JSON array, as a string,
# with the leading/trailing newlines the model emitted around it.
_REAL_BLOCKS = [
    {"kind": "p", "text": "LSPServerManager is a per-session language server manager."},
    {"kind": "sources", "items": ["packages/mewbo_tools/.../manager.py"]},
]
_REAL_BLOCKS_AS_STRING = "\n" + json.dumps(_REAL_BLOCKS) + "\n"


def test_blocks_sent_as_json_string_validates() -> None:
    """The exact `wiki_emit_answer` failure: `blocks` arrives as a string."""
    parsed = WikiSessionTool._parse_args(
        _BlocksArgs, _step({"blocks": _REAL_BLOCKS_AS_STRING})
    )
    assert not isinstance(parsed, MockSpeaker), "should no longer be a validation error"
    assert parsed.blocks == _REAL_BLOCKS


def test_blocks_sent_as_a_real_list_is_untouched() -> None:
    """The correct shape must keep working byte for byte."""
    parsed = WikiSessionTool._parse_args(_BlocksArgs, _step({"blocks": _REAL_BLOCKS}))
    assert not isinstance(parsed, MockSpeaker)
    assert parsed.blocks == _REAL_BLOCKS


def test_optional_list_behind_a_union_is_decoded() -> None:
    """`wiki_code_search.types` — `list[str] | None`, sent as '["Function"]'."""
    parsed = WikiSessionTool._parse_args(
        _OptionalListArgs,
        _step({"query": "LSPServerManager", "types": '["Function", "Class"]'}),
    )
    assert not isinstance(parsed, MockSpeaker)
    assert parsed.types == ["Function", "Class"]


def test_single_key_wrapper_naming_the_field_is_unwrapped() -> None:
    """`wiki_commit_plan` sent `'{"pages": [...]}'` for its `pages` field."""

    class _PagesArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        pages: list[dict[str, Any]]

    payload = [{"id": "overview", "parent": None}]
    parsed = WikiSessionTool._parse_args(
        _PagesArgs, _step({"pages": json.dumps({"pages": payload})})
    )
    assert not isinstance(parsed, MockSpeaker)
    assert parsed.pages == payload


# ── The narrowness guarantees — a real mistake must still fail ────────────────


@pytest.mark.parametrize(
    "value",
    [
        "not json at all",
        '"a bare json string"',
        "42",
        '{"unrelated": [1, 2]}',
    ],
)
def test_a_string_that_is_not_the_declared_container_still_fails(value: str) -> None:
    """Coercion must not launder an argument that is genuinely wrong."""
    parsed = WikiSessionTool._parse_args(_BlocksArgs, _step({"blocks": value}))
    assert isinstance(parsed, MockSpeaker), f"{value!r} should still be refused"


def test_a_scalar_field_given_a_json_string_is_left_alone() -> None:
    """Only list/dict-declared fields are eligible — `query` is a plain str."""
    parsed = WikiSessionTool._parse_args(
        _OptionalListArgs, _step({"query": '["still", "a", "string"]'})
    )
    assert not isinstance(parsed, MockSpeaker)
    assert parsed.query == '["still", "a", "string"]'


def test_min_length_still_applies_after_decoding() -> None:
    """Decoding feeds Pydantic; it does not bypass the model's own rules."""
    parsed = WikiSessionTool._parse_args(
        _BlocksArgs, _step({"blocks": json.dumps([{"kind": "p", "text": "only one"}])})
    )
    assert isinstance(parsed, MockSpeaker), "min_length=2 must still be enforced"
