#!/usr/bin/env python3
"""The safety plane's data model: observations, rules, verdicts, documents.

Two observation points, one rule vocabulary. A **gate** inspects a resolved
tool call before it executes; an **observer** inspects session progress between
turns. Both are the same plane — one parser, one rule union, one verdict shape —
because designing them apart produces two half-overlapping mechanisms with two
config surfaces and two context costs.

Three properties hold everything else up:

**Nothing here calls a model.** Every rule kind is decidable from data already
in hand at its seam: a tool id, its capability tier, the strings in its
arguments, a step index, an elapsed second count. A guardrail that spends tokens
to compare two integers costs more than the thing it guards, and a guardrail
whose verdict varies run to run on identical input cannot be audited. The
``Field(discriminator="kind")`` union below is the seam where a model-judged
kind could later be added with no dispatch changes anywhere; none ships until a
use case produces one that is genuinely undecidable without inference.

**Models never import I/O.** The tool call, the clock and the step counter
arrive as method ARGS, exactly as ``TriggerSpec.matches(payload)`` and
``next_fire_at(now)`` do. That is what lets every rule be tested with a literal
observation and no session, no clock patch and no network.

**The prose body is never sent to a model.** A document's markdown body is
rationale for the human reading the file and material for the disclosure a user
sees. It enters no prompt, so a long policy costs exactly zero tokens.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

# Front matter is delimited the same way skills and agent definitions delimit
# theirs. Deliberately a local copy rather than a shared import: factoring the
# three together means editing two working loaders to save one line, which is
# not the smallest diff that solves this problem.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Same shape skills and agents accept, so an author who has written one of those
# does not have to learn a second naming rule.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,62}[a-z0-9]?$")

CapabilityTier = Literal["read", "write", "execute"]
Decision = Literal["allow", "warn", "deny"]

# The directory a project keeps its plane in, and the two conventional
# subdirectories. The plane reads BOTH into one ordered rule list — the split is
# a convention that helps an author organise, not a type boundary, so a budget
# rule filed under ``policy/`` still works.
SAFETY_DIRNAME = ".mewbo"
POLICY_SUBDIR = "policy"
MONITOR_SUBDIR = "monitor"


class ToolCallObservation(BaseModel):
    """A fully-resolved tool call, captured before it executes.

    The gate's entire view of the world. It carries the tool's id, the operation
    it declared, the capability tier the registry assigned it, and the arguments
    the model produced — nothing else. No session, no message history, no
    transcript: a rule that cannot see conversation cannot be talked out of a
    verdict by anything the model writes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(description="Resolved tool id the model asked to run.")
    operation: str = Field(default="", description="Declared operation (get/set/execute).")
    capability: CapabilityTier | None = Field(
        default=None,
        description="Registry-assigned tier. None for tools that declare none.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Validated tool arguments. Heterogeneous by nature — narrowed here.",
    )

    def text_values(self) -> tuple[str, ...]:
        """Return every string reachable in the arguments, flattened.

        Path-shaped rules need to know which files a call would touch, and a
        tool is free to carry that in any argument under any name — ``path``,
        ``file_path``, ``cwd``, or embedded in a shell ``command``. Walking the
        values rather than naming the keys is what keeps a rule correct for a
        tool this module has never heard of, including an MCP tool added at
        runtime.
        """
        found: list[str] = []
        self._collect_strings(self.arguments, found)
        return tuple(found)

    @classmethod
    def _collect_strings(cls, value: object, into: list[str]) -> None:
        """Depth-first walk appending every string found in ``value``."""
        if isinstance(value, str):
            into.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                cls._collect_strings(item, into)
        elif isinstance(value, (list, tuple)):
            for item in value:
                cls._collect_strings(item, into)


class TurnObservation(BaseModel):
    """Session progress at a turn boundary — scalars only.

    Deliberately not an agent-tree snapshot. Nothing textual is observed, so
    there is nothing to redact, truncate or depth-limit, and the struct costs
    the same to build on turn one as on turn two hundred.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int = Field(ge=0, description="Turns completed so far in this run.")
    elapsed_seconds: float = Field(ge=0.0, description="Wall seconds since the run started.")
    tool_calls: int = Field(default=0, ge=0, description="Cumulative tool calls executed.")
    write_calls: int = Field(
        default=0, ge=0, description="Cumulative write-tier tool calls executed."
    )


class SafetyVerdict(BaseModel):
    """A rule's decision about one observation.

    Absence is not a verdict: a rule that does not match returns ``None`` rather
    than an ``allow``, so "no rule had an opinion" and "a rule deliberately
    permitted this" stay distinguishable — the same reason a session-end hook's
    optional return is not a boolean.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Decision
    rule: str = Field(description="Name of the rule that decided.")
    reason: str = Field(default="", description="Operator-facing explanation.")

    @property
    def blocks(self) -> bool:
        """Return True when this verdict stops the thing it judged."""
        return self.decision == "deny"


