#!/usr/bin/env python3
"""Generative-UI vocabulary — a data-owned discriminated union of UI nodes.

There are TWO shapes here and the difference is the whole design.

*Model-facing* is what the LLM fills in and what the tool's JSON Schema
describes: a discriminated union on ``component`` whose members carry their own
*typed, flat* fields. A precise per-variant schema is far easier for a model to
fill correctly than a generic ``props`` bag, and it is what lets every variant
own its own validators.

*Renderer-facing* is what crosses the wire: the generic node shape
``{component, props, children, key}`` that the vendored assistant-ui renderer
consumes. We do not get to choose it.

:meth:`GenerativeUINode.to_spec_node` is the ONE conversion between them, and it
is a single method on the BASE rather than eleven overrides: a variant's typed
fields simply ARE its props, so the conversion is
``model_dump(exclude=<structural fields>)`` and cannot drift when a variant
gains a field. The per-variant strategy method is :meth:`GenerativeUINode.to_text`
— the plain-text degradation every non-visual surface (CLI, mobile, Home
Assistant, MCP) renders instead of the tree, so none of them ever has to learn
the component vocabulary.

Shaped after :mod:`mewbo_core.triggers.spec`: one ``Field(discriminator=...)``
parse seam, per-variant validators and strategy methods on the members, and
deliberately ZERO service-side ``if component ==`` dispatch anywhere — that
drifts out of sync the moment a variant gains a field. Models import no I/O.
"""

from __future__ import annotations

import json
from typing import Annotated, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------
#
# Every string here is authored by a model and then persisted, broadcast to
# every client, and replayed on every history read — the same exposure that
# made ``run_error.py`` bound its diagnostics. So each cap is sized to the ROLE
# the string plays in the rendered surface rather than to one global number: a
# heading that needs two thousand characters is prose in the wrong component,
# and a cap that says so is a correction the model can act on.

# One-line labels: headings, badges, card titles, table headers, link text. Two
# hundred characters is already several times a legible label at any font size.
LABEL_MAX_CHARS = 200
# Prose bodies: a paragraph or an alert body. Longer than this wants several
# Text nodes, which is also what reads better.
PROSE_MAX_CHARS = 2_000
# Tabular / definition-list values. Short by nature — a cell that needs a
# paragraph is a Card, not a row.
VALUE_MAX_CHARS = 500
# Source listings. The one field with a genuinely large legitimate size, and
# the reason the aggregate cap below exists.
CODE_MAX_CHARS = 20_000
# Practical URL ceiling — the shortest limit among mainstream browsers.
HREF_MAX_CHARS = 2_000
# A syntax-highlighter language tag ("python", "objective-c", "c++").
LANGUAGE_MAX_CHARS = 32

# Tree limits. A model that blows one gets a validation error it can correct,
# never a silently truncated render.
MAX_TREE_DEPTH = 8
MAX_TREE_NODES = 200

# Aggregate ceiling on the serialized renderer-facing tree. The per-field caps
# alone do not bound the whole: two hundred maximal CodeBlocks would be four
# megabytes riding into every client and into the session transcript. Two
# hundred thousand characters still admits ten full-size code listings, which
# is far beyond any panel a person reads.
MAX_SPEC_CHARS = 200_000

# Only these schemes may appear in a ``Link``. This is a security boundary, not
# hygiene: props are spread directly onto the rendered component, so a
# ``javascript:`` href is script execution and a ``data:`` href is a phishing
# surface. ``urlsplit`` lowercases the scheme and strips the ASCII tab/newline
# characters browsers ignore, so ``JavaScript:`` and ``java\tscript:`` both
# normalize into this check rather than around it.
ALLOWED_LINK_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

# Constrained string aliases. The cap AND the non-empty rule live in the TYPE,
# declared once and reused across the variants, so a new field inherits both by
# naming its role. ``strip_whitespace`` runs before the length checks, which is
# what makes ``min_length=1`` mean "non-empty after strip".
Label = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LABEL_MAX_CHARS)
]
Prose = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=PROSE_MAX_CHARS)
]
# A blank table cell / definition value is legitimate data, so no minimum here.
Value = Annotated[str, StringConstraints(strip_whitespace=True, max_length=VALUE_MAX_CHARS)]
# Code is never stripped — leading indentation is the content.
Code = Annotated[str, StringConstraints(min_length=1, max_length=CODE_MAX_CHARS)]
Href = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=HREF_MAX_CHARS)
]
# Constrained to a plain tag so it cannot break out of the fence in ``to_text``
# or arrive as an unexpected shape on a highlighter's class name.
Language = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=LANGUAGE_MAX_CHARS,
        pattern=r"^[A-Za-z0-9_+#.-]+$",
    ),
]


