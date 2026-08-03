#!/usr/bin/env python3
"""Tests for ``SafetyPlane``: discovery, self-protection, disclosure, inertness."""

from __future__ import annotations

from pathlib import Path

from mewbo_core.safety.plane import SELF_PROTECTION_RULE, SafetyPlane
from mewbo_core.safety.spec import SafetyDocument, ToolCallObservation, TurnObservation


def _write_policy(root: Path, name: str, body: str) -> None:
    policy_dir = root / ".mewbo" / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / f"{name}.md").write_text(body, encoding="utf-8")


class TestInertness:
    """A default install must do ZERO work: no filesystem, no rule, no plane."""

    def test_disabled_returns_none_without_touching_disk(self, tmp_path: Path):
        # A directory that does not exist would raise on any real stat/glob —
        # proving load() never reaches the filesystem when disabled.
        missing = tmp_path / "does-not-exist"
        assert SafetyPlane.load(str(missing), enabled=False) is None

    def test_enabled_but_no_cwd_returns_none(self):
        assert SafetyPlane.load(None, enabled=True) is None

    def test_enabled_with_no_documents_still_holds_self_protection_only(
        self, tmp_path: Path
    ):
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None
        assert plane.rules == (SELF_PROTECTION_RULE,)


class TestDiscovery:
    def test_loads_rules_from_policy_and_monitor_dirs(self, tmp_path: Path):
        _write_policy(
            tmp_path,
            "house-rules",
            """---
name: house-rules
description: test
rules:
  - kind: tool.match
    name: no-payments
    decision: deny
    tools: ["mcp__payments__.*"]
---
body
""",
        )
        monitor_dir = tmp_path / ".mewbo" / "monitor"
        monitor_dir.mkdir(parents=True)
        (monitor_dir / "budget.md").write_text(
            """---
name: budget
description: test
rules:
  - kind: session.budget
    name: turn-ceiling
    decision: deny
    max_steps: 40
---
body
""",
            encoding="utf-8",
        )
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None
        names = [r.name for r in plane.rules]
        assert "no-payments" in names
        assert "turn-ceiling" in names

    def test_malformed_document_is_skipped_not_fatal(self, tmp_path: Path):
        _write_policy(tmp_path, "broken", "not even frontmatter")
        _write_policy(
            tmp_path,
            "good",
            """---
name: good
description: test
rules:
  - kind: tool.match
    name: ok-rule
    tools: ["x"]
---
body
""",
        )
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None
        assert "ok-rule" in [r.name for r in plane.rules]

    def test_disabled_document_contributes_no_rules(self, tmp_path: Path):
        _write_policy(
            tmp_path,
            "off",
            """---
name: off
description: test
enabled: false
rules:
  - kind: tool.match
    name: should-not-appear
    tools: ["x"]
---
body
""",
        )
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None
        assert "should-not-appear" not in [r.name for r in plane.rules]


class TestSelfProtection:
    """The headline property: the plane cannot be edited or deleted via a tool call."""

    def test_write_to_policy_dir_is_denied(self):
        plane = SafetyPlane(documents=())
        verdict = plane.evaluate_tool_call(
            ToolCallObservation(
                tool_id="file_edit_tool",
                capability="write",
                arguments={"file_path": "/proj/.mewbo/policy/house-rules.md"},
            )
        )
        assert verdict is not None
        assert verdict.decision == "deny"
        assert verdict.rule == "safety-plane-integrity"

    def test_write_to_monitor_dir_is_denied(self):
        plane = SafetyPlane(documents=())
        verdict = plane.evaluate_tool_call(
            ToolCallObservation(
                tool_id="file_edit_tool",
                capability="write",
                arguments={"file_path": "/proj/.mewbo/monitor/budget.md"},
            )
        )
        assert verdict is not None
        assert verdict.rule == "safety-plane-integrity"

    def test_shell_deletion_of_policy_dir_is_denied(self):
        plane = SafetyPlane(documents=())
        verdict = plane.evaluate_tool_call(
            ToolCallObservation(
                tool_id="aider_shell_tool",
                capability="execute",
                arguments={"command": "rm -rf .mewbo/policy"},
            )
        )
        assert verdict is not None
        assert verdict.rule == "safety-plane-integrity"

    def test_a_document_rule_cannot_override_self_protection(self, tmp_path: Path):
        # An author writes a permissive rule for the exact path the built-in
        # guard protects. First-match-wins over the PREPENDED list means the
        # built-in guard is always checked before any document rule — so the
        # widened rule below can never be reached for this path.
        _write_policy(
            tmp_path,
            "widen",
            """---
name: widen
description: attempts to allow writes to the plane's own directory
rules:
  - kind: path.guard
    name: allow-everything
    decision: allow
    paths: [".mewbo/policy"]
    tiers: ["write"]
---
body
""",
        )
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None
        verdict = plane.evaluate_tool_call(
            ToolCallObservation(
                tool_id="file_edit_tool",
                capability="write",
                arguments={"file_path": str(tmp_path / ".mewbo/policy/widen.md")},
            )
        )
        assert verdict is not None
        assert verdict.decision == "deny"
        assert verdict.rule == "safety-plane-integrity"

    def test_read_of_policy_dir_is_not_blocked(self):
        # The plane itself must remain readable — only write/execute are guarded.
        plane = SafetyPlane(documents=())
        verdict = plane.evaluate_tool_call(
            ToolCallObservation(
                tool_id="read_file",
                capability="read",
                arguments={"path": ".mewbo/policy/house-rules.md"},
            )
        )
        assert verdict is None


class TestFirstMatchWins:
    def test_earlier_rule_wins_over_later_allow_exception(self, tmp_path: Path):
        _write_policy(
            tmp_path,
            "ordered",
            """---
name: ordered
description: test
rules:
  - kind: tool.match
    name: deny-payments
    decision: deny
    tools: ["mcp__payments__.*"]
  - kind: tool.match
    name: allow-refund
    decision: allow
    tools: ["mcp__payments__refund"]
---
body
""",
        )
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None
        verdict = plane.evaluate_tool_call(
            ToolCallObservation(tool_id="mcp__payments__refund", capability="execute")
        )
        assert verdict is not None
        assert verdict.rule == "deny-payments"


class TestEvaluateTurn:
    def test_session_budget_rule_fires_at_boundary(self):
        doc = SafetyDocument.parse(
            """---
name: budget
description: test
rules:
  - kind: session.budget
    name: ceiling
    decision: deny
    max_steps: 5
---
body
""",
            name="budget",
        )
        plane = SafetyPlane((doc,))
        assert plane.evaluate_turn(TurnObservation(step=4, elapsed_seconds=0.0)) is None
        verdict = plane.evaluate_turn(TurnObservation(step=5, elapsed_seconds=0.0))
        assert verdict is not None
        assert verdict.rule == "ceiling"


class TestDisclosure:
    def test_lists_every_active_rule_and_what_it_inspects(self, tmp_path: Path):
        doc = SafetyDocument.parse(
            """---
name: house-rules
description: test
rules:
  - kind: tool.match
    name: no-payments
    tools: ["mcp__payments__.*"]
---
body
""",
            name="house-rules",
            source_path=str(tmp_path / ".mewbo/policy/house-rules.md"),
        )
        plane = SafetyPlane((doc,))
        disclosure = plane.disclosure()
        assert disclosure.active is True
        assert disclosure.inspects_tool_calls is True
        names = [r.name for r in disclosure.rules]
        assert "safety-plane-integrity" in names
        assert "no-payments" in names
        for rule in disclosure.rules:
            assert rule.inspects  # every rule can describe itself
