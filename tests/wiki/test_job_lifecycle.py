"""One home for the four lifecycle questions asked of an indexing job.

Four consumers ask about ``interrupted`` — the active-jobs route, restart
recovery, the session-end reconciler, the resume façade — plus a fifth in the
console. They are four GENUINELY different questions and must stay four sets;
what must not happen is each consumer owning a private literal copy, because
then a status is re-classified in one place and silently disagrees everywhere
else.

These tests pin, in order:

1. the membership matrix of the four predicates on :class:`IndexingJob`;
2. that each consumer really asks the model rather than re-deriving the answer;
3. ``isActive`` on the one ``_job_wire`` seam;
4. the termination cascade — nothing propagated session death into a job, so a
   terminated session left its job non-terminal forever;
5. a real hook manager reaching all four indexing start paths (three of them
   passed ``None``, which ``Orchestrator`` turns into a FRESH empty manager, so
   ``WikiIndexingSessionEndHook`` never fired for those runs).

Only I/O boundaries are stubbed: the runtime for the route tests, and nothing
at all for the cascade (real ``SessionRuntime`` + real stores on ``tmp_path``).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mewbo_graph.wiki.types import IndexingJob, IndexingStatus, RefreshDecision

API_KEY = "test-lifecycle-key"

ALL_STATUSES: tuple[IndexingStatus, ...] = (
    "queued",
    "scanning",
    "finalizing",
    "interrupted",
    "complete",
    "cancelled",
    "failed",
)

# The membership each consumer carried BEFORE the predicates existed. This table
# is the refactor's contract: moving where the knowledge lives must not move a
# single status between sets.
#
#   is_active      — routes.list_active_jobs ACTIVE
#   is_recoverable — JobRecovery._RECOVERABLE
#   is_terminal    — WikiIndexingSessionEndHook._TERMINAL
#   is_resumable   — NOT in WikiResume._NON_RESUMABLE
EXPECTED: dict[str, dict[str, bool]] = {
    "queued": {"active": True, "recoverable": True, "terminal": False, "resumable": True},
    "scanning": {"active": True, "recoverable": True, "terminal": False, "resumable": True},
    "finalizing": {"active": True, "recoverable": True, "terminal": False, "resumable": True},
    "interrupted": {"active": True, "recoverable": True, "terminal": False, "resumable": True},
    "complete": {"active": False, "recoverable": False, "terminal": True, "resumable": False},
    "cancelled": {"active": False, "recoverable": False, "terminal": True, "resumable": False},
    "failed": {"active": False, "recoverable": False, "terminal": False, "resumable": True},
}


def _job(status: IndexingStatus, *, job_id: str = "job-1", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status=status,
        scanned_count=0,
        total_count=0,
        current_file=None,
    )


def _submission_dict(slug: str = "org/repo") -> dict:
    return {
        "repoUrl": f"https://github.com/{slug}",
        "slug": slug,
        "platform": "github",
        "depth": "comprehensive",
        "language": "en",
        "model": "anthropic/claude-sonnet-4-6",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    }


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki-lifecycle")


@pytest.fixture()
def runtime_stub(store):
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-lifecycle"
    rt.start_async.return_value = True
    rt.is_running.return_value = False
    return rt


@pytest.fixture()
def hook_manager():
    from mewbo_core.hooks import HookManager

    return HookManager()


@pytest.fixture()
def wiki_app(monkeypatch, store, runtime_stub, hook_manager):
    """Flask app with the wiki routes mounted against a REAL hook manager."""
    monkeypatch.setenv("MEWBO_MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask

    flask_app = Flask(__name__ + "_lifecycle")
    flask_app.config["TESTING"] = True
    routes_mod.register(flask_app, runtime_stub, hook_manager=hook_manager)

    yield flask_app, store, runtime_stub, hook_manager

    routes_mod._runtime = None
    routes_mod._hook_manager = None


@pytest.fixture()
def client(wiki_app):
    flask_app, store, runtime_stub, hm = wiki_app
    return flask_app.test_client(), store, runtime_stub, hm


def _h() -> dict:
    return {"X-Api-Key": API_KEY}


# ── 1. The membership matrix ───────────────────────────────────────────────────


class TestLifecyclePredicates:
    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_predicates_match_the_pre_refactor_membership(self, status) -> None:
        job = _job(status)
        want = EXPECTED[status]
        assert job.is_active is want["active"]
        assert job.is_recoverable is want["recoverable"]
        assert job.is_terminal is want["terminal"]
        assert job.is_resumable is want["resumable"]

    def test_every_status_is_classified(self) -> None:
        """A new status must be given an answer to all four questions."""
        from typing import get_args

        assert set(get_args(IndexingStatus)) == set(EXPECTED)

    def test_interrupted_is_the_reason_the_sets_stay_separate(self) -> None:
        """The status four consumers disagree over answers YES three times."""
        job = _job("interrupted")
        assert (job.is_active, job.is_recoverable, job.is_resumable) == (True, True, True)
        assert job.is_terminal is False

    def test_failed_is_recoverable_by_nobody_but_still_resumable(self) -> None:
        """``failed`` separates the two sets that otherwise look identical.

        It is NOT re-driven by restart recovery and NOT settled as far as the
        session-end reconciler is concerned, yet ``ResumePlan`` may still have
        real checkpoints to reuse — which is why a user-initiated resume of a
        failed job is allowed.
        """
        job = _job("failed")
        assert job.is_recoverable is False
        assert job.is_terminal is False
        assert job.is_resumable is True

    def test_predicates_are_not_persisted_fields(self) -> None:
        """A predicate must not leak into the model dump.

        ``IndexingJob`` is ``extra="forbid"`` and round-trips through the store,
        so a computed FIELD would make every persisted snapshot fail to
        re-validate. The derived flag reaches the wire through ``_job_wire``.
        """
        dumped = _job("scanning").model_dump(mode="json", by_alias=True)
        assert "isActive" not in dumped
        assert "is_active" not in dumped
        assert IndexingJob.model_validate(dumped).status == "scanning"


# ── 2. Each consumer asks the model ────────────────────────────────────────────


class TestConsumersAskTheModel:
    def test_active_jobs_route_serves_exactly_the_active_set(self, client) -> None:
        c, store, _rt, _hm = client
        for status in ALL_STATUSES:
            store.create_job(_job(status, job_id=f"j-{status}"))

        listed = c.get("/v1/wiki/jobs/active", headers=_h()).get_json()
        served = {row["jobId"] for row in listed}
        assert served == {f"j-{s}" for s in ALL_STATUSES if EXPECTED[s]["active"]}

    def test_recovery_re_drives_exactly_the_recoverable_set(
        self, store, runtime_stub, monkeypatch
    ) -> None:
        from mewbo_api.wiki import recovery as recovery_mod

        for status in ALL_STATUSES:
            # One slug per status so recovery's per-slug dedupe can't hide one.
            store.create_job(_job(status, job_id=f"j-{status}", slug=f"org/{status}"))

        seen: list[str] = []

        class _CaptureResume:
            @staticmethod
            def resume(store_, runtime_, job_id, **kwargs):
                seen.append(job_id)
                return {"job_id": job_id, "session_id": "s", "status": "scanning"}

        monkeypatch.setattr(recovery_mod, "WikiResume", _CaptureResume)
        recovery_mod.JobRecovery.recover_interrupted(store, runtime_stub)

        assert set(seen) == {f"j-{s}" for s in ALL_STATUSES if EXPECTED[s]["recoverable"]}

    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_session_end_hook_leaves_terminal_jobs_alone(
        self, store, runtime_stub, status
    ) -> None:
        from mewbo_api.wiki.jobs import WikiIndexingSessionEndHook

        store.create_job(_job(status, job_id="j-end"))
        store.attach_job_session("j-end", "sess-end")

        WikiIndexingSessionEndHook(runtime_stub)("sess-end", None)

        after = store.get_job("j-end").status
        if EXPECTED[status]["terminal"] or status == "failed":
            assert after == status
        else:
            assert after == "interrupted"

    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_resume_refuses_exactly_the_non_resumable_set(
        self, store, runtime_stub, status
    ) -> None:
        from mewbo_api.wiki.resume import WikiResume

        store.create_job(_job(status, job_id="j-res"))
        if EXPECTED[status]["resumable"]:
            return  # covered by the resume suite; only the refusal is pinned here
        with pytest.raises(ValueError):
            WikiResume.resume(store, runtime_stub, "j-res")


# ── 3. isActive on the one wire seam ───────────────────────────────────────────


class TestIsActiveOnTheWire:
    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_snapshot_carries_is_active(self, client, status) -> None:
        c, store, _rt, _hm = client
        store.create_job(_job(status, job_id="j-wire"))
        body = c.get("/v1/wiki/index/j-wire", headers=_h()).get_json()
        assert body["isActive"] is EXPECTED[status]["active"]

    def test_both_endpoints_serve_the_same_flag(self, client) -> None:
        """Both routes go through ``_job_wire``, so the console's one
        ``IndexingJob`` interface stays honest."""
        c, store, _rt, _hm = client
        store.create_job(_job("scanning", job_id="j-live"))
        listed = c.get("/v1/wiki/jobs/active", headers=_h()).get_json()
        row = next(r for r in listed if r["jobId"] == "j-live")
        snapshot = c.get("/v1/wiki/index/j-live", headers=_h()).get_json()
        assert row["isActive"] is True
        assert snapshot["isActive"] is True

    def test_is_active_survives_exclude_none(self, client) -> None:
        """``_job_wire`` dumps with ``exclude_none=True``; a bool must always
        be present so the console never has to treat absence as false."""
        c, store, _rt, _hm = client
        store.create_job(_job("complete", job_id="j-done"))
        body = c.get("/v1/wiki/index/j-done", headers=_h()).get_json()
        assert body["isActive"] is False


# ── 4. Session termination cascades into the job ───────────────────────────────


@pytest.fixture()
def live_runtime(tmp_path: Path, store):
    """A REAL SessionRuntime over a temp session store, wiki store attached."""
    from mewbo_core.loop.session_runtime import SessionRuntime
    from mewbo_core.session.session_store import SessionStore

    rt = SessionRuntime(session_store=SessionStore(root_dir=str(tmp_path / "sessions")))
    rt.wiki_store = store
    return rt


@pytest.fixture()
def terminating_app(monkeypatch, live_runtime, store, hook_manager):
    """Wiki routes registered against the real runtime — this is what wires the
    cascade onto ``register_on_terminate``."""
    monkeypatch.setenv("MEWBO_MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask

    flask_app = Flask(__name__ + "_terminate")
    flask_app.config["TESTING"] = True
    routes_mod.register(flask_app, live_runtime, hook_manager=hook_manager)

    yield live_runtime, store

    routes_mod._runtime = None
    routes_mod._hook_manager = None


class TestTerminationCascade:
    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_terminating_a_session_settles_its_live_job(
        self, terminating_app, status
    ) -> None:
        """A live job whose session is terminated must reach a TERMINAL status.

        Before the cascade existed, nothing propagated session death into the
        job at all: it sat at ``interrupted`` (or worse, ``scanning``) forever,
        counted as live by the active-jobs surface AND re-driven by restart
        recovery — re-starting the very run the user terminated.
        """
        runtime, store = terminating_app
        session_id = runtime.resolve_session()
        store.create_job(_job(status, job_id="j-term"))
        store.attach_job_session("j-term", session_id)

        runtime.terminate_session(session_id)

        after = store.get_job("j-term")
        if EXPECTED[status]["active"]:
            assert after.status == "cancelled"
            assert after.is_active is False
            assert after.is_recoverable is False
        else:
            assert after.status == status  # already settled — never rewritten

    def test_cascade_records_why_before_the_terminal_event(
        self, terminating_app
    ) -> None:
        """The job event log must explain the cancel, and the explanation must
        precede the terminal ``cancelled`` event (which closes the SSE stream)."""
        runtime, store = terminating_app
        session_id = runtime.resolve_session()
        store.create_job(_job("scanning", job_id="j-why"))
        store.attach_job_session("j-why", session_id)

        runtime.terminate_session(session_id)

        types = [e.get("type") for e in store.load_job_events("j-why")]
        assert "cancelled" in types
        assert types.index("log") < types.index("cancelled")

    def test_a_session_with_no_job_is_a_no_op(self, terminating_app) -> None:
        runtime, _store = terminating_app
        session_id = runtime.resolve_session()
        result = runtime.terminate_session(session_id)
        assert result["status"] == "terminated"

    def test_cascade_never_reports_a_cancelled_trigger(self, terminating_app) -> None:
        """``terminate_session`` sums an int return into ``cancelled_triggers``
        — the TRIGGER store's field. A job is not a trigger, so the wiki
        callback must return nothing."""
        runtime, store = terminating_app
        session_id = runtime.resolve_session()
        store.create_job(_job("scanning", job_id="j-count"))
        store.attach_job_session("j-count", session_id)

        result = runtime.terminate_session(session_id)
        assert result["cancelled_triggers"] == 0

    def test_a_raising_store_does_not_break_termination(
        self, terminating_app, monkeypatch
    ) -> None:
        runtime, store = terminating_app
        session_id = runtime.resolve_session()
        monkeypatch.setattr(
            type(store),
            "find_job_by_session",
            lambda self, sid: (_ for _ in ()).throw(RuntimeError("store down")),
        )
        result = runtime.terminate_session(session_id)
        assert result["status"] == "terminated"
        assert runtime.is_terminated(session_id) is True


# ── 5. A real hook manager on every indexing start path ────────────────────────


class TestHookManagerReachesEveryStartPath:
    def test_the_indexing_session_end_hook_is_registered(self, wiki_app) -> None:
        from mewbo_api.wiki.jobs import WikiIndexingSessionEndHook

        _app, _store, _rt, hm = wiki_app
        assert any(isinstance(h, WikiIndexingSessionEndHook) for h in hm.on_session_end)

    def test_post_index_starts_the_session_with_the_real_hook_manager(
        self, client
    ) -> None:
        """``hook_manager=None`` is not "no hooks" — ``Orchestrator`` swaps in a
        FRESH empty ``HookManager``, so the wiki session-end reconciler never
        fired for a job started by the ordinary index route."""
        c, _store, rt, hm = client
        resp = c.post("/v1/wiki/index", json=_submission_dict(), headers=_h())
        assert resp.status_code == 202
        assert rt.start_async.call_args.kwargs["hook_manager"] is hm

    def test_refresh_hands_the_route_hook_manager_down(
        self, client, monkeypatch
    ) -> None:
        c, store, _rt, hm = client
        import mewbo_api.wiki.routes as routes_mod
        from mewbo_graph.wiki.types import Project

        store.create_project(
            Project(
                slug="org/repo",
                source="github",
                lang="en",
                indexed_at="2026-01-01T00:00:00Z",
                pages=1,
                desc="d",
                repo_url="https://github.com/org/repo",
            )
        )
        seen: dict = {}

        class _CaptureJob:
            """Double for ``WikiIndexingJob``, pinned to the REAL refresh contract.

            It takes ``mode`` and returns an ``IndexingJob`` because the route
            passes the first and reads ``refresh_decision`` off the second — a
            double that accepted neither would 500 here while the production
            path was fine, which is the failure mode a hand-written stub of a
            collaborator invites.
            """

            @staticmethod
            def refresh(slug, *, mode="auto", runtime, hook_manager=None):
                seen["hook_manager"] = hook_manager
                seen["mode"] = mode
                return IndexingJob(
                    jobId="j-refresh",
                    slug=slug,
                    status="queued",
                    scannedCount=0,
                    totalCount=0,
                    currentFile=None,
                    refreshDecision=RefreshDecision(path="full", reason="requested"),
                )

        monkeypatch.setattr(routes_mod, "WikiIndexingJob", _CaptureJob)
        resp = c.post("/v1/wiki/projects/org%2Frepo/refresh", headers=_h())
        assert resp.status_code == 200
        assert seen["hook_manager"] is hm
        # A body-less refresh is an ``auto`` refresh — the product default that
        # makes the console's existing button take the cheap path.
        assert seen["mode"] == "auto"

    def test_recovery_hands_its_hook_manager_to_resume(
        self, store, runtime_stub, hook_manager, monkeypatch
    ) -> None:
        from mewbo_api.wiki import recovery as recovery_mod

        store.create_job(_job("interrupted", job_id="j-rec"))
        seen: dict = {}

        class _CaptureResume:
            @staticmethod
            def resume(store_, runtime_, job_id, **kwargs):
                seen.update(kwargs)
                return {"job_id": job_id, "session_id": "s", "status": "scanning"}

        monkeypatch.setattr(recovery_mod, "WikiResume", _CaptureResume)
        recovery_mod.JobRecovery.recover_interrupted(
            store, runtime_stub, hook_manager=hook_manager
        )
        assert seen["hook_manager"] is hook_manager

    def test_init_wiki_threads_its_hook_manager_into_recovery(
        self, monkeypatch, tmp_path
    ) -> None:
        """The recovery call site sits behind ``init_wiki`` — the manager has to
        travel the whole way or the boot-time re-drives keep starting hookless
        sessions."""
        import mewbo_api.wiki as wiki_pkg

        seen: dict = {}
        monkeypatch.setattr(
            wiki_pkg.JobRecovery,
            "recover_interrupted",
            classmethod(lambda cls, store, runtime, **kw: seen.update(kw) or []),
        )
        hm = object()
        wiki_pkg._run_recovery(MagicMock(), MagicMock(), hook_manager=hm)
        assert seen["hook_manager"] is hm