class GenerativeUINode(BaseModel):
    """Shared structure + conversion for every component variant.

    Not itself a union member — concrete variants declare their own
    ``component`` Literal (see :data:`GenerativeUINodeUnion`). Instantiating
    this directly is meaningless by design: it holds the contract, not a
    renderable component.

    Subclasses override :meth:`to_text`; only the two container variants
    override :meth:`child_nodes`. Nothing else is per-variant, which is the
    point — :meth:`to_spec_node` and :meth:`measure` are written once and
    cannot fall out of step with a variant that grows a field.
    """

    model_config = ConfigDict(extra="forbid")

    # Structural fields, excluded from the emitted ``props`` because the
    # renderer reads them off the node itself. A plain ``set`` rather than a
    # frozenset because that is what pydantic's ``exclude`` accepts.
    _STRUCTURAL_FIELDS: ClassVar[set[str]] = {"component", "children"}

    component: str

    def child_nodes(self) -> tuple[GenerativeUINode, ...]:
        """Return the direct children of this node.

        Empty for every leaf variant. Leaves do not merely *return* nothing
        here — they have no ``children`` field at all, so ``extra="forbid"``
        rejects a leaf handed children long before this method is reached.
        """
        return ()

    def to_text(self) -> str:
        """Render this node as plain text.

        Every non-visual surface degrades to this, so the renderings are
        deliberately boring and stable: they are a wire contract with the CLI,
        the mobile client and Home Assistant as much as the node shape is one
        with the console.
        """
        raise NotImplementedError  # pragma: no cover - abstract on the base

    def to_spec_node(self) -> dict[str, object]:
        """Convert this typed variant into the renderer-facing node shape.

        The ONE model-facing → renderer-facing conversion. A variant's typed
        fields are its props by construction, so there is nothing per-variant
        to keep in sync and no ``if component ==`` anywhere. ``mode="json"``
        guarantees the result is JSON-native before it reaches the wire.

        The renderer's node shape also carries an optional ``key`` for React
        reconciliation, which is deliberately never emitted: no v1 component
        holds state (there is no input, no toggle, no chart), so re-mounting a
        replaced tree is invisible, while exposing the field would have put a
        reconciliation hint the model cannot use well into eleven variant
        schemas — about a sixth of the bytes bound on every LLM call. Add it
        with the first stateful component, not before.
        """
        node: dict[str, object] = {
            "component": self.component,
            "props": self.model_dump(exclude=self._STRUCTURAL_FIELDS, mode="json"),
        }
        children = self.child_nodes()
        if children:
            node["children"] = [child.to_spec_node() for child in children]
        return node

    def measure(self) -> tuple[int, int]:
        """Return ``(depth, node_count)`` for the subtree rooted at this node."""
        depth = 1
        count = 1
        for child in self.child_nodes():
            child_depth, child_count = child.measure()
            depth = max(depth, 1 + child_depth)
            count += child_count
        return depth, count


class _ContainerNode(GenerativeUINode):
    """Base for the two variants that own children.

    ``children`` lives here rather than on :class:`GenerativeUINode` so that a
    leaf's schema has no such field: a leaf carrying children is then rejected
    by ``extra="forbid"`` structurally, with no validator to write and none to
    forget on the twelfth variant.
    """

    children: list[GenerativeUINodeUnion] = Field(
        default_factory=list,
        max_length=MAX_TREE_NODES,
        description="Nodes rendered inside this container.",
    )

    def child_nodes(self) -> tuple[GenerativeUINode, ...]:
        """Return the contained nodes."""
        return tuple(self.children)


class TextNode(GenerativeUINode):
    """A paragraph of plain prose."""

    component: Literal["Text"] = "Text"
    value: Prose = Field(description="The paragraph text.")
    tone: Literal["default", "muted"] = Field(
        default="default",
        description="'muted' de-emphasises the paragraph as secondary detail.",
    )

    def to_text(self) -> str:
        """Return the paragraph verbatim."""
        return self.value


class HeadingNode(GenerativeUINode):
    """A section heading."""

    component: Literal["Heading"] = "Heading"
    value: Label = Field(description="The heading text.")
    level: Literal[1, 2, 3] = Field(
        default=2,
        description="Heading rank within the panel; 1 is the most prominent.",
    )

    def to_text(self) -> str:
        """Return the heading as a markdown heading one rank below the page title."""
        # The panel is nested inside a conversation, so its most prominent
        # heading renders as an ``h2`` — the markdown marker count mirrors that
        # rather than starting at ``#``.
        return f"{'#' * (self.level + 1)} {self.value}"


