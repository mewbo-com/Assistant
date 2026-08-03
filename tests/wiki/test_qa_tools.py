"""QA tools tests."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.plugins.wiki import (
    code_search as code_search_mod,
    emit_answer as emit_answer_mod,
    read_page as read_page_mod,
    search_pages as search_pages_mod,
)
from mewbo_graph.plugins.wiki.code_search import WikiCodeSearchTool
from mewbo_graph.plugins.wiki.emit_answer import WikiEmitAnswerTool
from mewbo_graph.plugins.wiki.read_page import WikiReadPageTool
from mewbo_graph.plugins.wiki.search_pages import WikiSearchPagesTool
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    Embedding,
    QaAnswer,
    WikiPage,
    make_graph_node,
)


@pytest.fixture
def qa_setup(tmp_path):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    # Seed an answer + session.
    ans = QaAnswer(
        answerId="a1",
        fromPageId="overview",
        summarySources=[],
        model="anthropic/claude-sonnet-4-6",
        blocks=[],
        slug="x/y",
    )
    store.save_qa(ans)
    store.attach_qa_session("a1", "sess-qa-1")
    # Seed some pages + graph for retrieval.
    store.save_page("x/y", WikiPage(
        id="auth", title="Auth",
        frontmatter={"title": "Auth", "slug": "auth"},
        body="Tokens, sessions, login flow.",
        toc=[], nav=[],
    ))
    store.save_page("x/y", WikiPage(
        id="overview", title="Overview",
        frontmatter={"title": "Overview", "slug": "overview"},
        body="System mechanics overview.",
        toc=[], nav=[],
    ))
    store.upsert_nodes("x/y", [
        make_graph_node(
            slug="x/y",
            node_id="f1",
            type="Function",
            name="authenticate",
            file="auth.py",
            range=(0, 100),
            docstring="Verify token.",
        ),
    ])
    store.upsert_embeddings("x/y", [
        Embedding(slug="x/y", node_id="f1", vector=[1.0, 0.0], model="m", dim=2),
    ])
    return store, "sess-qa-1"


def _runtime(store):
    return MagicMock(wiki_store=store)


def _fake_embedder(qvec):
    e = MagicMock()
    e.embed_nodes.return_value = [MagicMock(vector=qvec)]
    return e


def test_search_pages_returns_relevant_page(qa_setup):
    store, sid = qa_setup
    tool = WikiSearchPagesTool(session_id=sid)
    step = MagicMock(tool_input={"query": "authentication token"})
    with patch.object(search_pages_mod, "_resolve_runtime", return_value=_runtime(store)), \
         patch.object(search_pages_mod, "_make_embedder", return_value=_fake_embedder([0.0, 0.0])):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "auth" in body  # the auth page should rank first


def test_search_pages_validates_args(qa_setup):
    store, sid = qa_setup
    tool = WikiSearchPagesTool(session_id=sid)
    step = MagicMock(tool_input={"bogus": "x"})
    with patch.object(search_pages_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "validation" in str(result.content)


def test_read_page_returns_full_shape(qa_setup):
    store, sid = qa_setup
    tool = WikiReadPageTool(session_id=sid)
    step = MagicMock(tool_input={"pageId": "auth"})
    with patch.object(read_page_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "Auth" in body  # title
    assert "Tokens" in body  # body


def test_read_page_not_found(qa_setup):
    store, sid = qa_setup
    tool = WikiReadPageTool(session_id=sid)
    step = MagicMock(tool_input={"pageId": "missing"})
    with patch.object(read_page_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "not_found" in str(result.content)


def test_code_search_returns_node_hit(qa_setup):
    store, sid = qa_setup
    tool = WikiCodeSearchTool(session_id=sid)
    step = MagicMock(tool_input={"query": "authenticate", "k": 5})
    with patch.object(code_search_mod, "_resolve_runtime", return_value=_runtime(store)), \
         patch.object(code_search_mod, "_make_embedder", return_value=_fake_embedder([1.0, 0.0])):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "f1" in body or "authenticate" in body


def _access_events(store, answer_id="a1"):
    return [e for e in store.load_qa_events(answer_id) if e["type"] == "access"]


def test_code_search_records_scored_hits_with_rank(qa_setup):
    """code_search records ranked hits carrying real score + 1-based rank.

    The trail must capture the retriever's ranking signal — not an unscored bulk
    dump — so the finalizer can score-order + cap it. Each record uses the new
    ``records`` shape (not the legacy bare ``refs``).
    """
    store, sid = qa_setup
    tool = WikiCodeSearchTool(session_id=sid)
    step = MagicMock(tool_input={"query": "authenticate", "k": 5})
    with patch.object(code_search_mod, "_resolve_runtime", return_value=_runtime(store)), \
         patch.object(code_search_mod, "_make_embedder", return_value=_fake_embedder([1.0, 0.0])):
        asyncio.run(tool.handle(step))

    access = _access_events(store)
    assert access, "code_search should record an access event"
    recs = access[-1]["records"]
    assert recs and all(r["ref"].startswith("graph:") for r in recs)
    assert recs[0]["score"] is not None
    assert recs[0]["rank"] == 1
    assert recs[0]["op"] == "search"
    assert recs[0]["tool"] == "wiki_code_search"
    # New shape only — no legacy bare-ref list.
    assert "refs" not in access[-1]


def test_read_page_records_unscored_grounding_touch(qa_setup):
    """A page read is a grounding confirmation — recorded unscored (no score/rank)."""
    store, sid = qa_setup
    tool = WikiReadPageTool(session_id=sid)
    step = MagicMock(tool_input={"pageId": "auth"})
    with patch.object(read_page_mod, "_resolve_runtime", return_value=_runtime(store)):
        asyncio.run(tool.handle(step))

    access = _access_events(store)
    assert len(access) == 1
    recs = access[0]["records"]
    assert recs == [{
        "ref": "wiki:auth", "score": None, "rank": None,
        "tool": "wiki_read_page", "op": "read", "ok": True,
    }]


def test_emit_answer_persists_block_events_in_order_and_completes(qa_setup):
    """ONE atomic call fans its blocks into ordered block_open/block_close pairs.

    The array is delivered whole; the tool fans it server-side into the SAME
    per-block event contract the old choreography produced (index-keyed,
    in order), then appends the terminal ``complete`` event.
    """
    store, sid = qa_setup
    assert store.get_qa("a1").status == "running"  # precondition
    tool = WikiEmitAnswerTool(session_id=sid)
    step = MagicMock(tool_input={"blocks": [
        {"kind": "p", "text": "Hello, world."},
        {"kind": "sources", "items": ["src/main.py"]},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "ok" in str(result.content)
    events = store.load_qa_events("a1")
    types = [e["type"] for e in events]
    assert types == ["block_open", "block_close", "block_open", "block_close", "complete"]
    opened = [e for e in events if e["type"] == "block_open"]
    assert [e["index"] for e in opened] == [0, 1]
    assert [e["block"]["kind"] for e in opened] == ["p", "sources"]
    assert store.get_qa("a1").status == "complete"


def test_emit_answer_invalid_block_kind(qa_setup):
    """A bad block anywhere in the array surfaces a positional validation error."""
    store, sid = qa_setup
    tool = WikiEmitAnswerTool(session_id=sid)
    step = MagicMock(tool_input={"blocks": [
        {"kind": "not_a_real_kind", "text": "x"},
        {"kind": "sources", "items": ["src/main.py"]},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "validation" in body
    assert "blocks[0] invalid" in body


def test_emit_answer_refuses_when_already_emitted(qa_setup):
    """A second call after an answer already has ``block_open`` events is refused."""
    store, sid = qa_setup
    store.append_qa_event("a1", {
        "type": "block_open", "index": 0, "block": {"kind": "p", "text": "already there"},
    })
    tool = WikiEmitAnswerTool(session_id=sid)
    step = MagicMock(tool_input={"blocks": [
        {"kind": "p", "text": "x"},
        {"kind": "sources", "items": ["src/main.py"]},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "validation" in body
    assert "already emitted" in body


def test_emit_answer_requires_trailing_sources_block(qa_setup):
    """No ``sources`` block (or not last) → a validation error naming ``sources``.

    A rejected call writes NOTHING — the snapshot stays whatever it was before
    the attempt (``running``, since this fixture's answer never terminated).
    """
    store, sid = qa_setup
    tool = WikiEmitAnswerTool(session_id=sid)
    step = MagicMock(tool_input={"blocks": [
        {"kind": "p", "text": "a"},
        {"kind": "p", "text": "b"},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "validation" in body
    assert "sources" in body
    assert store.load_qa_events("a1") == []
    assert store.get_qa("a1").status == "running"


# ---------------------------------------------------------------------------
# Mermaid gate — refuse the emit, never terminate, nothing persisted
# ---------------------------------------------------------------------------


def test_emit_answer_refuses_known_bad_mermaid_without_terminating(qa_setup):
    """A fenced diagram matching a real measured failure mode is refused.

    Reuses ``test_mermaid_gate.py``'s own corpus (the reserved-keyword
    'graph' node id) so the two suites can't silently drift on what
    "known-bad" means. The run stays alive — ``should_terminate_run()`` is
    still False and the store gets NO new events — so the model can repair
    and re-send, exactly like the block-validation errors above.
    """
    from wiki.test_mermaid_gate import KNOWN_BAD  # noqa: PLC0415 (sibling test helper)

    store, sid = qa_setup
    bad_diagram = KNOWN_BAD[3][2]  # "lowercase 'graph' as a flowchart node id"
    tool = WikiEmitAnswerTool(session_id=sid)
    step = MagicMock(tool_input={"blocks": [
        {"kind": "p", "text": f"Here is the module layout:\n```mermaid\n{bad_diagram}\n```"},
        {"kind": "sources", "items": ["src/main.py"]},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "validation" in body
    assert "Mermaid" in body
    assert "graph" in body
    assert store.load_qa_events("a1") == []
    assert store.get_qa("a1").status == "running"
    assert tool.should_terminate_run() is False


def test_emit_answer_accepts_known_good_mermaid(qa_setup):
    """A fenced diagram the real parser accepts is unaffected by the gate.

    Reuses ``test_mermaid_gate.py``'s own known-good corpus for the same
    drift-proofing reason as the refusal test above.
    """
    from wiki.test_mermaid_gate import KNOWN_GOOD  # noqa: PLC0415 (sibling test helper)

    store, sid = qa_setup
    good_diagram = KNOWN_GOOD[0][1]
    tool = WikiEmitAnswerTool(session_id=sid)
    step = MagicMock(tool_input={"blocks": [
        {"kind": "p", "text": f"Here is the module layout:\n```mermaid\n{good_diagram}\n```"},
        {"kind": "sources", "items": ["src/main.py"]},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "ok" in str(result.content)
    assert store.get_qa("a1").status == "complete"


def test_emit_answer_unaffected_when_no_mermaid_present(qa_setup):
    """An ordinary answer with no fence at all is untouched by the gate."""
    store, sid = qa_setup
    tool = WikiEmitAnswerTool(session_id=sid)
    step = MagicMock(tool_input={"blocks": [
        {"kind": "p", "text": "No diagram needed for this narrow lookup."},
        {"kind": "sources", "items": ["src/main.py"]},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "ok" in str(result.content)
    assert store.get_qa("a1").status == "complete"


# ---------------------------------------------------------------------------
# Terminal status on the snapshot — the MCP poll's done-signal
# ---------------------------------------------------------------------------


def test_qa_answer_defaults_to_running_status():
    """A freshly-minted QaAnswer reports ``status='running'`` (serialized)."""
    ans = QaAnswer(
        answerId="a-status",
        fromPageId="overview",
        summarySources=[],
        model="m",
        blocks=[],
        slug="x/y",
    )
    assert ans.status == "running"
    # Serialized snapshot (what GET /v1/wiki/qa/<id> returns) carries the field.
    assert ans.model_dump(by_alias=True)["status"] == "running"


def test_terminal_call_sets_complete_on_snapshot(qa_setup):
    """A valid whole-answer call flips the persisted status to ``complete``."""
    store, sid = qa_setup
    tool = WikiEmitAnswerTool(session_id=sid)
    step = MagicMock(tool_input={"blocks": [
        {"kind": "p", "text": "The answer."},
        {"kind": "sources", "items": ["src/main.py"]},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "ok" in str(result.content)
    # The reloaded snapshot — exactly what the GET route serializes — is terminal.
    reloaded = store.get_qa("a1")
    assert reloaded is not None
    assert reloaded.status == "complete"
    assert reloaded.model_dump(by_alias=True)["status"] == "complete"


# ---------------------------------------------------------------------------
# Terminal-tool loop contract — should_terminate_run → terminal_reason
# ---------------------------------------------------------------------------


def test_emit_answer_terminal_reason_is_completed():
    """``WikiEmitAnswerTool`` inherits ``terminal_reason() == "completed"``.

    A tool overriding ``should_terminate_run`` but declaring no
    ``terminal_reason`` makes ``tool_use_loop`` raise AttributeError
    when it selects the terminating tool. The reason lives on the shared
    ``WikiSessionTool`` base — this test fails if that base method is removed,
    because the base IS the body under test (the tool defines no own override).
    """
    tool = WikiEmitAnswerTool(session_id="sess-qa-1")
    assert tool.terminal_reason() == "completed"


def test_emit_answer_terminate_then_reason_matches_loop_selector(qa_setup):
    """Drive the real ``should_terminate_run → terminal_reason`` selector contract.

    Mirrors the ``tool_use_loop`` step: before any call the tool does not
    request termination; the one atomic call — a valid array ending in the
    accept-state ``sources`` block — flips ``should_terminate_run()`` True and
    ``terminal_reason()`` resolves to ``"completed"`` WITHOUT raising — the
    exact pair the loop reads at the terminating-tool seam.
    """
    store, sid = qa_setup
    tool = WikiEmitAnswerTool(session_id=sid)
    assert tool.should_terminate_run() is False  # before any call

    step = MagicMock(tool_input={"blocks": [
        {"kind": "p", "text": "Hi."},
        {"kind": "sources", "items": ["src/main.py"]},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        asyncio.run(tool.handle(step))
    assert tool.should_terminate_run() is True
    assert tool.terminal_reason() == "completed"


# ---------------------------------------------------------------------------
# Required-terminal declaration — the completion seam's gate contract
# ---------------------------------------------------------------------------


def test_emit_answer_declares_required_terminal():
    """``WikiEmitAnswerTool`` opts into the completion seam's required-terminal gate."""
    tool = WikiEmitAnswerTool(session_id="sess-qa-1")
    assert tool.required_terminal is True


