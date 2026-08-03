"""Tests for the Mermaid validity gate on ``wiki_finalize``.

The corpus below is not invented. Every block in ``KNOWN_BAD`` is a diagram a
real page-writer produced that the real ``mermaid.parse()`` rejected, and every
block in ``KNOWN_GOOD`` is one it accepted. The two lists are the actual
contract: catching a failure is worth nothing if the same rule also rejects
valid work, so both directions are asserted.

``KNOWN_GOOD`` is weighted toward blocks that a *plausible* implementation of
these rules gets wrong — legal bracket shapes, quoted labels holding the very
characters the rules ban, trailing semicolons, and a capitalised keyword that is
reserved in one diagram family but not the other. Each entry names the mistake it
guards, because a bare list of valid diagrams reads as filler and gets deleted.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import mewbo_graph.plugins.wiki.finalize as finalize_mod
import pytest
from mewbo_core.common import MockSpeaker
from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool
from mewbo_graph.plugins.wiki.mermaid import (
    MermaidBlock,
    MermaidValidator,
    MessageSemicolonDefect,
    ReservedIdentifierDefect,
    UnquotedLabelDefect,
)
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import Frontmatter, IndexingJob, WikiPage

# ── The corpus ────────────────────────────────────────────────────────────────

# Diagrams the real parser rejected, paired with the mode that explains each.
# Two carry rewritten identifiers; the failing structure is preserved verbatim.
KNOWN_BAD: list[tuple[str, str, str]] = [
    (
        "reserved keyword as a sequence message target",
        "reserved_identifier",
        """sequenceDiagram
    participant Caller as Test caller
    participant Eng as SchedulerEngine
    participant Loop as Async periodic loop
    Caller->>Eng: start()
    Eng->>Loop: schedule periodic work
    Loop->>Caller: completion callback""",
    ),
    (
        "reserved keyword as a message target, quoted aliases",
        "reserved_identifier",
        """sequenceDiagram
    participant Surface as "Application surface"
    participant API as "Session/runtime boundary"
    participant Loop as "ToolUseLoop"
    Surface->>API: "Submit turn"
    API->>Loop: "Resolve session and run\"""",
    ),
    (
        "reserved keyword as a message source",
        "reserved_identifier",
        """sequenceDiagram
    participant Test as "pytest test"
    participant Loop as "ToolUseLoop"
    participant Model as "AsyncMock model"
    Test->>Loop: "run with real context"
    Loop->>Model: "ainvoke\"""",
    ),
    (
        "lowercase 'graph' as a flowchart node id",
        "reserved_identifier",
        """flowchart TD
    core["mewbo_core orchestration SDK"] --> tools["mewbo_tools integrations"]
    core --> graph["mewbo_graph graph and memory"]
    core --> iam["mewbo_iam identity kernel"]""",
    ),
    (
        "nested bracket in an unquoted label",
        "unquoted_label",
        """flowchart TD
  P[toolkit base install] --> T[CLI only]
  P --> D[toolkit[daemon]]
  P --> C[toolkit[client]]""",
    ),
    (
        "parenthesis in an unquoted label",
        "unquoted_label",
        """flowchart LR
  Lifecycle --> Session[Tagged maintainer session]
  Session --> Data[AppDataStore\\n(app, collection, key)]
  Session --> Tracker[PipelineRun tracker]""",
    ),
    (
        "semicolon inside a sequence message",
        "message_semicolon",
        """sequenceDiagram
    participant P as "Platform"
    participant S as "AuraSession"
    participant M as "AssistTurnMachine"
    P->>S: "onShow"
    S->>M: "show; startListening\"""",
    ),
]

