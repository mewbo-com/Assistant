"""A rewritten page's ``DocPageNote`` is re-derived from the body just written.

The defect these cover needs TWO refreshes with a page rewrite between them, and
that is exactly why it shipped: a single refresh scores every note from scratch,
so it looks correct no matter how stale the note's own anchors are. Only the
NEXT refresh reads the anchors the page-writer should have replaced.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.wiki.graph import GraphParseResult
from mewbo_graph.wiki.memory_types import FileManifest
from mewbo_graph.wiki.refresh import (
    DocStalenessPlanner,
    GraphDelta,
    GraphDeltaIndexer,
    RefreshOrchestrator,
)
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    Frontmatter,
    GraphEdge,
    IndexingJob,
    SourceRef,
    WikiPage,
    make_graph_node,
)

from .conftest import FakeParser

SLUG = "org/repo"


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


# ── fixtures shared by the refresh legs ─────────────────────────────────────


def _node(nid, typ, name, f):
    return make_graph_node(slug=SLUG, node_id=nid, type=typ, name=name, file=f, range=(0, 9))


def _page(page_id, sources, body="# body"):
    return WikiPage(
        id=page_id,
        title=page_id.title(),
        frontmatter=Frontmatter(
            title=page_id.title(),
            slug=page_id,
            relevantSources=[SourceRef(path=p) for p in sources],
        ),
        body=body,
        toc=[],
        nav=[],
    )


def _reparse(file: str, suffix: str) -> GraphParseResult:
    """One file's re-parse, distinct per pass so the delta is non-empty."""
    fid, nid = f"f-{suffix}", f"n-{suffix}"
    return GraphParseResult(
        nodes=[
            _node(fid, "File", file, file),
            _node(nid, "Function", "verify", file),
        ],
        edges=[GraphEdge(slug=SLUG, source=fid, target=nid, type="CONTAINS")],
        skipped=[],
    )


def _seed(store, *, anchors: list[str]) -> None:
    """One page anchored to ``anchors``; a manifest so the first diff is real."""
    store.upsert_file_manifest(
        SLUG,
        [FileManifest(slug=SLUG, path="auth.py", content_hash="stale", entity_keys=[])],
    )
    store.save_page(SLUG, _page("auth", anchors, body="# original"))


def _refresh(store, root: Path, *, file: str, content: str, commit: str):
    """Write *file*, then run the real orchestrator over the working tree."""
    path = root / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    orch = RefreshOrchestrator(
        store=store,
        graph_indexer=GraphDeltaIndexer(
            store, parser=FakeParser({file: _reparse(file, commit)})
        ),
        clock=lambda: "2026-06-05T12:00:00Z",
    )
    files = [p for p in root.rglob("*") if p.is_file()]
    return orch.refresh(SLUG, root, files, commit=commit)


# ── the act phase, driven through the real page-writer tool ─────────────────


def _submit_page(store, *, page_id: str, sources: list[str], body: str, commit: str):
    """Rewrite a page through the ONE production page writer."""
    import mewbo_graph.plugins.wiki.submit_page as mod
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    job_id = f"job-{commit}"
    store.create_job(
        IndexingJob(
            job_id=job_id,
            slug=SLUG,
            status="finalizing",
            scanned_count=1,
            total_count=1,
            current_file=None,
            commitSha=commit,
        )
    )
    store.attach_job_session(job_id, f"sess-{commit}")
    step = MagicMock()
    step.tool_input = {
        "pageId": page_id,
        "frontmatter": {
            "title": page_id.title(),
            "slug": page_id,
            "relevantSources": [{"path": p} for p in sources],
        },
        "body": body,
    }
    tool = WikiSubmitPageTool(session_id=f"sess-{commit}")
    with patch.object(mod, "_resolve_runtime", return_value=SimpleNamespace(wiki_store=store)):
        return asyncio.run(tool.handle(step))


# ── the acceptance case: two refreshes with a rewrite between them ──────────


