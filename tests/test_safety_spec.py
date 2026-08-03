#!/usr/bin/env python3
"""Tests for the safety plane's data model: rules, verdicts, documents."""

from __future__ import annotations

import pytest
from mewbo_core.safety.spec import (
    PathGuardRule,
    SafetyDocument,
    SafetyRule,
    SessionBudgetRule,
    ToolCallObservation,
    ToolMatchRule,
    TurnObservation,
)
from pydantic import ValidationError


class TestToolCallObservation:
    def test_text_values_walks_nested_arguments(self):
        obs = ToolCallObservation(
            tool_id="aider_shell_tool",
            capability="execute",
            arguments={"command": "rm -rf .mewbo", "nested": {"cwd": "/tmp"}, "flags": ["a", "b"]},
        )
        assert set(obs.text_values()) == {"rm -rf .mewbo", "/tmp", "a", "b"}

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ToolCallObservation(tool_id="x", bogus=1)  # type: ignore[call-arg]

    def test_is_frozen(self):
        obs = ToolCallObservation(tool_id="x")
        with pytest.raises(ValidationError):
            obs.tool_id = "y"  # type: ignore[misc]


class TestToolMatchRule:
    def test_matches_by_regex(self):
        rule = ToolMatchRule(
            name="no-payments", decision="deny", tools=["mcp__payments__.*"]
        )
        verdict = rule.on_tool_call(
            ToolCallObservation(tool_id="mcp__payments__refund", capability="execute")
        )
        assert verdict is not None
        assert verdict.decision == "deny"
        assert verdict.rule == "no-payments"

    def test_no_match_returns_none(self):
        rule = ToolMatchRule(name="no-payments", tools=["mcp__payments__.*"])
        assert rule.on_tool_call(ToolCallObservation(tool_id="read_file")) is None

    def test_matches_by_tier(self):
        rule = ToolMatchRule(name="no-writes", decision="deny", tiers=["write"])
        assert rule.on_tool_call(
            ToolCallObservation(tool_id="file_edit_tool", capability="write")
        ) is not None
        assert rule.on_tool_call(
            ToolCallObservation(tool_id="read_file", capability="read")
        ) is None

    def test_invalid_regex_rejected_at_definition(self):
        with pytest.raises(ValidationError):
            ToolMatchRule(name="bad", tools=["("])

    def test_never_evaluates_turns(self):
        rule = ToolMatchRule(name="x", tools=[".*"])
        assert rule.on_turn(TurnObservation(step=0, elapsed_seconds=0.0)) is None


class TestPathGuardRule:
    def test_matches_write_targeting_guarded_path(self):
        rule = PathGuardRule(
            name="guard", decision="deny", paths=(".mewbo/policy",), tiers=("write",)
        )
        verdict = rule.on_tool_call(
            ToolCallObservation(
                tool_id="file_edit_tool",
                capability="write",
                arguments={"file_path": "/proj/.mewbo/policy/house-rules.md"},
            )
        )
        assert verdict is not None
        assert verdict.rule == "guard"

    def test_matches_shell_command_containing_path(self):
        rule = PathGuardRule(name="guard", paths=(".mewbo/policy",), tiers=("execute",))
        verdict = rule.on_tool_call(
            ToolCallObservation(
                tool_id="aider_shell_tool",
                capability="execute",
                arguments={"command": "rm -rf .mewbo/policy"},
            )
        )
        assert verdict is not None

    def test_read_tier_exempt_by_default(self):
        rule = PathGuardRule(name="guard", paths=(".mewbo/policy",))
        verdict = rule.on_tool_call(
            ToolCallObservation(
                tool_id="read_file",
                capability="read",
                arguments={"path": ".mewbo/policy/house-rules.md"},
            )
        )
        assert verdict is None

    def test_unrelated_write_not_matched(self):
        rule = PathGuardRule(name="guard", paths=(".mewbo/policy",), tiers=("write",))
        verdict = rule.on_tool_call(
            ToolCallObservation(
                tool_id="file_edit_tool",
                capability="write",
                arguments={"file_path": "/proj/src/app.py"},
            )
        )
        assert verdict is None

    def test_backslash_paths_normalised(self):
        rule = PathGuardRule(name="guard", paths=(".mewbo/policy",), tiers=("write",))
        verdict = rule.on_tool_call(
            ToolCallObservation(
                tool_id="file_edit_tool",
                capability="write",
                arguments={"file_path": r"C:\proj\.mewbo\policy\house-rules.md"},
            )
        )
        assert verdict is not None


class TestSessionBudgetRule:
    def test_matches_when_step_ceiling_crossed(self):
        rule = SessionBudgetRule(name="turn-ceiling", decision="deny", max_steps=40)
        assert rule.on_turn(TurnObservation(step=39, elapsed_seconds=0.0)) is None
        verdict = rule.on_turn(TurnObservation(step=40, elapsed_seconds=0.0))
        assert verdict is not None
        assert verdict.rule == "turn-ceiling"

    def test_matches_when_wall_ceiling_crossed(self):
        rule = SessionBudgetRule(name="deadline", max_seconds=60.0)
        assert rule.on_turn(TurnObservation(step=0, elapsed_seconds=59.0)) is None
        assert rule.on_turn(TurnObservation(step=0, elapsed_seconds=60.0)) is not None

    def test_empty_budget_never_matches(self):
        rule = SessionBudgetRule(name="inert")
        assert rule.on_turn(TurnObservation(step=10_000, elapsed_seconds=10_000.0)) is None

    def test_never_evaluates_tool_calls(self):
        rule = SessionBudgetRule(name="x", max_steps=1)
        assert rule.on_tool_call(ToolCallObservation(tool_id="anything")) is None


class TestSafetyRuleParse:
    def test_discriminates_by_kind(self):
        parsed = SafetyRule.parse(
            {"kind": "tool.match", "name": "x", "tools": ["read_file"]}
        )
        assert isinstance(parsed, ToolMatchRule)

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            SafetyRule.parse({"kind": "bogus", "name": "x"})

    def test_bad_name_rejected(self):
        with pytest.raises(ValidationError):
            SafetyRule.parse({"kind": "tool.match", "name": "Not Valid"})


class TestSafetyDocument:
    def test_parses_frontmatter_and_body(self):
        raw = """---
name: house-rules
description: Refuse destructive shell.
rules:
  - kind: tool.match
    name: no-shell
    decision: deny
    reason: Shell is operator-only.
    tools: ["aider_shell_tool"]
---

This is the rationale. Never sent to a model.
"""
        doc = SafetyDocument.parse(raw, name="house-rules")
        assert doc.name == "house-rules"
        assert len(doc.rules) == 1
        assert doc.rules[0].name == "no-shell"
        assert "rationale" in doc.body

    def test_missing_frontmatter_raises(self):
        with pytest.raises(ValueError, match="front matter"):
            SafetyDocument.parse("just a body, no frontmatter", name="x")

    def test_unknown_top_level_key_rejected(self):
        raw = "---\nname: x\nbogus_key: 1\n---\nbody\n"
        with pytest.raises(ValidationError):
            SafetyDocument.parse(raw, name="x")

    def test_malformed_yaml_raises_value_error(self):
        raw = "---\nname: [unterminated\n---\nbody\n"
        with pytest.raises(ValueError, match="not valid YAML"):
            SafetyDocument.parse(raw, name="x")

    def test_name_defaults_from_filename(self):
        raw = "---\ndescription: no explicit name\n---\nbody\n"
        doc = SafetyDocument.parse(raw, name="from-filename")
        assert doc.name == "from-filename"
