#!/usr/bin/env python3
"""``SafetyPlane`` — the operator-owned gate and observer, assembled once.

One atomic class holding the plane's whole state: the documents parsed off
disk, the ordered rule list, and the disclosure a user is shown. Collaborators
(the clock, the observation) arrive as arguments; nothing here reaches for the
world on its own after ``load``.

The plane is **not a tool.** It is never bound to a model, appears in no
catalog, and no agent can name it. That is the first of four legs holding up
the property that a root agent cannot switch off its own guardrail — the others
are in this module's doctrine file.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mewbo_core.common import get_logger
from mewbo_core.safety.spec import (
    MONITOR_SUBDIR,
    POLICY_SUBDIR,
    SAFETY_DIRNAME,
    PathGuardRule,
    SafetyDocument,
    SafetyRuleUnion,
    SafetyVerdict,
    ToolCallObservation,
    TurnObservation,
)

logger = get_logger(name="core.safety")

# The plane's own files, relative to a project root. Written once here and read
# by both the loader and the built-in guard so the guarded path and the read
# path can never drift apart.
_POLICY_DIR = f"{SAFETY_DIRNAME}/{POLICY_SUBDIR}"
_MONITOR_DIR = f"{SAFETY_DIRNAME}/{MONITOR_SUBDIR}"

# Constructed in code and prepended to every rule list, so no document can omit
# it and — because the first matching rule wins — no document rule can run
# ahead of it and permit what it denies.
SELF_PROTECTION_RULE = PathGuardRule(
    name="safety-plane-integrity",
    decision="deny",
    reason=(
        "This path holds the session's own safety plane. An agent that can "
        "rewrite its guardrail has no guardrail."
    ),
    paths=(_POLICY_DIR, _MONITOR_DIR),
    tiers=("write", "execute"),
)


class RuleDisclosure(BaseModel):
    """One rule, described to the user before anything runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: str
    decision: str
    inspects: str = Field(description="What this rule looks at, in one line.")


class SafetyDisclosure(BaseModel):
    """What the user is told about the plane, before it evaluates anything.

    Emitted at session start and served by the REST read endpoint. Silent
    inspection of a session is the thing this feature must not be, so this is a
    wire contract rather than a log line: every client renders the same object.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    active: bool = Field(description="Whether any rule will be evaluated this session.")
    documents: tuple[str, ...] = Field(default=(), description="Source files in effect.")
    rules: tuple[RuleDisclosure, ...] = Field(default=(), description="Every active rule.")
    inspects_tool_calls: bool = Field(
        default=False, description="Whether a tool call is judged before it runs."
    )
    inspects_turns: bool = Field(
        default=False, description="Whether session progress is judged between turns."
    )


class SafetyPlane:
    """The assembled safety plane for one session.

    Built once at orchestrator construction and forwarded BY VALUE to every
    child loop. Its rules are frozen models, so an agent that edits or deletes
    a document mid-session changes nothing about the session judging it — which
    is the leg that actually carries the tamper-proof requirement, rather than
    the file-level guard, which a determined shell command can obfuscate past.
    """

    def __init__(self, documents: tuple[SafetyDocument, ...]) -> None:
        """Assemble the ordered rule list from already-parsed documents."""
        self._documents = documents
        rules: list[SafetyRuleUnion] = [SELF_PROTECTION_RULE]
        for document in documents:
            if document.enabled:
                rules.extend(document.rules)
        self._rules: tuple[SafetyRuleUnion, ...] = tuple(rules)

    @classmethod
    def load(cls, cwd: str | None, *, enabled: bool) -> SafetyPlane | None:
        """Return a plane for ``cwd``, or ``None`` when the plane is off.

        ``None`` is the inert state and it is total: a disabled deployment does
        not stat a directory, does not read a file, does not build a rule and
        does not emit an event. Every call site holds ``plane is not None`` as
        its only cost, so a default install pays one attribute check per tool
        call and nothing else.
        """
        if not enabled or not cwd:
            return None
        root = Path(cwd)
        documents: list[SafetyDocument] = []
        for subdir in (_POLICY_DIR, _MONITOR_DIR):
            documents.extend(cls._load_dir(root / subdir))
        return cls(tuple(documents))

    @classmethod
    def _load_dir(cls, directory: Path) -> list[SafetyDocument]:
        """Parse every ``*.md`` in ``directory``, sorted by filename.

        A file that fails to parse is skipped with a logged error rather than
        raising: one malformed document must not make a session unstartable,
        and the remaining rules still apply. The error is loud precisely because
        the failure mode it guards against — a rule that silently never fires —
        is indistinguishable from safety.
        """
        documents: list[SafetyDocument] = []
        try:
            candidates = sorted(directory.glob("*.md"))
        except OSError:
            return documents
        for path in candidates:
            try:
                raw = path.read_text(encoding="utf-8")
                documents.append(
                    SafetyDocument.parse(raw, name=path.stem, source_path=str(path))
                )
            except (OSError, ValueError) as exc:
                logger.error("safety document {} could not be loaded: {}", path, exc)
        return documents

    @property
    def rules(self) -> tuple[SafetyRuleUnion, ...]:
        """Return every active rule, self-protection first."""
        return self._rules

    def evaluate_tool_call(self, call: ToolCallObservation) -> SafetyVerdict | None:
        """Judge a resolved tool call. ``None`` means no rule had an opinion.

        First match wins, in rule order — which is what makes an allow-exception
        expressible at all, and why the built-in guard is prepended rather than
        merely present.
        """
        for rule in self._rules:
            verdict = rule.on_tool_call(call)
            if verdict is not None:
                return verdict
        return None

    def evaluate_turn(self, turn: TurnObservation) -> SafetyVerdict | None:
        """Judge session progress at a turn boundary."""
        for rule in self._rules:
            verdict = rule.on_turn(turn)
            if verdict is not None:
                return verdict
        return None

    def disclosure(self) -> SafetyDisclosure:
        """Describe the plane to the user, before it evaluates anything."""
        rules = tuple(
            RuleDisclosure(
                name=rule.name,
                kind=rule.kind,
                decision=rule.decision,
                inspects=rule.inspects(),
            )
            for rule in self._rules
        )
        return SafetyDisclosure(
            active=True,
            documents=tuple(d.source_path for d in self._documents if d.enabled),
            rules=rules,
            inspects_tool_calls=any("tool_call" in r.OBSERVES for r in self._rules),
            inspects_turns=any("turn" in r.OBSERVES for r in self._rules),
        )


__all__ = [
    "SELF_PROTECTION_RULE",
    "RuleDisclosure",
    "SafetyDisclosure",
    "SafetyPlane",
]
