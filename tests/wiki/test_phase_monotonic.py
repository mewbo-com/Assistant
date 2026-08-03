"""A resumed index must never walk its progress bar backwards.

A resume is told to re-clone and re-scan so the source is on disk before pages
are written. Those tools call ``emit_phase`` unconditionally, so a job that had
already built its graph re-stamped ``clone`` and then ``scan`` — with a
``phase_started_at`` LATER than the graph build's. Both progress surfaces (the
landing card polling the snapshot, the indexing page folding the event stream)
read that phase, so the bar visibly regressed mid-resume.
"""
from __future__ import annotations

from pathlib import Path

from mewbo_graph.plugins.wiki._ctx import WikiJobCtx, emit_phase
from mewbo_graph.wiki.resume import ResumePlan
from mewbo_graph.wiki.types import IndexingJob


def _store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path)


def _job(phase: str | None = None, job_id: str = "job-phase") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug="org/repo",
        status="scanning",
        scanned_count=0,
        total_count=5,
        current_file=None,
        phase=phase,
    )


def _ctx(store, tmp_path: Path, plan: ResumePlan | None = None) -> WikiJobCtx:
    return WikiJobCtx(
        job_id="job-phase",
        slug="org/repo",
        session_id="sess-phase",
        clone_dir=tmp_path / "clone",
        store=store,
        resume_plan=plan,
    )


# ── the model's own question ───────────────────────────────────────────────────


class TestRegressesTo:
    def test_backward_is_a_regression(self):
        assert _job("graph").regresses_to("scan") is True
        assert _job("finalize").regresses_to("clone") is True

    def test_forward_is_not(self):
        assert _job("scan").regresses_to("graph") is False
        assert _job("clone").regresses_to("finalize") is False

    def test_same_phase_is_not(self):
        # Re-entering the phase you are already in is a no-op, not a regression;
        # ``emit_phase_once`` owns that separate question.
        assert _job("graph").regresses_to("graph") is False

    def test_unset_phase_is_not(self):
        assert _job(None).regresses_to("clone") is False

    def test_unknown_name_is_never_a_regression(self):
        # An unrecognised phase belongs to a vocabulary this model does not
        # know; swallowing its transition would hide real progress.
        assert _job("graph").regresses_to("not-a-phase") is False
        assert _job("graph").regresses_to("") is False


# ── the seam both surfaces read ────────────────────────────────────────────────


class TestEmitPhaseIsMonotonic:
    def test_backward_move_updates_neither_surface(self, tmp_path: Path):
        store = _store(tmp_path)
        store.create_job(_job("graph"))
        emit_phase(_ctx(store, tmp_path), "scan")

        assert store.get_job("job-phase").phase == "graph"
        events = store.load_job_events("job-phase")
        assert [e for e in events if e.get("type") == "phase"] == [], (
            "suppressing only the snapshot would let the two surfaces disagree"
        )

    def test_forward_move_updates_both_surfaces(self, tmp_path: Path):
        store = _store(tmp_path)
        store.create_job(_job("scan"))
        emit_phase(_ctx(store, tmp_path), "graph")

        job = store.get_job("job-phase")
        assert job.phase == "graph"
        assert job.phase_started_at
        phases = [e for e in store.load_job_events("job-phase") if e.get("type") == "phase"]
        assert [e["name"] for e in phases] == ["graph"]

    def test_first_phase_of_a_fresh_job_is_recorded(self, tmp_path: Path):
        store = _store(tmp_path)
        store.create_job(_job(None))
        emit_phase(_ctx(store, tmp_path), "clone")
        assert store.get_job("job-phase").phase == "clone"

    def test_restart_may_return_to_the_top(self, tmp_path: Path):
        # A restart really is redoing the pipeline from the beginning, so its
        # phase must be allowed back to ``clone`` — otherwise the bar would sit
        # at ``finalize`` while the repo is being re-cloned.
        store = _store(tmp_path)
        store.create_job(_job("finalize"))
        emit_phase(_ctx(store, tmp_path, ResumePlan.for_restart()), "clone")
        assert store.get_job("job-phase").phase == "clone"

    def test_resume_plan_that_is_not_a_restart_still_blocks(self, tmp_path: Path):
        store = _store(tmp_path)
        store.create_job(_job("graph"))
        plan = ResumePlan(skip=frozenset({"graph"}), node_count=24769)
        emit_phase(_ctx(store, tmp_path, plan), "scan")
        assert store.get_job("job-phase").phase == "graph"
