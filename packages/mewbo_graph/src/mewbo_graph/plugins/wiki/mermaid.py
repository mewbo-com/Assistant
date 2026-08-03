"""Mermaid validity rules for generated wiki pages.

A generated page carries hand-written Mermaid, and a diagram that fails to parse
renders as an error card in the console — the page ships broken and nothing in
the pipeline notices. This module is the gate that notices.

**What this validator is, and what it is NOT.** It does not embed a Mermaid
parser: the real one is JavaScript, and the API runtime has no Node. Rather than
approximate a grammar, the rules below encode *measured* parse behaviour — every
reserved word, every breaking character and every case-sensitivity rule here was
established by running the real ``mermaid.parse()`` over a corpus of generated
pages and over targeted probes, then keeping only what actually failed. Three
failure modes accounted for every observed invalid block:

- a reserved keyword used as a node id or participant alias,
- an unescaped bracket-class character in an *unquoted* flowchart label,
- a semicolon inside a ``sequenceDiagram`` message.

The limit is stated plainly rather than hidden: a *novel* failure mode passes
this gate and renders broken, exactly as it does today. That is a real gap, and
it is still strictly better than the generic structural check (known first word,
bracket balance, arrow present) that was tried first — that check flagged NONE
of the known-bad blocks, because no failure mode changes the diagram type, a
reserved word is not a bracket, and a stray bracket is usually offset by an
equal-and-opposite one in the same block. A validator honest about being partial
beats one that reports "clean" on a corpus with known-bad blocks in it.

Two measured asymmetries are load-bearing and easy to get backwards:

- ``sequenceDiagram`` keywords are matched **case-INSENSITIVELY** — ``Loop``,
  ``LOOP`` and ``loop`` all fail as a message endpoint.
- ``flowchart`` keywords are matched **case-SENSITIVELY** — only the exact
  lowercase ``graph`` fails as a node id; ``Graph`` is fine.

Declaring an alias is always legal (``participant Loop as ...`` parses); the
failure is at *use*, as a message endpoint. So the rules look at use sites only.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# ---------------------------------------------------------------------------
# Measured constants
# ---------------------------------------------------------------------------

# Words that fail as a message endpoint in a sequenceDiagram, matched
# case-insensitively. Established by probing each candidate as both the source
# and the target of a message against the real parser.
_SEQUENCE_RESERVED: frozenset[str] = frozenset({
    "accdescr", "acctitle", "activate", "actor", "alt", "and", "autonumber",
    "box", "break", "create", "critical", "deactivate", "destroy", "else",
    "end", "link", "links", "loop", "note", "opt", "option", "over", "par",
    "participant", "rect", "sequencediagram", "title",
})

# Words that fail as a flowchart node id, matched case-SENSITIVELY — the exact
# lowercase spelling is the only one the flowchart lexer reserves.
_FLOWCHART_RESERVED: frozenset[str] = frozenset({
    "class", "classDef", "end", "flowchart", "graph", "interpolate",
    "linkStyle", "style", "subgraph",
})

# Characters that fail inside an UNQUOTED flowchart label. Wrapping the label in
# double quotes fixes every one of them except the quote itself.
#
# ``@`` is deliberately ABSENT despite failing in isolation (``A[x@y]`` does not
# parse). It introduces node metadata (``id@{ shape: … }``) under a condition
# narrower than any spelling tried here: a prose label reading ``…\n@file /
# @dir`` parses cleanly while ``x@y`` does not, and two attempts to characterise
# the boundary each rejected that real, valid label. It causes no observed
# failure, so it is left unchecked and declared unchecked rather than guessed at
# — a rule that blocks a wiki has to be right about the labels it rejects.
_LABEL_BREAKERS: frozenset[str] = frozenset('[](){}|"')

# Opening shape delimiters, longest first — a flowchart node's shape is spelled
# by its brackets (``[[sub]]``, ``[(db)]``, ``((circle))``, ``[/par/]``, …), so
# the label scanner has to recognise the shape before it can judge the text
# inside it. Scanning for a bare bracket instead flags every legal shape.
_SHAPE_OPENERS: tuple[str, ...] = (
    "(((", "[[", "[(", "((", "([", "{{", "[/", "[\\", "[", "(", "{", ">",
)
_SHAPE_TRIM_LEAD = "[({/\\ "
_SHAPE_TRIM_TAIL = "])}/\\ "

# A sequenceDiagram message: ``A->>B: text``. The endpoint may carry an
# activation prefix (``->>+B``). Ordered so the longest arrow matches first.
_SEQUENCE_MESSAGE = re.compile(
    r"^(?P<src>[^\s:]+?)\s*"
    r"(?P<arrow><<-{1,2}>>|-{1,2}>>|-{1,2}>|-{1,2}[x)])\s*"
    r"(?P<dst>[+-]?[^\s:]+?)\s*:(?P<text>.*)$"
)

# A flowchart link. Alternation is longest-first so ``-->`` never matches as
# ``--`` with a stray ``>`` left behind in the next segment.
_FLOWCHART_LINK = re.compile(
    r"(?:--[xo]|[xo]--|<?-\.+-*>?|<?-{2,}>?|<?={2,}>?|~{3,})"
)
_EDGE_LABEL = re.compile(r"\|[^|]*\|")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<ticks>`{3,})[ \t]*(?P<info>[^\s`]*)[ \t]*$")


# ---------------------------------------------------------------------------
# The extracted block
# ---------------------------------------------------------------------------


class MermaidBlock(BaseModel):
    """One fenced ```mermaid block lifted out of a page body.

    Carries the text and the line-level parsing every rule shares. The page body
    arrives as an argument — this model reads no store and opens no file.
    """

    model_config = ConfigDict(extra="forbid")

    page_id: str
    block_index: int = Field(ge=0)
    source: str

    @classmethod
    def extract(cls, page_id: str, body: str) -> list[MermaidBlock]:
        """Return every ```mermaid fenced block in *body*, in document order.

        Matches the fence grammar the console's renderer accepts: three or more
        backticks with a ``mermaid`` info string, closed by a run of at least the
        same length. An unclosed fence takes the remainder of the body, which is
        what a Markdown renderer does with it too.
        """
        blocks: list[MermaidBlock] = []
        lines = body.splitlines()
        index = 0
        cursor = 0
        while cursor < len(lines):
            opening = _FENCE.match(lines[cursor])
            if opening is None or opening.group("info").lower() != "mermaid":
                cursor += 1
                continue
            ticks = opening.group("ticks")
            collected: list[str] = []
            cursor += 1
            while cursor < len(lines):
                closing = _FENCE.match(lines[cursor])
                if closing is not None and len(closing.group("ticks")) >= len(ticks):
                    if closing.group("info") == "":
                        break
                collected.append(lines[cursor])
                cursor += 1
            blocks.append(
                cls(page_id=page_id, block_index=index, source="\n".join(collected))
            )
            index += 1
            cursor += 1
        return blocks

    @property
    def declaration(self) -> str:
        """The diagram-type line — the first line that is not blank or a comment."""
        for _, text in self._significant_lines():
            return text
        return ""

    @property
    def family(self) -> str:
        """``flowchart``, ``sequence``, or ``other`` for a type carrying no rules.

        ``other`` covers ``classDiagram``/``erDiagram``/``mindmap`` and anything
        else: no measured failure mode reached them, so they are reported as
        unchecked rather than guessed at.
        """
        first = self.declaration.split()[0].lower() if self.declaration.split() else ""
        if first in ("flowchart", "graph"):
            return "flowchart"
        if first == "sequencediagram":
            return "sequence"
        return "other"

    def statements(self) -> list[tuple[int, str]]:
        """Return ``(line_number, text)`` for every statement after the declaration.

        Line numbers are 1-based **within the block**, which is the coordinate a
        repair needs: the page body's own numbering shifts as prose is edited.
        """
        return list(self._significant_lines())[1:]

    def _significant_lines(self) -> list[tuple[int, str]]:
        """Non-blank, non-comment lines with their 1-based in-block line numbers."""
        out: list[tuple[int, str]] = []
        for number, raw in enumerate(self.source.splitlines(), start=1):
            text = raw.strip()
            if not text or text.startswith("%%"):
                continue
            out.append((number, text))
        return out

    @staticmethod
    def labels(line: str) -> list[tuple[int, int, str]]:
        """Return ``(start, stop, inner_text)`` for each node label on *line*.

        Walks the line tracking quote state and bracket depth so a nested or
        unbalanced label is still bounded correctly, and peels the shape
        decoration off both ends (``[(db)]`` → ``db``) leaving only the text the
        author wrote.

        A shape opener is only recognised when it directly follows the node id it
        decorates. Without that anchor the ``>text]`` asymmetric shape captures
        the ``>`` of every ``-->`` and swallows the rest of the line as a label —
        which reads a clean ``M[Manager] --> G[Repo]`` as a nested-bracket
        defect, and did so on three quarters of a real corpus.
        """
        found: list[tuple[int, int, str]] = []
        index = 0
        while index < len(line):
            opener = next(
                (o for o in _SHAPE_OPENERS if line.startswith(o, index)), None
            )
            anchored = index > 0 and (line[index - 1].isalnum() or line[index - 1] == "_")
            if opener is None or not anchored:
                index += 1
                continue
            end = MermaidBlock._closing_index(line, index, opener[0])
            if end == -1:
                index += len(opener)
                continue
            inner = line[index + 1 : end].strip(_SHAPE_TRIM_LEAD + _SHAPE_TRIM_TAIL)
            found.append((index, end + 1, inner))
            index = end + 1
        return found

    @staticmethod
    def _closing_index(line: str, start: int, primary: str) -> int:
        """Index of the bracket closing the label opened at *start*, or ``-1``."""
        closer = {"[": "]", "(": ")", "{": "}", ">": "]"}[primary]
        depth = 0
        in_quote = False
        for scan in range(start, len(line)):
            char = line[scan]
            if char == '"':
                in_quote = not in_quote
                continue
            if in_quote:
                continue
            if char == primary:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth <= 0:
                    return scan
        return -1

    @staticmethod
    def node_ids(line: str) -> list[str]:
        """Return the identifiers occupying node-id position on a flowchart *line*.

        Label bodies are blanked before the line is split on links, so an arrow
        or a pipe inside a label can never be read as structure.
        """
        masked = list(line)
        for start, stop, _ in MermaidBlock.labels(line):
            for position in range(start, stop):
                masked[position] = " "
        flat = _EDGE_LABEL.sub(" ", "".join(masked))
        ids: list[str] = []
        for segment in _FLOWCHART_LINK.split(flat):
            candidate = _IDENTIFIER.match(segment.strip())
            if candidate is not None:
                ids.append(candidate.group(0))
        return ids


# ---------------------------------------------------------------------------
# The failure modes — a discriminated union, each member owning its own
# detection and its own repair instruction
# ---------------------------------------------------------------------------


class _MermaidDefect(BaseModel):
    """Shared shape of a detected defect. Never instantiated directly."""

    model_config = ConfigDict(extra="forbid")

    page_id: str
    block_index: int = Field(ge=0)
    line: int = Field(ge=1, description="1-based line number within the block.")
    excerpt: str = Field(description="The offending line, verbatim.")


class ReservedIdentifierDefect(_MermaidDefect):
    """A reserved keyword used as a node id or a message endpoint.

    The dominant mode by count, and the one that recurs: the words a writer
    reaches for when diagramming this codebase (``Loop`` for the tool-use loop,
    ``graph`` for the graph library) are exactly the ones the lexer claims.
    """

    kind: Literal["reserved_identifier"] = "reserved_identifier"
    identifier: str
    suggestion: str

    @property
    def instruction(self) -> str:
        """The repair to apply."""
        return (
            f"'{self.identifier}' is a reserved Mermaid keyword and cannot be a "
            f"node id or a message endpoint. Rename it to '{self.suggestion}' "
            f"everywhere it appears in this block, including its declaration. "
            f"The human-readable label may keep the original wording."
        )

    @classmethod
    def detect(cls, block: MermaidBlock) -> list[ReservedIdentifierDefect]:
        """Find reserved words used as identifiers in *block*."""
        if block.family == "sequence":
            return cls._detect_sequence(block)
        if block.family == "flowchart":
            return cls._detect_flowchart(block)
        return []

    @classmethod
    def _detect_sequence(cls, block: MermaidBlock) -> list[ReservedIdentifierDefect]:
        """Message endpoints, matched case-insensitively."""
        found: list[ReservedIdentifierDefect] = []
        for number, text in block.statements():
            message = _SEQUENCE_MESSAGE.match(text)
            if message is None:
                continue
            endpoints = (
                message.group("src"),
                message.group("dst").lstrip("+-"),
            )
            for endpoint in endpoints:
                if endpoint.startswith('"'):
                    continue
                if endpoint.lower() in _SEQUENCE_RESERVED:
                    found.append(cls(
                        page_id=block.page_id,
                        block_index=block.block_index,
                        line=number,
                        excerpt=text,
                        identifier=endpoint,
                        suggestion=f"{endpoint}Svc",
                    ))
        return found

    @classmethod
    def _detect_flowchart(cls, block: MermaidBlock) -> list[ReservedIdentifierDefect]:
        """Node ids, matched case-sensitively, on link-bearing lines only.

        A line with no link is a declaration (``subgraph S``, ``style a fill:…``,
        ``end``) where the keyword is legitimately the first token; only a line
        that wires nodes together puts an identifier in node-id position.
        """
        found: list[ReservedIdentifierDefect] = []
        for number, text in block.statements():
            if not _FLOWCHART_LINK.search(text):
                continue
            for identifier in block.node_ids(text):
                if identifier in _FLOWCHART_RESERVED:
                    found.append(cls(
                        page_id=block.page_id,
                        block_index=block.block_index,
                        line=number,
                        excerpt=text,
                        identifier=identifier,
                        suggestion=f"{identifier}Node",
                    ))
        return found


class UnquotedLabelDefect(_MermaidDefect):
    """A bracket-class character inside an unquoted flowchart label.

    Quoting the label makes every one of these legal, which is why the repair is
    always the same and always safe.
    """

    kind: Literal["unquoted_label"] = "unquoted_label"
    character: str
    label: str

    @property
    def instruction(self) -> str:
        """The repair to apply."""
        return (
            f"The label {self.label!r} contains {self.character!r}, which ends "
            f"the label early when it is unquoted. Wrap the label in double "
            f'quotes — Node["{self.label}"] — leaving the node id unchanged.'
        )

    @classmethod
    def detect(cls, block: MermaidBlock) -> list[UnquotedLabelDefect]:
        """Find unquoted labels carrying a breaking character."""
        if block.family != "flowchart":
            return []
        found: list[UnquotedLabelDefect] = []
        for number, text in block.statements():
            for _, _, inner in block.labels(text):
                if inner.startswith('"') and inner.endswith('"') and len(inner) >= 2:
                    continue
                breaker = next((c for c in inner if c in _LABEL_BREAKERS), None)
                if breaker is not None:
                    found.append(cls(
                        page_id=block.page_id,
                        block_index=block.block_index,
                        line=number,
                        excerpt=text,
                        character=breaker,
                        label=inner,
                    ))
        return found


class MessageSemicolonDefect(_MermaidDefect):
    """A semicolon inside a ``sequenceDiagram`` message.

    ``;`` is a statement separator wherever it appears, and — unlike a
    participant alias — quoting a message gives no protection. A *trailing*
    semicolon is legal (it just terminates the statement), so only an interior
    one is a defect.
    """

    kind: Literal["message_semicolon"] = "message_semicolon"
    message: str

    @property
    def instruction(self) -> str:
        """The repair to apply."""
        return (
            f"The message {self.message!r} contains ';', which always separates "
            f"statements — quoting does not escape it. Replace it with a comma "
            f"or a dash, or split the message in two."
        )

    @classmethod
    def detect(cls, block: MermaidBlock) -> list[MessageSemicolonDefect]:
        """Find semicolons inside message text."""
        if block.family != "sequence":
            return []
        found: list[MessageSemicolonDefect] = []
        for number, text in block.statements():
            message = _SEQUENCE_MESSAGE.match(text)
            if message is None:
                continue
            body = message.group("text").strip().rstrip(";")
            if ";" in body:
                found.append(cls(
                    page_id=block.page_id,
                    block_index=block.block_index,
                    line=number,
                    excerpt=text,
                    message=message.group("text").strip(),
                ))
        return found


_DefectModel = ReservedIdentifierDefect | UnquotedLabelDefect | MessageSemicolonDefect

#: The parse seam — one discriminated union, no dispatch anywhere.
MermaidDefect = Annotated[_DefectModel, Field(discriminator="kind")]

#: A rule IS a union member: adding a fourth measured failure mode means writing
#: a class that owns its own ``detect`` and ``instruction``, not extending a
#: table here.
MermaidRule = (
    type[ReservedIdentifierDefect]
    | type[UnquotedLabelDefect]
    | type[MessageSemicolonDefect]
)


# ---------------------------------------------------------------------------
# The refusal envelope — a trust boundary, read back by the model
# ---------------------------------------------------------------------------


class MermaidRepair(BaseModel):
    """One actionable repair: which page, which block, which line, what to do."""

    model_config = ConfigDict(extra="forbid")

    page_id: str
    block_index: int = Field(ge=0)
    line: int = Field(ge=1)
    excerpt: str
    defect: MermaidDefect
    instruction: str

    @classmethod
    def of(cls, defect: _DefectModel) -> MermaidRepair:
        """Project a defect into the wire shape, asking it for its own instruction."""
        return cls(
            page_id=defect.page_id,
            block_index=defect.block_index,
            line=defect.line,
            excerpt=defect.excerpt,
            defect=defect,
            instruction=defect.instruction,
        )


class MermaidGateError(BaseModel):
    """The ``{code, message}`` body every wiki tool error carries."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["validation"] = "validation"
    message: str