class CardNode(_ContainerNode):
    """A titled surface grouping related nodes."""

    component: Literal["Card"] = "Card"
    title: Label | None = Field(default=None, description="Optional card heading.")

    def to_text(self) -> str:
        """Return the title followed by the children, indented one level."""
        lines: list[str] = []
        if self.title:
            lines.append(self.title)
        for child in self.children:
            lines.extend(f"  {line}".rstrip() for line in child.to_text().split("\n"))
        return "\n".join(lines)


class StackNode(_ContainerNode):
    """A flex row or column of nodes."""

    component: Literal["Stack"] = "Stack"
    direction: Literal["vertical", "horizontal"] = Field(
        default="vertical", description="Axis the children are laid out along."
    )
    gap: Literal["sm", "md", "lg"] = Field(
        default="md", description="Spacing between children."
    )

    def to_text(self) -> str:
        """Return the children one per line — layout carries no meaning in text."""
        return "\n".join(child.to_text() for child in self.children)


class BadgeNode(GenerativeUINode):
    """A short status pill."""

    component: Literal["Badge"] = "Badge"
    label: Label = Field(description="The pill text (1-3 words).")
    status: Literal["neutral", "success", "warning", "info", "danger"] = Field(
        default="neutral", description="Semantic colour of the pill."
    )

    def to_text(self) -> str:
        """Return the label in brackets."""
        return f"[{self.label}]"


class KeyValueItem(BaseModel):
    """One ``label: value`` row of a :class:`KeyValueNode`."""

    model_config = ConfigDict(extra="forbid")

    label: Label = Field(description="The field name.")
    value: Value = Field(description="The field value.")


class KeyValueNode(GenerativeUINode):
    """A definition list of short label/value pairs."""

    component: Literal["KeyValue"] = "KeyValue"
    items: list[KeyValueItem] = Field(
        min_length=1, max_length=20, description="The rows, in display order."
    )

    def to_text(self) -> str:
        """Return one ``label: value`` line per row."""
        return "\n".join(f"{item.label}: {item.value}" for item in self.items)


class TableNode(GenerativeUINode):
    """A small tabular dataset."""

    component: Literal["Table"] = "Table"
    columns: list[Label] = Field(
        min_length=1, max_length=8, description="Column headers, left to right."
    )
    rows: list[list[Value]] = Field(
        default_factory=list,
        max_length=50,
        description="Row cells, each row exactly as long as `columns`.",
    )

    @model_validator(mode="after")
    def _rows_match_columns(self) -> TableNode:
        """Reject a ragged row rather than padding it.

        A short row is a model that lost track of its own schema mid-generation,
        and silently padding it renders a confidently wrong table — the one
        failure mode a data component must not have.
        """
        width = len(self.columns)
        for index, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(
                    f"row {index} has {len(row)} cells, expected {width} "
                    f"(one per column: {self.columns})"
                )
        return self

    @staticmethod
    def _cell(text: str) -> str:
        """Escape a cell so it cannot break out of the markdown table."""
        return text.replace("\n", " ").replace("|", r"\|")

    def to_text(self) -> str:
        """Return the table in markdown."""
        header = f"| {' | '.join(self._cell(c) for c in self.columns)} |"
        divider = f"| {' | '.join('---' for _ in self.columns)} |"
        body = [f"| {' | '.join(self._cell(c) for c in row)} |" for row in self.rows]
        return "\n".join([header, divider, *body])


class CodeBlockNode(GenerativeUINode):
    """A fenced block of source code or structured output."""

    component: Literal["CodeBlock"] = "CodeBlock"
    code: Code = Field(description="The literal source, newlines and indentation intact.")
    language: Language | None = Field(
        default=None, description="Syntax-highlighting language tag, e.g. 'python'."
    )

    def to_text(self) -> str:
        """Return the code in a fenced markdown block."""
        return f"```{self.language or ''}\n{self.code}\n```"


class AlertNode(GenerativeUINode):
    """A callout drawing attention to one fact."""

    component: Literal["Alert"] = "Alert"
    body: Prose = Field(description="What the reader needs to know.")
    title: Label | None = Field(default=None, description="Optional callout heading.")
    variant: Literal["info", "success", "warning", "danger"] = Field(
        default="info", description="Semantic severity of the callout."
    )

    def to_text(self) -> str:
        """Return ``VARIANT: title — body``, dropping the title when absent."""
        head = f"{self.variant.upper()}:"
        if self.title:
            return f"{head} {self.title} — {self.body}"
        return f"{head} {self.body}"


class DividerNode(GenerativeUINode):
    """A horizontal rule between sections."""

    component: Literal["Divider"] = "Divider"

    def to_text(self) -> str:
        """Return a markdown rule."""
        return "---"