class SafetyRule(BaseModel):
    """Base for every rule kind — never instantiated directly.

    ``kind`` is an untyped ``str`` here and a ``Literal`` on each member, so a
    bare ``SafetyRule`` cannot validate and every concrete rule is reached
    through the discriminated union below. Each member owns its own validators
    and overrides exactly the hook it can answer; the two hooks default to
    ``None`` so there is no ``if kind ==`` anywhere in the plane.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    _ADAPTER: ClassVar[TypeAdapter | None] = None

    #: Which observation points this kind answers. Declared rather than derived
    #: — asking at runtime whether a subclass overrode a hook means introspecting
    #: the MRO, which reads as working right up until someone adds a base class.
    OBSERVES: ClassVar[tuple[Literal["tool_call", "turn"], ...]] = ()

    kind: str
    name: str = Field(description="Stable id for this rule, reported in every verdict.")
    decision: Decision = Field(
        default="deny", description="What happens when this rule matches."
    )
    reason: str = Field(default="", description="Operator-facing explanation shown on a match.")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        """Reject a name that is not a lowercase-hyphen id."""
        if not _NAME_RE.match(value):
            raise ValueError(
                f"rule name {value!r} must be lowercase letters, digits and "
                "single hyphens (same rule as skills and agent definitions)"
            )
        return value

    def on_tool_call(self, call: ToolCallObservation) -> SafetyVerdict | None:
        """Judge a resolved tool call. ``None`` means this rule does not apply."""
        return None

    def on_turn(self, turn: TurnObservation) -> SafetyVerdict | None:
        """Judge session progress at a turn boundary. ``None`` means no opinion."""
        return None

    def inspects(self) -> str:
        """Return one line naming what this rule looks at, for disclosure.

        Every rule must be able to describe itself to the user BEFORE it runs —
        that is the disclosure contract, and putting the sentence on the rule is
        what stops it drifting from what the rule actually does.
        """
        raise NotImplementedError

    def _verdict(self) -> SafetyVerdict:
        """Build this rule's verdict from its own configured decision."""
        return SafetyVerdict(decision=self.decision, rule=self.name, reason=self.reason)

    @classmethod
    def parse(cls, value: object) -> SafetyRuleUnion:
        """Validate an untrusted mapping into one concrete rule kind."""
        if cls._ADAPTER is None:
            cls._ADAPTER = TypeAdapter(SafetyRuleUnion)
        parsed: SafetyRuleUnion = cls._ADAPTER.validate_python(value)
        return parsed


class ToolMatchRule(SafetyRule):
    """Match a tool call by id or by capability tier.

    ``tools`` entries are full-match regexes, which makes an exact id and a
    whole MCP surface (``mcp__payments__.*``) the same expression rather than
    two features. An entry that does not compile is rejected at definition, not
    at the first call it was supposed to guard.
    """

    OBSERVES: ClassVar[tuple[Literal["tool_call", "turn"], ...]] = ("tool_call",)

    kind: Literal["tool.match"] = "tool.match"
    tools: tuple[str, ...] = Field(
        default=(), description="Regex patterns matched against the whole tool id."
    )
    tiers: tuple[CapabilityTier, ...] = Field(
        default=(), description="Capability tiers this rule applies to."
    )

    @field_validator("tools")
    @classmethod
    def _validate_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject a pattern that will not compile."""
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"tool pattern {pattern!r} is not a valid regex: {exc}") from exc
        return value

    def on_tool_call(self, call: ToolCallObservation) -> SafetyVerdict | None:
        """Return this rule's verdict when the call matches its id or tier set."""
        if self.tiers and call.capability in self.tiers:
            return self._verdict()
        for pattern in self.tools:
            if re.fullmatch(pattern, call.tool_id):
                return self._verdict()
        return None

    def inspects(self) -> str:
        """Describe the tool ids and tiers this rule watches."""
        parts: list[str] = []
        if self.tools:
            parts.append("tool ids matching " + ", ".join(self.tools))
        if self.tiers:
            parts.append(" or ".join(self.tiers) + "-tier tool calls")
        return "the id and capability tier of " + (" and ".join(parts) or "no tool")


class PathGuardRule(SafetyRule):
    """Match a tool call whose arguments name a guarded path.

    Compares against every string in the call's arguments rather than a named
    key, so it covers a file tool's ``path``, an edit tool's ``file_path`` and a
    shell tool's ``command`` alike, including tools this module has never seen.

    **Its reach over a shell command is a substring test, and that is a real
    limit.** A command deliberately obfuscating the path (string concatenation,
    a variable, a here-doc) is not caught. The rule is defence in depth against
    the direct attempt; it is not what makes the plane tamper-proof — see the
    package doctrine for the property that does.
    """

    OBSERVES: ClassVar[tuple[Literal["tool_call", "turn"], ...]] = ("tool_call",)

    kind: Literal["path.guard"] = "path.guard"
    paths: tuple[str, ...] = Field(
        default=(), description="Path fragments that may not be targeted."
    )
    tiers: tuple[CapabilityTier, ...] = Field(
        default=("write", "execute"),
        description="Tiers this guard applies to. A read never mutates, so reads are exempt.",
    )

    @field_validator("paths")
    @classmethod
    def _normalize_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Normalise separators so a guard written either way matches either way."""
        return tuple(fragment.replace("\\", "/").strip("/") for fragment in value if fragment)

    def on_tool_call(self, call: ToolCallObservation) -> SafetyVerdict | None:
        """Return this rule's verdict when a guarded fragment appears in the call."""
        if call.capability not in self.tiers:
            return None
        for text in call.text_values():
            haystack = text.replace("\\", "/")
            for fragment in self.paths:
                if fragment and fragment in haystack:
                    return self._verdict()
        return None

    def inspects(self) -> str:
        """Describe the paths and tiers this guard watches."""
        tiers = " and ".join(self.tiers) or "no"
        return f"{tiers}-tier tool arguments naming " + ", ".join(self.paths)


