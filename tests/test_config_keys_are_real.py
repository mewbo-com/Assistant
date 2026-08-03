#!/usr/bin/env python3
"""Tripwire: every ``get_config_value`` key must name a real typed field.

``get_config_value`` walks one ``getattr`` per key and returns the caller's
default the moment a hop misses. That is a silent read: a key whose typed field
does not exist returns the default in EVERY deployment, raising nothing and
logging nothing, so the feature behind it is simply unreachable while the call
site reads as correct. It shipped that way once already — a status meter read
``llm.model_context_windows`` and ``llm.default_context_window``, both of which
live under ``token_budget``, so it reported a constant window for every model
for as long as it existed.

This test derives the guard set instead of maintaining one: it finds the call
sites and resolves each key path against the real ``AppConfig``. A renamed or
moved section fails here rather than in production.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from mewbo_core.config import AppConfig
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_ROOTS = (REPO_ROOT / "packages", REPO_ROOT / "apps")

# Keys reached through a value that is a free-form mapping rather than a typed
# submodel. The walk succeeds at runtime because the hop lands on a ``dict``;
# there is no field to resolve, so a static check cannot follow it.
DYNAMIC_PARENTS = frozenset({"projects", "channels", "mcp_servers", "tokens"})

# Reads that resolve to NO field today. Each one silently returns its caller's
# default in every deployment, so the behaviour behind it is unreachable — these
# are defects held under a tripwire, not exemptions.
#
# EXACT set, deliberately, so it is loud in BOTH directions: a new unreachable
# key fails, and so does a listed one being fixed without being removed here.
# Adding a row is a decision to ship a dead read; prefer declaring the field.
KNOWN_UNREACHABLE_KEYS: frozenset[tuple[str, ...]] = frozenset(
    {
        # ``agent.retry`` declares no ``poll_tools``.
        ("agent", "retry", "poll_tools"),
        # ``scg`` declares only ``enabled`` and ``traversal`` — there is no
        # ``entity_resolution`` submodel, so both thresholds are inert.
        ("scg", "entity_resolution", "confident_threshold"),
        ("scg", "entity_resolution", "band_low"),
    }
)


class ConfigKeyUse(BaseModel):
    """One literal ``get_config_value(...)`` call found in the tree."""

    path: str
    line: int
    keys: tuple[str, ...]

    def resolves(self) -> bool:
        """True when every key hop names a declared field on the config tree.

        Walks the MODEL, never an instance: a field that is set to ``None`` in a
        given deployment still exists, and this is a question about the schema.
        """
        model: type[BaseModel] | None = AppConfig
        for key in self.keys:
            if model is None:
                return True  # left the typed tree at a free-form mapping
            field = model.model_fields.get(key)
            if field is None:
                return False
            annotation = field.annotation
            nested = isinstance(annotation, type) and issubclass_safe(annotation)
            model = annotation if nested else None
            if key in DYNAMIC_PARENTS:
                return True
        return True


def issubclass_safe(annotation: type) -> bool:
    """True when *annotation* is a nested config model worth descending into."""
    try:
        return issubclass(annotation, BaseModel)
    except TypeError:  # pragma: no cover - non-class annotations
        return False


def _collect() -> list[ConfigKeyUse]:
    """Every ``get_config_value`` call whose keys are all string literals.

    A call built from variables is skipped rather than guessed at — this test
    convicts only on evidence it can actually read.
    """
    uses: list[ConfigKeyUse] = []
    for root in SEARCH_ROOTS:
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name != "get_config_value" or not node.args:
                    continue
                if not all(
                    isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args
                ):
                    continue
                uses.append(
                    ConfigKeyUse(
                        path=str(path.relative_to(REPO_ROOT)),
                        line=node.lineno,
                        keys=tuple(a.value for a in node.args),  # type: ignore[attr-defined]
                    )
                )
    return uses


def test_call_sites_were_found():
    """Guard the guard: an empty scan would make the assertion below vacuous."""
    assert len(_collect()) > 20


@pytest.mark.parametrize("use", _collect(), ids=lambda u: f"{u.path}:{u.line}:{'.'.join(u.keys)}")
def test_config_key_names_a_real_field(use: ConfigKeyUse):
    """A key path that resolves to no field reads its default forever."""
    if use.keys in KNOWN_UNREACHABLE_KEYS:
        pytest.xfail(f"known-unreachable: {'.'.join(use.keys)}")
    assert use.resolves(), (
        f"{use.path}:{use.line} reads '{'.'.join(use.keys)}', which names no field on "
        f"AppConfig. get_config_value returns the default silently, so the value is "
        f"unreachable in every deployment."
    )


def test_no_known_unreachable_key_was_quietly_fixed():
    """The exact set stays loud in both directions.

    A listed key that now resolves has been fixed, and leaving it listed would
    let the NEXT regression on that key pass unnoticed.
    """
    still_broken = {u.keys for u in _collect() if not u.resolves()}
    healed = KNOWN_UNREACHABLE_KEYS - still_broken
    assert not healed, (
        f"these keys now resolve and must be removed from KNOWN_UNREACHABLE_KEYS: "
        f"{sorted('.'.join(k) for k in healed)}"
    )