# Diagrams the real parser accepted. Each names the false positive it guards.
KNOWN_GOOD: list[tuple[str, str]] = [
    (
        "'-->' must not be read as the '>text]' asymmetric shape opener — "
        "reading it that way swallows the rest of the line as one nested label",
        """flowchart TD
  M[QueueManager] --> G[BlobStore]
  B[BackendConfig] --> C[TransportClient]""",
    ),
    (
        "every legal node shape is spelled with the characters the label rule bans",
        """flowchart TD
  a[[subroutine]] --> b[(database)]
  c((circle)) --> d{{hexagon}}
  e([stadium]) --> f>asymmetric]
  g[/parallelogram/] --> h[\\alternate\\]
  i{rhombus} --> j((circle two))""",
    ),
    (
        "a QUOTED label may hold the banned characters — quoting is the fix, "
        "so a rule that ignores quoting rejects its own remedy",
        """flowchart LR
  a["AppDataStore (app, collection, key)"] --> b["toolkit[daemon]"]
  c["pair_backend()"] --> d["POST /auth/pair"]""",
    ),
    (
        "'@' in prose parses; it only marks node metadata in a narrower position "
        "than any simple rule captures, so it is left unchecked",
        """flowchart TD
  U[User request] --> R[Reference expansion\\n@file / @dir / @diff / @URL]""",
    ),
    (
        "a TRAILING semicolon is a legal statement terminator in both families",
        """graph TD;
    A-->B;
    B-->C;""",
    ),
    (
        "a trailing semicolon on a sequence message is legal — only an interior one fails",
        """sequenceDiagram
    participant A as Alpha
    participant B as Beta
    A->>B: hello;""",
    ),
    (
        "flowchart keywords are CASE-SENSITIVE: 'End' and 'Graph' are valid node ids "
        "even though 'end' and 'graph' are not",
        """flowchart TD
  Start[Begin] --> Graph[Build graph]
  Graph --> End[Done]""",
    ),
    (
        "'subgraph'/'end' and 'style' are legitimate statement keywords",
        """flowchart TD
  subgraph Ingest
    a --> b
  end
  b --> c
  style a fill:#eee""",
    ),
    (
        "a piped edge label is not a node id",
        """flowchart TD
  a -->|yes| b
  b -->|no| c""",
    ),
    (
        "sequence activation markers prefix the endpoint and are not part of its name",
        """sequenceDiagram
    participant A as Alpha
    participant B as Beta
    A->>+B: go
    B-->>-A: done""",
    ),
]


def _fence(source: str) -> str:
    """Wrap a diagram in the fenced block a page body carries it in."""
    return f"# Page\n\nProse.\n\n```mermaid\n{source}\n```\n\nMore prose.\n"


# ── The three failure modes ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected_kind", "source"),
    KNOWN_BAD,
    ids=[case[0] for case in KNOWN_BAD],
)
def test_known_bad_block_is_caught_with_its_mode(
    label: str, expected_kind: str, source: str
) -> None:
    """Every diagram the real parser rejected is flagged, under the right mode."""
    del label
    repairs = MermaidValidator().inspect("page", _fence(source))

    assert repairs, "a block the real parser rejected was passed as clean"
    assert expected_kind in {r.defect.kind for r in repairs}


@pytest.mark.parametrize(
    ("guards", "source"), KNOWN_GOOD, ids=[case[0][:60] for case in KNOWN_GOOD]
)
def test_known_good_block_is_not_flagged(guards: str, source: str) -> None:
    """No diagram the real parser accepted is rejected."""
    repairs = MermaidValidator().inspect("page", _fence(source))

    assert repairs == [], f"false positive — this case guards: {guards}"


def test_reserved_words_are_case_insensitive_in_sequence_only() -> None:
    """The measured asymmetry between the two families, asserted both ways.

    Getting this backwards is silent in both directions: a case-sensitive
    sequence rule misses ``Loop`` (the dominant real failure), and a
    case-insensitive flowchart rule rejects ``Graph``, which parses fine.
    """
    validator = MermaidValidator()
    sequence = "sequenceDiagram\n    participant {0} as W\n    A->>{0}: hi"
    flowchart = "flowchart TD\n    a[A] --> {0}[W]"

    for spelling in ("loop", "Loop", "LOOP"):
        assert validator.inspect("p", _fence(sequence.format(spelling))), spelling

    assert validator.inspect("p", _fence(flowchart.format("graph")))
    assert validator.inspect("p", _fence(flowchart.format("end")))
    for spelling in ("Graph", "GRAPH", "End", "Style"):
        assert validator.inspect("p", _fence(flowchart.format(spelling))) == [], spelling


def test_declaring_a_reserved_alias_is_allowed_only_using_it_fails() -> None:
    """``participant Loop as ...`` parses; the failure is at the use site."""
    declaration_only = "sequenceDiagram\n    participant Loop as Async loop\n"

    assert MermaidValidator().inspect("p", _fence(declaration_only)) == []


def test_each_rule_can_be_exercised_in_isolation() -> None:
    """Rules are injected, so a suite can bind one without the others firing."""
    semicolon_only = MermaidValidator(rules=(MessageSemicolonDefect,))
    reserved_and_semicolon = """sequenceDiagram
    participant Loop as L
    A->>Loop: "a; b\""""

    kinds = {r.defect.kind for r in semicolon_only.inspect("p", _fence(reserved_and_semicolon))}

    assert kinds == {"message_semicolon"}


# ── Honest coverage: what the gate does NOT check ─────────────────────────────


