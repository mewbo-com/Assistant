#!/usr/bin/env python3
"""How a built-in tool's argument schema is DECLARED, and how it renders.

One concern: turning a readable declaration into the JSON Schema a provider
binds. It exists because a tool's schema is consumed in two places that are
built at different times — the ``ToolSpec`` the registry registers, and the
cached manifest ``_ensure_auto_manifest`` writes before those specs exist — so a
schema written as a literal has to be written TWICE and is then free to drift.
Declaring it once as a validated object and rendering it at both sites removes
the second copy without inverting that build order.

The validation earns its place rather than decorating: a ``required`` naming a
property that does not exist is a schema no caller can ever satisfy, and nothing
checked for it before. That is a real defect class, caught here at definition.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mewbo_core.contracts.types import JsonValue

JsonType = Literal["string", "integer", "number", "boolean", "array", "object"]


class ToolParameter(BaseModel):
    """One argument a tool accepts: its type, its prose, and its bounds.

    ``description`` is required rather than optional because it is the only
    thing a caller reads to decide what to pass — an undescribed parameter is a
    parameter that gets guessed at.

    ``default``/``minimum``/``maximum`` exist so a constraint is stated where it
    can be CHECKED, not only in prose. "max 30000" buried in a description is
    advice a caller has to notice and honour; ``maximum: 30000`` is part of the
    contract the provider validates against, and it survives the description
    being rewritten. State a bound in both places — the prose says why, the
    field says what.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: JsonType
    description: str = Field(min_length=1)
    enum: tuple[str, ...] = ()
    default: str | int | float | bool | None = None
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def _bounds_suit_the_type(self) -> ToolParameter:
        """Refuse a numeric bound on a parameter that holds no number."""
        has_bound = self.minimum is not None or self.maximum is not None
        if has_bound and self.type not in ("integer", "number"):
            raise ValueError(f"minimum/maximum are meaningless on a {self.type!r} parameter")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(f"minimum {self.minimum} exceeds maximum {self.maximum}")
        if self.enum and self.type != "string":
            raise ValueError(f"enum is only meaningful on a string parameter, not {self.type!r}")
        return self

    def as_json_schema(self) -> dict[str, JsonValue]:
        """Render this parameter as a JSON Schema property.

        A ``None`` default is indistinguishable from "no default declared" and
        is rendered as the latter — a tool whose default really is null says so
        in its description rather than through an absent field.
        """
        rendered: dict[str, JsonValue] = {
            "type": self.type,
            "description": self.description,
        }
        if self.enum:
            rendered["enum"] = list(self.enum)
        if self.default is not None:
            rendered["default"] = self.default
        if self.minimum is not None:
            rendered["minimum"] = self.minimum
        if self.maximum is not None:
            rendered["maximum"] = self.maximum
        return rendered


class ToolSchema(BaseModel):
    """The full argument contract for one tool.

    Rendered — never stored — at both consumption sites, so the two can restate
    the same declaration without being able to disagree about it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    properties: dict[str, ToolParameter]
    required: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _required_names_exist(self) -> ToolSchema:
        """Refuse a ``required`` argument the schema never declares."""
        missing = [name for name in self.required if name not in self.properties]
        if missing:
            raise ValueError(
                f"required names {missing} are not declared properties; "
                f"declared: {sorted(self.properties)}"
            )
        return self

    def as_json_schema(self) -> dict[str, JsonValue]:
        """Render the whole contract as the JSON Schema a provider binds."""
        return {
            "type": "object",
            "properties": {
                name: parameter.as_json_schema() for name, parameter in self.properties.items()
            },
            "required": list(self.required),
        }


__all__ = ["JsonType", "ToolParameter", "ToolSchema"]