class LinkNode(GenerativeUINode):
    """A hyperlink."""

    component: Literal["Link"] = "Link"
    href: Href = Field(description="Absolute http, https or mailto URL.")
    label: Label = Field(description="The link text.")

    @field_validator("href")
    @classmethod
    def _safe_scheme(cls, value: str) -> str:
        """Admit only the schemes in :data:`ALLOWED_LINK_SCHEMES`.

        Rejects ``javascript:``/``data:``/``file:`` outright, and rejects a
        scheme-relative ``//host/path`` too — it parses with no scheme at all
        and would inherit the console's, which is precisely the trick this
        allowlist exists to refuse.
        """
        parts = urlsplit(value)
        if parts.scheme not in ALLOWED_LINK_SCHEMES:
            raise ValueError(
                f"href scheme {parts.scheme or '(none)'!r} is not allowed; "
                f"use one of {sorted(ALLOWED_LINK_SCHEMES)}"
            )
        # A scheme with nothing behind it ("https://", "mailto:") renders as a
        # dead link, so require the part each scheme actually addresses.
        target = parts.path if parts.scheme == "mailto" else parts.netloc
        if not target:
            raise ValueError(f"href {value!r} names a scheme but no destination")
        return value

    def to_text(self) -> str:
        """Return ``label (href)`` — the destination survives the degradation."""
        return f"{self.label} ({self.href})"


# The ONE parse seam. Every consumer resolves a node through this alias, so a
# twelfth variant is added in exactly one place.
GenerativeUINodeUnion = Annotated[
    TextNode
    | HeadingNode
    | CardNode
    | StackNode
    | BadgeNode
    | KeyValueNode
    | TableNode
    | CodeBlockNode
    | AlertNode
    | DividerNode
    | LinkNode,
    Field(discriminator="component"),
]

# The containers reference the union above by forward reference; resolve it now
# that the alias exists.
_ContainerNode.model_rebuild()
CardNode.model_rebuild()
StackNode.model_rebuild()


class GenerativeUISpec(BaseModel):
    """A complete UI tree — the ``root`` array the renderer consumes."""

    model_config = ConfigDict(extra="forbid")

    root: list[GenerativeUINodeUnion] = Field(
        min_length=1,
        max_length=MAX_TREE_NODES,
        description="Top-level nodes, rendered in order.",
    )

    def measure(self) -> tuple[int, int]:
        """Return ``(depth, node_count)`` across every root subtree."""
        depth = 0
        count = 0
        for node in self.root:
            node_depth, node_count = node.measure()
            depth = max(depth, node_depth)
            count += node_count
        return depth, count

    def to_wire(self) -> dict[str, object]:
        """Return the frozen renderer-facing ``{"root": [...]}`` shape."""
        return {"root": [node.to_spec_node() for node in self.root]}

    def to_text(self) -> str:
        """Return the plain-text degradation of the whole tree."""
        return "\n".join(node.to_text() for node in self.root)

    @model_validator(mode="after")
    def _within_tree_limits(self) -> GenerativeUISpec:
        """Enforce the depth, node-count and serialized-size ceilings.

        Tree-wide limits can only be checked once the whole tree exists, which
        is why they live here rather than on a node. Each raises rather than
        truncating: a model that overran a budget can correct it, and a
        half-rendered panel is worse than an error it never sees.
        """
        depth, count = self.measure()
        if depth > MAX_TREE_DEPTH:
            raise ValueError(f"tree is {depth} levels deep, limit is {MAX_TREE_DEPTH}")
        if count > MAX_TREE_NODES:
            raise ValueError(f"tree holds {count} nodes, limit is {MAX_TREE_NODES}")
        size = len(json.dumps(self.to_wire(), ensure_ascii=False))
        if size > MAX_SPEC_CHARS:
            raise ValueError(
                f"tree serializes to {size} characters, limit is {MAX_SPEC_CHARS}; "
                "show less at once or summarise the largest node"
            )
        return self


__all__ = [
    "ALLOWED_LINK_SCHEMES",
    "CODE_MAX_CHARS",
    "HREF_MAX_CHARS",
    "LABEL_MAX_CHARS",
    "LANGUAGE_MAX_CHARS",
    "MAX_SPEC_CHARS",
    "MAX_TREE_DEPTH",
    "MAX_TREE_NODES",
    "PROSE_MAX_CHARS",
    "VALUE_MAX_CHARS",
    "AlertNode",
    "BadgeNode",
    "CardNode",
    "CodeBlockNode",
    "DividerNode",
    "GenerativeUINode",
    "GenerativeUINodeUnion",
    "GenerativeUISpec",
    "HeadingNode",
    "KeyValueItem",
    "KeyValueNode",
    "LinkNode",
    "StackNode",
    "TableNode",
    "TextNode",
]
