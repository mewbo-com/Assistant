"""ResumePlan.build — checkpoint detection from real store artifacts."""
from __future__ import annotations

import pytest
from mewbo_graph.wiki.resume import ResumePlan
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    Frontmatter,
    IndexingJob,
    WikiPage,
    make_graph_node,
)


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _job(store, *, job_id="j1", slug="org/repo", status="interrupted"):
    job = IndexingJob(
        jobId=job_id, slug=slug, status=status,
        scannedCount=0, totalCount=0, currentFile=None,
    )
    store.create_job(job)
    return job


def _node(slug, nid, name="f"):
    return make_graph_node(
        slug=slug, node_id=nid, type="Function", name=name, file="a.py", range=(0, 1)
    )


def _page(pid):
    return WikiPage(
        id=pid, title=pid, frontmatter=Frontmatter(title=pid, slug=pid),
        body="x", toc=[], nav=[],
    )


def _plan(*ids):
    return [{"id": pid, "title": pid} for pid in ids]


def _write_page(store, job_id, pid, *, slug="org/repo"):
    """Persist a page the way ``wiki_submit_page`` does — page + job claim.

    The claim is not bookkeeping on the side: it is what records WHICH job wrote
    the page, and it is the only thing that distinguishes this run's output from
    a page some earlier index of the same repository left in the store.
    """
    store.save_page(slug, _page(pid))
    store.claim_job_page(slug, job_id, pid)


def test_empty_graph_forces_full_rebuild(store):
    """No graph / no plan / no pages → empty skip set (resume == rebuild)."""
    job = _job(store)
    plan = ResumePlan.build(store, job)
    assert plan.skip == frozenset()
    assert plan.pages_done == frozenset()
    assert plan.pages_remaining == ()
    assert plan.is_noop()
    assert not plan.should_skip("graph")


def test_populated_graph_skips_graph(store):
    job = _job(store)
    store.upsert_nodes("org/repo", [_node("org/repo", "n1"), _node("org/repo", "n2")])
    plan = ResumePlan.build(store, job)
    assert plan.should_skip("graph")
    assert plan.node_count == 2
    assert not plan.should_skip("enrich")  # no entities yet


def test_committed_plan_skips_plan(store):
    job = _job(store)
    store.save_job_plan("j1", _plan("overview", "arch"))
    plan = ResumePlan.build(store, job)
    assert plan.should_skip("plan")
    assert plan.total_pages == 2
    # No pages persisted yet → both remaining.
    assert plan.pages_done == frozenset()
    assert plan.pages_remaining == ("overview", "arch")


def test_pages_done_and_remaining_computed_from_plan_and_store(store):
    job = _job(store)
    store.save_job_plan("j1", _plan("a", "b", "c"))
    _write_page(store, "j1", "a")
    _write_page(store, "j1", "c")
    plan = ResumePlan.build(store, job)
    assert plan.pages_done == frozenset({"a", "c"})
    # Order preserved from the plan; only the missing one remains.
    assert plan.pages_remaining == ("b",)


def _legacy_meta(store, job_id, submitted):
    """Write the PRE-CLAIM job meta shape: a bare counter, no id list.

    A job created by current code always carries ``submitted_page_ids``, so no
    test that builds its fixture through ``claim_job_page`` can reach the
    compat path. The only way to exercise it is to write the old shape by hand.
    """
    import json

    path = store._job_meta_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"submitted_pages": submitted}), encoding="utf-8")


def test_a_job_interrupted_before_claims_existed_still_resumes(store):
    """The upgrade case: an in-flight job whose meta has no claim record.

    Reading the claim alone reported NOTHING done, so the resume regenerated
    every page the job had already written correctly — one page-writer
    generation each, which is the exact waste this branch exists to remove. Page
    attribution predates the claim record and can answer for such a job.
    """
    job = _job(store, job_id="j-legacy")
    ids = [f"p{i}" for i in range(5)]
    store.save_job_plan("j-legacy", _plan(*ids))
    for pid in ids[:3]:
        store.save_page("org/repo", _page(pid), job_id="j-legacy")
    _legacy_meta(store, "j-legacy", submitted=3)

    plan = ResumePlan.build(store, job)
    assert plan.pages_done == frozenset({"p0", "p1", "p2"})
    assert plan.pages_remaining == ("p3", "p4")


