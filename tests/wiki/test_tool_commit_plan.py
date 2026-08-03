"""Tests for WikiCommitPlanTool — TDD: tests written first."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob

# ── Helpers ────────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _job(job_id: str = "job-cp", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="scanning",
        scanned_count=5,
        total_count=10,
        current_file=None,
    )


def _fake_runtime(store: JsonWikiStore) -> SimpleNamespace:
    return SimpleNamespace(wiki_store=store)


def _make_action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _two_page_plan() -> list[dict]:
    return [
        {"id": "overview", "title": "Overview", "description": "High-level overview"},
        {"id": "architecture", "title": "Architecture", "description": "System design"},
    ]


# ── Test 1: plan persisted + finalizing event emitted ─────────────────────────


def test_commit_plan_persists_plan_and_emits_finalizing(tmp_path: Path) -> None:
    """A 2-page plan is saved; a finalizing event with correct counts is emitted."""
    import mewbo_graph.plugins.wiki.commit_plan as mod
    from mewbo_graph.plugins.wiki.commit_plan import WikiCommitPlanTool

    store = _store(tmp_path)
    job = _job("job-cp1", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-cp1", "sess-cp1")

    runtime = _fake_runtime(store)
    tool = WikiCommitPlanTool(session_id="sess-cp1")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"pages": _two_page_plan()})))

    # No error in result
    assert "error" not in result.content
    assert "committed" in result.content

    # Finalizing event emitted
    events = store.load_job_events("job-cp1")
    finalizing = [e for e in events if e["type"] == "finalizing"]
    assert len(finalizing) == 1
    ev = finalizing[0]
    assert ev["scannedCount"] == job.scanned_count
    assert ev["totalCount"] == job.total_count

    # Plan is persisted and retrievable
    saved = store.get_job_plan("job-cp1")
    assert saved is not None
    assert len(saved) == 2
    assert saved[0]["id"] == "overview"
    assert saved[1]["id"] == "architecture"


# ── Test 2: empty pages list → validation error, no event ─────────────────────


def test_commit_plan_empty_returns_validation_error(tmp_path: Path) -> None:
    """Empty pages list → validation error; no event is appended."""
    import mewbo_graph.plugins.wiki.commit_plan as mod
    from mewbo_graph.plugins.wiki.commit_plan import WikiCommitPlanTool

    store = _store(tmp_path)
    job = _job("job-cp2", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-cp2", "sess-cp2")

    runtime = _fake_runtime(store)
    tool = WikiCommitPlanTool(session_id="sess-cp2")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"pages": []})))

    assert "validation" in result.content

    # No events appended
    events = store.load_job_events("job-cp2")
    assert len(events) == 0


# ── Test 3: second call overwrites the first plan ─────────────────────────────


def test_commit_plan_overwrites_previous_plan(tmp_path: Path) -> None:
    """Calling commit_plan twice: the second call's plan wins."""
    import mewbo_graph.plugins.wiki.commit_plan as mod
    from mewbo_graph.plugins.wiki.commit_plan import WikiCommitPlanTool

    store = _store(tmp_path)
    job = _job("job-cp3", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-cp3", "sess-cp3")

    runtime = _fake_runtime(store)
    tool = WikiCommitPlanTool(session_id="sess-cp3")

    first_plan = [{"id": "intro", "title": "Intro", "description": ""}]
    second_plan = [
        {"id": "alpha", "title": "Alpha", "description": ""},
        {"id": "beta", "title": "Beta", "description": ""},
        {"id": "gamma", "title": "Gamma", "description": ""},
    ]

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(tool.handle(_make_action_step({"pages": first_plan})))
        asyncio.run(tool.handle(_make_action_step({"pages": second_plan})))

    saved = store.get_job_plan("job-cp3")
    assert saved is not None
    assert len(saved) == 3
    assert saved[0]["id"] == "alpha"


# ── The terminal precondition, decided at plan time ───────────────────────────


def test_commit_plan_refuses_a_landing_page_it_does_not_contain(tmp_path: Path) -> None:
    """Decided at plan time, not ~25 minutes later at wiki_finalize.

    ``landingPageId 'X' not found in submitted pages`` is decidable the instant
    the plan is committed — the plan IS the set of ids that will exist — so
    deferring it makes a doomed run pay the whole pages fan-out first.
    """
    import mewbo_graph.plugins.wiki.commit_plan as mod
    from mewbo_graph.plugins.wiki.commit_plan import WikiCommitPlanTool

    store = _store(tmp_path)
    store.create_job(_job("job-land", "org/repo"))
    store.attach_job_session("job-land", "sess-land")

    tool = WikiCommitPlanTool(session_id="sess-land")
    with patch.object(mod, "_resolve_runtime", return_value=_fake_runtime(store)):
        result = asyncio.run(tool.handle(_make_action_step(
            {"pages": _two_page_plan(), "landingPageId": "index"}
        )))

    assert "validation" in result.content
    assert "index" in result.content
    # Nothing was committed, so the run has not been left half-planned.
    assert store.get_job_plan("job-land") is None


def test_commit_plan_records_a_valid_landing_page_on_the_job(tmp_path: Path) -> None:
    """A landing page that IS in the plan is accepted and pinned to the job."""
    import mewbo_graph.plugins.wiki.commit_plan as mod
    from mewbo_graph.plugins.wiki.commit_plan import WikiCommitPlanTool

    store = _store(tmp_path)
    store.create_job(_job("job-ok", "org/repo"))
    store.attach_job_session("job-ok", "sess-ok")

    tool = WikiCommitPlanTool(session_id="sess-ok")
    with patch.object(mod, "_resolve_runtime", return_value=_fake_runtime(store)):
        result = asyncio.run(tool.handle(_make_action_step(
            {"pages": _two_page_plan(), "landingPageId": "overview"}
        )))

    assert "error" not in result.content
    job = store.get_job("job-ok")
    assert job is not None
    assert job.landing_page_id == "overview"


def test_commit_plan_still_accepts_a_plan_with_no_landing_page(tmp_path: Path) -> None:
    """The field is optional: an indexer that names its landing page only at
    finalize keeps working exactly as before."""
    import mewbo_graph.plugins.wiki.commit_plan as mod
    from mewbo_graph.plugins.wiki.commit_plan import WikiCommitPlanTool

    store = _store(tmp_path)
    store.create_job(_job("job-none", "org/repo"))
    store.attach_job_session("job-none", "sess-none")

    tool = WikiCommitPlanTool(session_id="sess-none")
    with patch.object(mod, "_resolve_runtime", return_value=_fake_runtime(store)):
        result = asyncio.run(
            tool.handle(_make_action_step({"pages": _two_page_plan()}))
        )

    assert "error" not in result.content
    job = store.get_job("job-none")
    assert job is not None and job.landing_page_id is None


def test_commit_plan_refuses_duplicate_page_ids(tmp_path: Path) -> None:
    """A duplicated id makes the progress denominator unreachable.

    ``total_pages`` counts the duplicate but a page id can only be written — and
    counted — once, so the bar stops short of its own total for the rest of the
    run.
    """
    import mewbo_graph.plugins.wiki.commit_plan as mod
    from mewbo_graph.plugins.wiki.commit_plan import WikiCommitPlanTool

    store = _store(tmp_path)
    store.create_job(_job("job-dup", "org/repo"))
    store.attach_job_session("job-dup", "sess-dup")

    tool = WikiCommitPlanTool(session_id="sess-dup")
    with patch.object(mod, "_resolve_runtime", return_value=_fake_runtime(store)):
        result = asyncio.run(tool.handle(_make_action_step(
            {"pages": [*_two_page_plan(), {"id": "overview", "title": "Overview again"}]}
        )))

    assert "validation" in result.content
    assert "overview" in result.content
    assert store.get_job_plan("job-dup") is None
