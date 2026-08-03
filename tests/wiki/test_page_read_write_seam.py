"""ONE session must be able to both READ and WRITE wiki pages.

An agent asked to decide *which* pages a change made stale has to read them
before it can decide anything, and then write the ones it chose. Those two
halves resolve through different context resolvers — the page read tools through
:func:`resolve_qa_ctx`, ``wiki_submit_page`` through :func:`resolve_job_ctx` —
and no single session could satisfy both: a job-bound session had no QA answer
and no workspace, so every read refused, while a QA/structured session had no
job, so every write refused.

The fix is one extra tier in the resolver ladder that already had three, keyed on
the slug a job already knows. These tests assert the property from BOTH sides for
the SAME session id, which is the only shape that can fail if either half
regresses — asserting one resolver alone would stay green while the pair stayed
unusable.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx, resolve_qa_ctx
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import Frontmatter, IndexingJob, WikiPage

SLUG = "git.example.com/acme/beacon"
SESSION = "sess-act-1"
JOB = "job-act-1"


@pytest.fixture()
def store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def job_session(store: JsonWikiStore) -> SimpleNamespace:
    """A session bound to an indexing job, with one page already published."""
    store.create_job(IndexingJob(
        jobId=JOB,
        slug=SLUG,
        status="scanning",
        scannedCount=0,
        totalCount=0,
        currentFile=None,
        model="anthropic/claude-sonnet-4-6",
    ))
    store.attach_job_session(JOB, SESSION)
    store.save_page(SLUG, WikiPage(
        id="auth",
        title="Auth",
        frontmatter=Frontmatter(title="Auth", slug="auth"),
        body="# Auth\n\nold prose",
        toc=[],
        nav=[],
    ))
    return SimpleNamespace(wiki_store=store)


def test_a_job_bound_session_resolves_a_read_context(job_session) -> None:
    """The half that did not exist: reads grounded on the job's own slug."""
    ctx = resolve_qa_ctx(SESSION, job_session)

    assert ctx is not None
    assert ctx.slug == SLUG
    # Slug-only, shape-identical to the workspace and project tiers, so every
    # tool already guarding on ``answer_id`` stays correct here.
    assert ctx.answer_id is None


def test_the_same_session_still_resolves_a_write_context(job_session) -> None:
    """The other half, unchanged — asserted on the SAME session id.

    Read and write resolving for one session is the whole property; either
    assertion alone passes in a world where the pair is still unusable.
    """
    ctx = resolve_job_ctx(SESSION, job_session)

    assert ctx is not None
    assert ctx.job_id == JOB
    assert ctx.slug == SLUG


def test_the_session_can_actually_list_pages(job_session) -> None:
    """Through the real tool, not just the resolver."""
    import mewbo_graph.plugins.wiki.list_pages as lp_mod
    from mewbo_graph.plugins.wiki.list_pages import WikiListPagesTool

    step = MagicMock()
    step.tool_input = {}
    tool = WikiListPagesTool(session_id=SESSION)
    with patch.object(lp_mod, "_resolve_runtime", return_value=job_session):
        result = asyncio.run(tool.handle(step))

    payload = ast.literal_eval(result.content)
    assert payload["count"] == 1
    assert payload["pages"][0]["pageId"] == "auth"


def test_a_session_bound_to_nothing_still_resolves_no_read_context(store) -> None:
    """The tier must not ground a session that has no wiki at all.

    Grounding on nothing would be worse than refusing: a tool would answer about
    some other project's pages rather than reporting that it is not bound.
    """
    assert resolve_qa_ctx("sess-unbound", SimpleNamespace(wiki_store=store)) is None