def test_a_legacy_job_keeps_counting_from_what_it_already_wrote(store):
    """...and its counter continues, rather than restarting from one.

    The count is the SIZE of the written set, so a job that resumes and
    writes its two remaining pages finishes at 5 of 5 — not 2 (a restart from
    zero) and not 8 (the old free-running increment continuing from 3).
    """
    _job(store, job_id="j-count")
    ids = [f"p{i}" for i in range(5)]
    store.save_job_plan("j-count", _plan(*ids))
    for pid in ids[:3]:
        store.save_page("org/repo", _page(pid), job_id="j-count")
    _legacy_meta(store, "j-count", submitted=3)

    counts = [store.claim_job_page("org/repo", "j-count", pid).count for pid in ids[3:]]
    assert counts == [4, 5]
    assert store.get_job_submitted_count("j-count") == 5


def test_re_writing_every_page_of_a_legacy_job_cannot_overshoot_the_plan(store):
    """The user-visible symptom was ``90/50`` on a 50-page plan.

    A free-running counter carried the interrupted attempt's 40 and then added
    one per re-written page. Counting distinct pages instead makes the overshoot
    structurally impossible: re-writing all five here ends at five.
    """
    _job(store, job_id="j-over")
    ids = [f"p{i}" for i in range(5)]
    store.save_job_plan("j-over", _plan(*ids))
    for pid in ids[:3]:
        store.save_page("org/repo", _page(pid), job_id="j-over")
    _legacy_meta(store, "j-over", submitted=3)

    for pid in ids:
        store.claim_job_page("org/repo", "j-over", pid)
    assert store.get_job_submitted_count("j-over") == 5


def test_a_page_from_an_earlier_index_is_not_this_run_s_work(store):
    """The defect: ``pages_done`` was the slug's whole page list.

    A slug accumulates the union of every index ever run against it, so on a
    re-index every page an EARLIER job wrote — at a different commit, from a
    different plan — read as already done, and the resume skipped regenerating
    it. The run then shipped a wiki mixing two commits' documentation while
    reporting itself complete.
    """
    old = _job(store, job_id="j-old")
    fresh = _job(store, job_id="j-new")
    store.save_job_plan(old.job_id, _plan("a", "b"))
    store.save_job_plan(fresh.job_id, _plan("a", "b"))
    # The previous index wrote both pages; the pages ARE in the store for this slug.
    _write_page(store, old.job_id, "a")
    _write_page(store, old.job_id, "b")

    assert ResumePlan.build(store, old).pages_done == frozenset({"a", "b"})
    # ... and none of it counts as work the NEW job has done.
    plan = ResumePlan.build(store, fresh)
    assert plan.pages_done == frozenset()
    assert plan.pages_remaining == ("a", "b")


def test_the_6_of_7_interrupted_at_pages_scenario(store):
    """The exact failure: interrupted at ``pages`` with graph populated +
    6/7 plan pages written → skip graph/enrich/plan; only the 1 missing page
    remains; finalize follows."""
    job = _job(store, status="interrupted")
    # graph built ...
    store.upsert_nodes("org/repo", [_node("org/repo", f"n{i}") for i in range(5)])
    # ... entities minted (enrich done) ...
    from mewbo_graph.entities.types import Entity
    store.upsert_entities("org/repo", [Entity(name="Widget", type="concept")])
    # ... plan committed (7 pages) ...
    ids = [f"p{i}" for i in range(7)]
    store.save_job_plan("j1", _plan(*ids))
    # ... 6 of 7 pages written (p3 missing).
    for pid in ids:
        if pid != "p3":
            _write_page(store, "j1", pid)

    plan = ResumePlan.build(store, job)
    assert plan.should_skip("graph")
    assert plan.should_skip("enrich")
    assert plan.should_skip("plan")
    assert plan.pages_remaining == ("p3",)
    assert len(plan.pages_done) == 6
    assert not plan.is_noop()
    # Summary tells the agent to reuse + write only the remaining page.
    summary = plan.summary()
    assert "p3" in summary
    assert "SKIP wiki_build_graph" in summary
    assert "SKIP wiki_commit_plan" in summary


def test_persisted_roundtrip_is_cheap_rebuild(store):
    """to_persisted → from_persisted reconstructs an equivalent plan (the
    per-tool-call cheap path that avoids a graph re-query)."""
    job = _job(store)
    store.upsert_nodes("org/repo", [_node("org/repo", "n1")])
    store.save_job_plan("j1", _plan("a", "b"))
    _write_page(store, "j1", "a")
    built = ResumePlan.build(store, job)

    data = built.to_persisted()
    rebuilt = ResumePlan.from_persisted(data)
    assert rebuilt is not None
    assert rebuilt.skip == built.skip
    assert rebuilt.pages_done == built.pages_done
    assert rebuilt.pages_remaining == built.pages_remaining
    assert rebuilt.node_count == built.node_count
    assert rebuilt.total_pages == built.total_pages


def test_from_persisted_none_and_empty_yield_none():
    assert ResumePlan.from_persisted(None) is None
    assert ResumePlan.from_persisted({}) is None