def test_a_rewritten_page_is_scored_against_its_NEW_anchors(store, tmp_path) -> None:
    """The loop the act phase would otherwise run forever.

    Refresh 1 flags the page; the act rewrites it, and the rewrite legitimately
    re-points the page at where the code now lives. Unless the note is re-derived
    from the frontmatter actually submitted, refresh 2 keeps scoring the page
    against the file it stopped documenting — reaching the same verdict, paying
    for the same rewrite, on every refresh from then on.

    A version of this test whose rewrite keeps the SAME anchors cannot fail on
    the defect: ``_assess`` re-scores from scratch each pass, so the verdict
    returns to ``keep`` on its own the moment those anchors leave Δ. The stale
    ANCHOR SET is what survives a re-score, which is why the rewrite here moves
    the page's sources.
    """
    root = tmp_path / "clone"
    _seed(store, anchors=["auth.py"])

    first = _refresh(store, root, file="auth.py", content="v1", commit="c2")
    assert "auth" in first.pages_to_regenerate

    # The act phase rewrites the page; the code it documents has moved.
    _submit_page(
        store,
        page_id="auth",
        sources=["core/auth.py"],
        body="# rewritten\n\nNow documents core/auth.py.",
        commit="c2",
    )

    second = _refresh(store, root, file="auth.py", content="v2", commit="c3")

    assert "auth" not in second.pages_to_regenerate
    note = store.get_doc_note(SLUG, "auth")
    assert note.anchor_keys == ["core/auth.py"]
    assert note.generation_policy == "keep"


# ── the page-writer's own contract ──────────────────────────────────────────


def test_submitting_a_page_re_derives_its_note_from_the_submitted_body(
    store, tmp_path
) -> None:
    """Hash, anchors, commit and the four staleness fields all come from the write."""
    _seed(store, anchors=["auth.py"])
    DocStalenessPlanner(store=store).plan(
        SLUG,
        GraphDelta(
            added_keys=frozenset(),
            modified_keys=frozenset({"auth.py"}),
            removed_keys=frozenset(),
            affected=frozenset({"auth.py"}),
            early_cutoff_files=(),
        ),
        commit="c2",
    )
    flagged = store.get_doc_note(SLUG, "auth")
    assert flagged.generation_policy == "regenerate"  # the state the act acts on

    body = "# rewritten body"
    _submit_page(
        store, page_id="auth", sources=["auth.py", "session.py"], body=body, commit="c9"
    )

    note = store.get_doc_note(SLUG, "auth")
    assert note.content_hash == DocStalenessPlanner._hash(body)
    assert note.anchor_keys == ["auth.py", "session.py"]
    assert note.last_indexed_commit == "c9"
    assert note.staleness_score == 0.0
    assert note.staleness_reason == "clean"
    assert note.generation_policy == "keep"
    assert note.stale_anchor_keys == []
    assert note.deleted_anchor_keys == []


def test_a_page_written_before_any_note_exists_gets_one(store, tmp_path) -> None:
    """The first index writes pages with no notes yet — it must not skip them."""
    _submit_page(
        store, page_id="intro", sources=["intro.py"], body="# intro", commit="c1"
    )

    note = store.get_doc_note(SLUG, "intro")
    assert note is not None
    assert note.anchor_keys == ["intro.py"]
    assert note.last_indexed_commit == "c1"


def test_a_doc_note_failure_never_fails_the_page_write(store, tmp_path) -> None:
    """The page is the deliverable; the note is bookkeeping about it."""
    import mewbo_graph.wiki.refresh as refresh_mod

    def _boom(self, slug, page, *, commit):
        raise RuntimeError("doc note store unavailable")

    with patch.object(refresh_mod.DocStalenessPlanner, "restamp_page", _boom):
        result = _submit_page(
            store, page_id="intro", sources=["intro.py"], body="# intro", commit="c1"
        )

    assert "error" not in result.content
    page = store.get_page(SLUG, "intro")
    assert page is not None and page.body == "# intro"
    assert store.get_doc_note(SLUG, "intro") is None


# ── the commit stamp ────────────────────────────────────────────────────────


def test_the_planner_stamps_the_commit_on_created_AND_existing_notes(store) -> None:
    """Writing it only where notes are CREATED leaves it null forever.

    ``migrate`` is create-only, so every project indexed before this field was
    written would keep a null commit for the life of the project unless the
    re-assessment path stamps it too.
    """
    _seed(store, anchors=["auth.py"])
    planner = DocStalenessPlanner(store=store)
    delta = GraphDelta(
        added_keys=frozenset(),
        modified_keys=frozenset({"other.py"}),
        removed_keys=frozenset(),
        affected=frozenset({"other.py"}),
        early_cutoff_files=(),
    )

    planner.plan(SLUG, delta, commit="c2")
    assert store.get_doc_note(SLUG, "auth").last_indexed_commit == "c2"

    # A second pass over the SAME (already-migrated) note advances the stamp.
    planner.plan(SLUG, delta, commit="c3")
    assert store.get_doc_note(SLUG, "auth").last_indexed_commit == "c3"