class MermaidRejection(BaseModel):
    """The ``wiki_finalize`` refusal for a wiki carrying invalid diagrams.

    Read back by the model, so it is a validated contract rather than a
    formatted string. It states what was checked as plainly as what failed:
    ``coverage`` names the modes this gate actually tests, because a gate that
    implied full coverage would be lying about the diagrams it let through.
    """

    model_config = ConfigDict(extra="forbid")

    error: MermaidGateError
    pages_saved: bool = Field(
        default=True,
        description="Pages are persisted before this gate runs and are never discarded.",
    )
    repairs: list[MermaidRepair]
    coverage: str = Field(
        default=(
            "Rule-based over three measured failure modes (reserved identifier, "
            "unquoted label, message semicolon) for flowchart and sequenceDiagram "
            "blocks. Other diagram types and novel failure modes are NOT checked."
        )
    )
    next_step: str = Field(
        default=(
            "Every page is already saved — do not regenerate the wiki. Spawn one "
            "wiki-page-writer per affected page, scoped to repairing only the "
            "diagrams listed in `repairs`, then call wiki_finalize again."
        )
    )


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


class MermaidValidator:
    """Applies the failure-mode rules to a set of generated pages.

    Rules are injected, defaulting to the three measured modes, so a test can
    exercise one in isolation and a fourth measured mode is added by writing a
    union member rather than by editing a dispatch table here.
    """

    DEFAULT_RULES: ClassVar[tuple[MermaidRule, ...]] = (
        ReservedIdentifierDefect,
        UnquotedLabelDefect,
        MessageSemicolonDefect,
    )

    def __init__(self, rules: Sequence[MermaidRule] | None = None) -> None:
        """Bind the rule set; ``None`` selects the three measured modes."""
        self._rules: tuple[MermaidRule, ...] = (
            tuple(rules) if rules is not None else self.DEFAULT_RULES
        )

    def inspect(self, page_id: str, body: str) -> list[MermaidRepair]:
        """Return every repair owed by the diagrams in one page body."""
        repairs: list[MermaidRepair] = []
        for block in MermaidBlock.extract(page_id, body):
            for rule in self._rules:
                repairs.extend(MermaidRepair.of(d) for d in rule.detect(block))
        return repairs

    def review(self, pages: Iterable[tuple[str, str]]) -> MermaidRejection | None:
        """Return a rejection for *pages*, or ``None`` when every diagram passes.

        *pages* is any iterable of ``(page_id, body)`` — the page bodies arrive
        as an argument, so nothing here reads a store. Returning ``None`` for a
        clean wiki keeps the caller's happy path a single falsy check.
        """
        repairs: list[MermaidRepair] = []
        for page_id, body in pages:
            repairs.extend(self.inspect(page_id, body))
        if not repairs:
            return None
        pages_affected = len({r.page_id for r in repairs})
        return MermaidRejection(
            error=MermaidGateError(
                message=(
                    f"cannot finalize: {len(repairs)} invalid Mermaid diagram(s) "
                    f"across {pages_affected} page(s) would render as an error. "
                    f"Every page is saved; repair the diagrams listed in "
                    f"`repairs` and call wiki_finalize again."
                ),
            ),
            repairs=repairs,
        )


__all__ = [
    "MermaidBlock",
    "MermaidDefect",
    "MermaidGateError",
    "MermaidRejection",
    "MermaidRepair",
    "MermaidRule",
    "MermaidValidator",
    "MessageSemicolonDefect",
    "ReservedIdentifierDefect",
    "UnquotedLabelDefect",
]