def test_emit_answer_terminal_satisfied_tracks_should_terminate_run(qa_setup):
    """``terminal_satisfied()`` reads the SAME success flag as ``should_terminate_run``.

    Before any call neither is satisfied; the one atomic accept-state call
    flips BOTH together — they must never disagree for the same call, or the
    completion-seam gate could nudge a run that already delivered its answer.
    """
    store, sid = qa_setup
    tool = WikiEmitAnswerTool(session_id=sid)
    assert tool.terminal_satisfied() is False  # before any call

    step = MagicMock(tool_input={"blocks": [
        {"kind": "p", "text": "Hi."},
        {"kind": "sources", "items": ["src/main.py"]},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        asyncio.run(tool.handle(step))
    assert tool.terminal_satisfied() is True
    assert tool.terminal_satisfied() == tool.should_terminate_run()


def test_emit_answer_terminal_satisfied_stays_false_on_rejected_call(qa_setup):
    """A rejected call (validation error) never flips ``terminal_satisfied``."""
    store, sid = qa_setup
    tool = WikiEmitAnswerTool(session_id=sid)
    step = MagicMock(tool_input={"blocks": [
        {"kind": "not_a_real_kind", "text": "x"},
        {"kind": "sources", "items": ["src/main.py"]},
    ]})
    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=_runtime(store)):
        asyncio.run(tool.handle(step))
    assert tool.terminal_satisfied() is False