def test_unchecked_diagram_types_pass_but_are_declared_unchecked() -> None:
    """A type carrying no measured rule is reported as unchecked, never as valid.

    This is the gate's degradation contract. No Mermaid parser is reachable
    in-process, so ``classDiagram``/``erDiagram``/``mindmap`` are not validated
    at all — the payload has to say so rather than let silence read as a pass.
    """
    validator = MermaidValidator()
    other = "classDiagram\n    class Loop\n    Loop : +run() void"

    assert MermaidBlock.extract("p", _fence(other))[0].family == "other"
    assert validator.inspect("p", _fence(other)) == []

    rejection = validator.review([("bad", _fence(KNOWN_BAD[0][2]))])
    assert rejection is not None
    assert "NOT checked" in rejection.coverage
    assert "novel failure modes" in rejection.coverage


def test_validator_never_raises_on_malformed_input() -> None:
    """Unparseable input degrades to "nothing found", never to an exception.

    A gate that throws on a malformed body would fail the whole index for a
    diagram it merely could not read.
    """
    validator = MermaidValidator()
    malformed = [
        "```mermaid\n",
        "```mermaid\nflowchart TD\n  a[unclosed\n```\n",
        "```mermaid\n\n```\n",
        "```mermaid\n%%{init: {'theme':'dark'}}%%\n```\n",
        "no fences at all",
    ]

    for body in malformed:
        assert validator.inspect("p", body) == []


def test_only_mermaid_fences_are_inspected() -> None:
    """A python block that happens to contain diagram-like text is ignored."""
    body = "```python\nx = 'A->>Loop: not a diagram'\n```\n"

    assert MermaidBlock.extract("p", body) == []
    assert MermaidValidator().inspect("p", body) == []


# ── The refusal payload ───────────────────────────────────────────────────────


def test_rejection_is_actionable_per_block() -> None:
    """Each repair names page, block, line and the fix — enough to act on alone."""
    body = _fence(KNOWN_BAD[4][2])  # nested bracket, on the third line of the block

    rejection = MermaidValidator().review([("packaging", body)])

    assert rejection is not None
    assert rejection.pages_saved is True
    assert rejection.error.code == "validation"
    repair = rejection.repairs[0]
    assert repair.page_id == "packaging"
    assert repair.block_index == 0
    assert repair.line == 3
    assert "toolkit[daemon]" in repair.excerpt
    assert "quotes" in repair.instruction
    assert "wiki_finalize again" in rejection.next_step


def test_gate_is_per_block_not_per_page() -> None:
    """A page keeps its good diagrams; only the bad block is named.

    Every affected page observed carried exactly one bad diagram among good
    ones, so rejecting whole pages would discard sound work.
    """
    good, bad = KNOWN_GOOD[0][1], KNOWN_BAD[3][2]
    body = f"```mermaid\n{good}\n```\n\n```mermaid\n{bad}\n```\n\n```mermaid\n{good}\n```\n"

    repairs = MermaidValidator().inspect("multi", body)

    assert {r.block_index for r in repairs} == {1}


def test_rejection_round_trips_through_the_discriminated_union() -> None:
    """The payload is a validated contract, not a formatted string."""
    from mewbo_graph.plugins.wiki.mermaid import MermaidRejection

    original = MermaidValidator().review([("p", _fence(KNOWN_BAD[6][2]))])
    assert original is not None

    revived = MermaidRejection.model_validate(original.model_dump())

    assert revived == original
    assert isinstance(revived.repairs[0].defect, MessageSemicolonDefect)


def test_defect_kinds_are_distinct_union_members() -> None:
    """Each mode owns its own detection and instruction — no shared dispatch."""
    for rule, source in (
        (ReservedIdentifierDefect, KNOWN_BAD[0][2]),
        (UnquotedLabelDefect, KNOWN_BAD[4][2]),
        (MessageSemicolonDefect, KNOWN_BAD[6][2]),
    ):
        block = MermaidBlock.extract("p", _fence(source))[0]
        found = rule.detect(block)

        assert found, rule.__name__
        assert found[0].instruction


# ── The finalize gate ─────────────────────────────────────────────────────────


def _store(tmp_path: Path, slug: str) -> JsonWikiStore:
    """A store with the code-graph node finalize's completion gate requires."""
    from mewbo_graph.wiki.types import make_graph_node

    store = JsonWikiStore(root_dir=tmp_path)
    store.upsert_nodes(
        slug,
        [make_graph_node(
            slug=slug, node_id=f"{slug}:n1", type="File",
            name="a.py", file="a.py", range=(0, 0),
        )],
    )
    return store


def _save_page(store: JsonWikiStore, slug: str, page_id: str, body: str) -> None:
    store.save_page(slug, WikiPage(
        id=page_id,
        title=page_id,
        frontmatter=Frontmatter(title=page_id, slug=page_id),
        body=body,
        toc=[],
        nav=[],
    ))