class SessionBudgetRule(SafetyRule):
    """Bound a run by steps, wall time, or tool calls.

    The observer half. Every bound is optional and the rule matches when ANY
    configured bound is crossed; a rule with no bound set never matches, which
    is why an empty budget is inert rather than an error.
    """

    OBSERVES: ClassVar[tuple[Literal["tool_call", "turn"], ...]] = ("turn",)

    kind: Literal["session.budget"] = "session.budget"
    max_steps: int | None = Field(default=None, ge=1, description="Turn ceiling.")
    max_seconds: float | None = Field(default=None, gt=0.0, description="Wall-clock ceiling.")
    max_tool_calls: int | None = Field(default=None, ge=1, description="Tool-call ceiling.")

    def on_turn(self, turn: TurnObservation) -> SafetyVerdict | None:
        """Return this rule's verdict when any configured ceiling is crossed."""
        crossed = (
            (self.max_steps is not None and turn.step >= self.max_steps)
            or (self.max_seconds is not None and turn.elapsed_seconds >= self.max_seconds)
            or (self.max_tool_calls is not None and turn.tool_calls >= self.max_tool_calls)
        )
        return self._verdict() if crossed else None

    def inspects(self) -> str:
        """Describe the ceilings this rule watches."""
        bounds: list[str] = []
        if self.max_steps is not None:
            bounds.append(f"{self.max_steps} turns")
        if self.max_seconds is not None:
            bounds.append(f"{self.max_seconds:g}s elapsed")
        if self.max_tool_calls is not None:
            bounds.append(f"{self.max_tool_calls} tool calls")
        return "session progress against " + (", ".join(bounds) or "no ceiling")


SafetyRuleUnion = Annotated[
    ToolMatchRule | PathGuardRule | SessionBudgetRule,
    Field(discriminator="kind"),
]

parse_rule = SafetyRule.parse


class SafetyDocument(BaseModel):
    """One parsed safety file: front matter plus its prose body.

    The body is documentation. It reaches the human reading the file and the
    disclosure a user is shown; it reaches no prompt, which is what makes a long
    policy free.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="Document id, defaulted from the filename.")
    description: str = Field(default="", description="One line shown in the user's disclosure.")
    enabled: bool = Field(default=True, description="Toggle the document without deleting it.")
    rules: tuple[SafetyRuleUnion, ...] = Field(default=(), description="Ordered rules.")
    body: str = Field(default="", description="Prose rationale. Never sent to a model.")
    source_path: str = Field(default="", description="File this document was parsed from.")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        """Reject a document name that is not a lowercase-hyphen id."""
        if not _NAME_RE.match(value):
            raise ValueError(f"document name {value!r} must be a lowercase-hyphen id")
        return value

    @classmethod
    def parse(cls, raw: str, *, name: str, source_path: str = "") -> SafetyDocument:
        """Parse a markdown file's text into a document.

        Raises ``ValueError`` on anything malformed. A safety document is a
        trust boundary and its front matter is `extra="forbid"`, so a typo'd key
        is a load error the operator sees rather than a rule that silently never
        fires — which is the failure mode that makes a guardrail worthless.
        """
        match = _FRONTMATTER_RE.match(raw)
        if match is None:
            raise ValueError(f"{source_path or name}: no YAML front matter block found")
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise ValueError(
                f"{source_path or name}: front matter is not valid YAML: {exc}"
            ) from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"{source_path or name}: front matter must be a mapping")

        payload = dict(loaded)
        payload.setdefault("name", name)
        payload["body"] = raw[match.end() :]
        payload["source_path"] = source_path
        return cls.model_validate(payload)


__all__ = [
    "MONITOR_SUBDIR",
    "POLICY_SUBDIR",
    "SAFETY_DIRNAME",
    "CapabilityTier",
    "Decision",
    "PathGuardRule",
    "SafetyDocument",
    "SafetyRule",
    "SafetyRuleUnion",
    "SafetyVerdict",
    "SessionBudgetRule",
    "ToolCallObservation",
    "ToolMatchRule",
    "TurnObservation",
    "parse_rule",
]