def _seed_job(store: JsonWikiStore, job_id: str, session_id: str, slug: str) -> None:
    store.create_job(IndexingJob(
        job_id=job_id, slug=slug, status="finalizing",
        scanned_count=1, total_count=1, current_file=None,
    ))
    store.attach_job_session(job_id, session_id)
    store.save_job_submission(job_id, {
        "repoUrl": "https://github.com/org/repo",
        "slug": slug,
        "platform": "github",
        "language": "en",
        "depth": "concise",
        "model": "anthropic/claude-sonnet-4-6",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    })


def _finalize(
    store: JsonWikiStore, session_id: str, landing: str
) -> tuple[MockSpeaker, WikiFinalizeTool]:
    """Drive one real ``wiki_finalize`` call, returning its result and the tool.

    The tool comes back because ``should_terminate_run()`` is part of the
    contract under test — a refusal must leave the run alive.
    """
    step = MagicMock()
    step.tool_input = {"landingPageId": landing}
    tool = WikiFinalizeTool(session_id=session_id)
    with patch.object(
        finalize_mod, "_resolve_runtime", return_value=SimpleNamespace(wiki_store=store)
    ):
        result = asyncio.run(tool.handle(step))
    return result, tool


def test_finalize_is_blocked_and_pages_survive(tmp_path: Path) -> None:
    """The crux: pages are SAVED, only the finalize is refused.

    Nothing is discarded and the expensive phases are never redone — the model
    repairs the named diagrams against work that is still on disk.
    """
    slug = "org/repo"
    store = _store(tmp_path, slug)
    _seed_job(store, "job-mm", "sess-mm", slug)
    _save_page(store, slug, "overview", _fence(KNOWN_BAD[3][2]))
    _save_page(store, slug, "healthy", _fence(KNOWN_GOOD[0][1]))

    result, tool = _finalize(store, "sess-mm", "overview")

    assert "repairs" in result.content
    assert "validation" in result.content

    # Both pages are still persisted, bodies untouched.
    assert {p.id for p in store.list_pages(slug)} == {"overview", "healthy"}
    assert store.get_page(slug, "overview").body == _fence(KNOWN_BAD[3][2])

    # The index did NOT complete.
    assert store.get_project(slug) is None
    job = store.get_job("job-mm")
    assert job is not None
    assert job.status == "finalizing"

    # The run stays alive so the model can repair — the trap this gate must not
    # spring is refusing while also signalling termination, which strands the
    # session with no terminal event.
    assert tool.should_terminate_run() is False


def test_finalize_refusal_names_the_page_and_the_fix(tmp_path: Path) -> None:
    """The refusal is self-instructing: which page, which block, what to do."""
    slug = "org/repo"
    store = _store(tmp_path, slug)
    _seed_job(store, "job-mm2", "sess-mm2", slug)
    _save_page(store, slug, "aura", _fence(KNOWN_BAD[6][2]))

    result, _ = _finalize(store, "sess-mm2", "aura")

    assert "aura" in result.content
    assert "message_semicolon" in result.content
    assert "pages_saved" in result.content


def test_finalize_completes_when_every_diagram_is_valid(tmp_path: Path) -> None:
    """The gate is invisible on a clean wiki — no behaviour change."""
    slug = "org/repo"
    store = _store(tmp_path, slug)
    _seed_job(store, "job-mm3", "sess-mm3", slug)
    for page_id, (_, source) in zip(("overview", "shapes"), KNOWN_GOOD[:2], strict=True):
        _save_page(store, slug, page_id, _fence(source))

    result, tool = _finalize(store, "sess-mm3", "overview")

    assert "repairs" not in result.content
    assert "complete" in result.content
    assert store.get_job("job-mm3").status == "complete"
    assert tool.should_terminate_run() is True


def test_finalize_succeeds_after_the_named_diagram_is_repaired(tmp_path: Path) -> None:
    """The full loop: refuse, repair only what was named, finalize again."""
    slug = "org/repo"
    store = _store(tmp_path, slug)
    _seed_job(store, "job-mm4", "sess-mm4", slug)
    _save_page(store, slug, "overview", _fence(KNOWN_BAD[3][2]))

    first, _ = _finalize(store, "sess-mm4", "overview")
    assert "repairs" in first.content

    # Rename the reserved id, exactly as the instruction directs.
    repaired = _fence(KNOWN_BAD[3][2].replace("graph[", "graphLib["))
    _save_page(store, slug, "overview", repaired)

    second, tool = _finalize(store, "sess-mm4", "overview")

    assert "repairs" not in second.content
    assert store.get_job("job-mm4").status == "complete"
    assert tool.should_terminate_run() is True
