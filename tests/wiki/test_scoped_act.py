"""Stage 2 of a scoped refresh — the act phase and its durable sidecar.

The runner drives the REAL delta pass over a REAL tree-sitter parse (same two
stubbed I/O boundaries as ``test_scoped_refresh.py``: the clone subprocess and
the embedder), because the work-list the act phase acts on is produced by the
doc planner rather than injected — a stubbed report would prove nothing about
which pages actually get regenerated.

What IS injected is the launcher, at the ``_act_launcher`` seam. The concrete
launcher lives behind a registration the api owns, so a test that required one
would be asserting on another layer's wiring rather than on this runner's
lifecycle.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mewbo_graph.plugins.wiki import _jobless as jobless_mod
from mewbo_graph.plugins.wiki._ctx import build_jobless_ctx
from mewbo_graph.plugins.wiki.act_launcher import ActLauncher, ActOutcome
from mewbo_graph.plugins.wiki.scoped_refresh import ScopedRefreshRunner
from mewbo_graph.wiki.memory_types import DocPageNote, FileManifest
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    IndexingJob,
    Project,
    WizardSubmission,
    make_graph_node,
)

SLUG = "example.com/o/r"
JOB = "j-act"
OLD_COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEW_COMMIT = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

KEPT_SOURCE = "def untouched():\n    return 1\n"
CHANGED_BEFORE = "def moved():\n    return 2\n"
CHANGED_AFTER = "def moved():\n    return 2\n\n\ndef added_later():\n    return 3\n"

# The page whose only anchor is the file the refresh dirties, so the planner
# scores it 0.5 — above the ``edit`` threshold — and it lands on the work-list.
STALE_PAGE = "changed-overview"


SESSION = "sess-act-1"


class _FakeLauncher:
    """A launcher with the REAL split shape: ``start`` returns, ``wait`` blocks.

    Faking the two as one synchronous call is precisely the mistake this seam is
    shaped to prevent, so the fake keeps them apart and records what the store
    held AT WAIT TIME — the only way to prove the runner wrote the sidecar (and
    the page plan) before it blocked rather than after it returned.
    """

    def __init__(
        self,
        store: JsonWikiStore | None = None,
        *,
        session_id: str | None = SESSION,
        status: str = "completed",
        error: str | None = None,
        raises: Exception | None = None,
        submits: list[str] | None = None,
    ) -> None:
        self.store = store
        self.calls: list[dict[str, object]] = []
        self.waited: list[str] = []
        self.seen_at_wait: dict[str, object] = {}
        self._session_id = session_id
        self._status = status
        self._error = error
        self._raises = raises
        # What the act session claims while it runs. ``None`` means "every page
        # it was asked for" — a well-behaved tier. A shorter list is a model that
        # wrote some, and ``[]`` is one that narrated its submissions instead of
        # making them, which is the case the gate exists for.
        self._submits = submits

    @classmethod
    def available(cls) -> bool:
        return True

    def start(
        self,
        *,
        job_id: str,
        slug: str,
        page_ids: list[str],
        submission: WizardSubmission | None = None,
    ) -> str | None:
        self.calls.append({
            "job_id": job_id, "slug": slug,
            "page_ids": list(page_ids), "submission": submission,
        })
        if self._raises is not None:
            raise self._raises
        return self._session_id

    def wait(self, session_id: str, *, timeout: float | None = None) -> ActOutcome:
        self.waited.append(session_id)
        if self.store is not None:
            self.seen_at_wait = {
                "act_plan": self.store.get_act_plan(JOB),
                "job_plan": self.store.get_job_plan(JOB),
                "timeout": timeout,
            }
            asked = [str(p) for p in self.calls[-1]["page_ids"]]  # type: ignore[union-attr]
            for page_id in (asked if self._submits is None else self._submits):
                # The real session claims through this same call — claim, then
                # write — so the gate reads exactly what production reads.
                self.store.claim_job_page(SLUG, JOB, page_id)
        return ActOutcome(
            session_id=session_id, status=self._status, error=self._error
        )


def _submission() -> WizardSubmission:
    return WizardSubmission.model_validate({
        "repoUrl": "https://example.com/o/r",
        "slug": SLUG,
        "platform": "git",
        "depth": "comprehensive",
        "language": "en",
        "model": "unused-in-a-scoped-refresh",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    })


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _seed_prior_index(
    store: JsonWikiStore, extra_pages: list[tuple[str, str]] = []
) -> None:
    """The state a completed FULL index at ``OLD_COMMIT`` would leave behind.

    *extra_pages* seeds further ``(page_id, title)`` notes anchored to the same
    dirtied file, so a test can express a work-list of more than one page — the
    only way a PARTIAL act phase is expressible at all.
    """
    store.upsert_nodes(
        SLUG,
        [
            make_graph_node(
                slug=SLUG, node_id="kept-file", type="File",
                name="kept.py", file="kept.py", range=(0, len(KEPT_SOURCE)),
            ),
            make_graph_node(
                slug=SLUG, node_id="changed-file", type="File",
                name="changed.py", file="changed.py",
                range=(0, len(CHANGED_BEFORE)),
            ),
        ],
        commit_sha=OLD_COMMIT,
    )
    store.upsert_file_manifest(
        SLUG,
        [
            FileManifest(
                slug=SLUG, path="kept.py", content_hash=_hash(KEPT_SOURCE),
                last_indexed_commit=OLD_COMMIT, entity_keys=["kept.py"],
            ),
            FileManifest(
                slug=SLUG, path="changed.py", content_hash=_hash(CHANGED_BEFORE),
                last_indexed_commit=OLD_COMMIT, entity_keys=["changed.py"],
            ),
        ],
    )
    store.upsert_doc_notes(SLUG, [
        DocPageNote(
            slug=SLUG, page_id=page_id, title=title,
            content_hash=_hash("old body"), page_type="module",
            anchor_keys=["changed.py"], last_indexed_commit=OLD_COMMIT,
        )
        for page_id, title in [(STALE_PAGE, "What changed.py does"), *extra_pages]
    ])
    store.create_project(Project(
        slug=SLUG, source="git", lang="en", indexedAt="2020-01-01T00:00:00Z",
        pages=7, desc="the description a maintainer edited",
        landingPageId="overview", repoUrl="https://example.com/o/r",
        branch="main", commitSha=OLD_COMMIT, commitShort=OLD_COMMIT[:7],
    ))


@pytest.fixture(autouse=True)
def _no_ambient_launcher():
    """Pin the seam to "unregistered" around every test in this module.

    ``ActLauncher._impl`` is a process-wide ClassVar and ``init_wiki`` registers
    a real one, so ANY earlier test in the session that initialises the api
    leaves a concrete launcher installed — under which the tests below that
    assert the act phase is ABSENT instead drive a live session. It reproduced
    exactly that way: green alone, ``pages`` emitted in the full suite. Restore
    rather than clear on the way out, so this fixture cannot become the leak it
    exists to contain.
    """
    previous = ActLauncher._impl
    ActLauncher.reset()
    yield
    ActLauncher.register(previous)


@pytest.fixture
def refresh(tmp_path, monkeypatch):
    """Return ``run(changed_body) -> store`` for one real scoped refresh."""
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    monkeypatch.setattr(
        jobless_mod, "_git_rev_parse",
        lambda d, args: NEW_COMMIT if args == ["HEAD"] else "main",
    )
    monkeypatch.setattr(
        "mewbo_graph.wiki.embedder.Embedder.enabled", staticmethod(lambda: False)
    )
    monkeypatch.setattr(
        "mewbo_graph.wiki.embedder.make_embedder_or_none", lambda: None
    )

    def _run(
        changed_body: str = CHANGED_AFTER,
        launcher: _FakeLauncher | None = None,
        extra_pages: list[tuple[str, str]] = [],
        before_run=None,
    ) -> JsonWikiStore:
        store = JsonWikiStore(root_dir=tmp_path / "wiki")
        if launcher is not None:
            # Bound here rather than at construction so the fake can read the
            # store mid-``wait`` — the store does not exist until the run does.
            launcher.store = store
            monkeypatch.setattr(
                ScopedRefreshRunner, "_act_launcher", staticmethod(lambda: launcher)
            )
        store.create_job(IndexingJob(
            jobId=JOB, slug=SLUG, status="queued",
            scannedCount=0, totalCount=0, currentFile=None,
        ))
        _seed_prior_index(store, extra_pages)

        def _fake_clone(cmd, *a, **kw):
            dest = Path(cmd[-1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "kept.py").write_text(KEPT_SOURCE, encoding="utf-8")
            (dest / "changed.py").write_text(changed_body, encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", _fake_clone)
        if before_run is not None:
            # The last write before the runner starts — how a RESUME arrives:
            # an advanced manifest and/or a sidecar left mid-stage-2.
            before_run(store)
        ctx = build_jobless_ctx(job_id=JOB, slug=SLUG, store=store)
        ScopedRefreshRunner(ctx, _submission()).run()
        return store

    return _run


def _phases(store: JsonWikiStore) -> list[str]:
    return [
        e["name"] for e in store.load_job_events(JOB, after_idx=-1)
        if e.get("type") == "phase"
    ]


def _logs(store: JsonWikiStore) -> str:
    return "\n".join(
        str(e.get("text", "")) for e in store.load_job_events(JOB, after_idx=-1)
        if e.get("type") == "log"
    )


# ── with an act tier registered ─────────────────────────────────────────────


def test_the_act_phase_runs_under_pages_before_finalize(refresh) -> None:
    """``pages`` must be emitted, and it must come BEFORE ``finalize``.

    Both halves matter. The name is the existing ``IndexingPhase`` member the
    scoped pipeline had never used, so nothing about the console's phase map or
    the stored ordinal moves. The ORDER is what keeps the work visible: the
    finalizer writes ``status="complete"`` and the ``complete`` event, and the
    SSE stream closes on it — a page written afterwards reaches no reader.
    """
    launcher = _FakeLauncher()

    store = refresh(launcher=launcher)

    assert _phases(store) == ["clone", "scan", "graph", "pages", "finalize"]
    assert [c["page_ids"] for c in launcher.calls] == [[STALE_PAGE]]
    assert launcher.calls[0]["slug"] == SLUG
    assert launcher.calls[0]["job_id"] == JOB
    job = store.get_job(JOB)
    assert job is not None and job.status == "complete"


def test_the_refresh_waits_for_the_act_session_before_finalizing(refresh) -> None:
    """``start`` returns a session id, NOT a finished act phase.

    The seam is split precisely because the session runs on: treating ``start``
    as the whole phase drops the runner straight into finalize, which writes
    ``status="complete"`` and the event the SSE stream closes on, while pages are
    still being rewritten. Nothing raises — the run just announces work it has
    not finished — so the only thing that can catch it is asserting the wait.
    """
    launcher = _FakeLauncher()

    store = refresh(launcher=launcher)

    assert launcher.waited == [SESSION]
    # No timeout is passed: the deadline belongs to the concrete launcher, and a
    # second one named here would be one bound in two places.
    assert launcher.seen_at_wait["timeout"] is None
    job = store.get_job(JOB)
    assert job is not None and job.status == "complete"


def test_the_submission_travels_with_the_act_session(refresh) -> None:
    """The api mints the session from the operator's own model/depth/language."""
    launcher = _FakeLauncher()

    refresh(launcher=launcher)

    submission = launcher.calls[0]["submission"]
    assert isinstance(submission, WizardSubmission)
    assert submission.slug == SLUG


def test_the_narrowed_work_list_is_the_denominator_before_the_session_starts(
    refresh,
) -> None:
    """A scoped job commits no page plan, and ``wiki_submit_page`` divides by it.

    The denominator that tool reports progress against is the committed plan's
    LENGTH, which is zero for a refresh — every regenerated page would render as
    a fraction of nothing. It has to be in place BEFORE the session starts
    writing, or the first page it submits is the one that misses it.
    """
    launcher = _FakeLauncher()

    store = refresh(launcher=launcher)

    expected = [{"id": STALE_PAGE, "title": "What changed.py does"}]
    assert launcher.seen_at_wait["job_plan"] == expected
    assert store.get_job_plan(JOB) == expected


def test_the_sidecar_carries_the_session_id_while_the_act_phase_runs(
    refresh,
) -> None:
    """Recorded BETWEEN ``start`` and ``wait`` — the reason the seam is split.

    A job that dies inside the wait resumes knowing not just that stage 2 was
    owed but WHICH session was already running, so it can judge or re-attach to
    that one. Without the id a resume starts a second session that rewrites the
    same pages concurrently with the first, which is worse than doing nothing.
    """
    launcher = _FakeLauncher()

    refresh(launcher=launcher)

    assert launcher.seen_at_wait["act_plan"] == {
        "stage": "act", "page_ids": [STALE_PAGE],
        "commit_sha": NEW_COMMIT, "session_id": SESSION,
    }


def test_the_sidecar_reaches_finalize_stage_when_the_act_phase_succeeds(
    refresh,
) -> None:
    """The record carries the stage, the work-list and the session that did it."""
    store = refresh(launcher=_FakeLauncher())

    assert store.get_act_plan(JOB) == {
        "stage": "finalize", "page_ids": [STALE_PAGE],
        "commit_sha": NEW_COMMIT, "session_id": SESSION,
    }


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        ("timeout", "act session did not settle within 3600s", "timeout"),
        ("unmet_goal", None, "unmet_goal"),
        ("failed", "the model refused", "the model refused"),
    ],
)
def test_a_not_ok_outcome_fails_the_job_and_leaves_stage_two_owed(
    refresh, status: str, error: str | None, expected: str
) -> None:
    """Only ``completed`` is ok — ``unmet_goal`` and a timeout are failures.

    And the job log must be able to tell them apart: a timeout means the wait
    gave up on a session that may still be running, a refusal means it stopped.
    Collapsing both to "act phase failed" leaves an operator with no way to know
    which happened, and the log is the only place either is ever read.
    """
    store = refresh(launcher=_FakeLauncher(status=status, error=error))

    plan = store.get_act_plan(JOB)
    assert plan is not None
    assert plan["stage"] == "act"
    assert plan["session_id"] == SESSION
    job = store.get_job(JOB)
    assert job is not None and job.status == "failed"
    errors = [
        e for e in store.load_job_events(JOB, after_idx=-1) if e.get("type") == "error"
    ]
    assert expected in errors[0]["error"]["message"]
    types = {e.get("type") for e in store.load_job_events(JOB, after_idx=-1)}
    assert "complete" not in types


SECOND_PAGE = "changed-internals"
TWO_PAGES = [(SECOND_PAGE, "How changed.py works")]


# ── recovering a work-list a resumed run cannot re-derive ───────────────────


def test_a_resumed_act_phase_recovers_the_work_list_from_its_own_record(
    refresh,
) -> None:
    """A resumed run's own diff CANNOT see the pages it still owes.

    Stage 1 ends by advancing the file manifest for the scope it touched, so the
    resumed run diffs the tree against an already-advanced manifest, finds
    nothing, and gets ``RefreshReport.noop`` — ``pages_to_regenerate`` empty.
    Trusting that would finalize having rewritten none of the pages the first
    attempt scored stale, and clicking Refresh could never recover them: the
    next refresh diffs against the same advanced manifest and also finds
    nothing. Silent, permanent, and it renders as success.

    Seeded here the way a resume really arrives: the manifest already carries
    the NEW content hash (so the refresh is a genuine no-op) and the sidecar
    still reads ``stage="act"``.
    """
    launcher = _FakeLauncher()

    def _resume(store: JsonWikiStore) -> None:
        store.upsert_file_manifest(SLUG, [FileManifest(
            slug=SLUG, path="changed.py", content_hash=_hash(CHANGED_AFTER),
            last_indexed_commit=NEW_COMMIT, entity_keys=["changed.py"],
        )])
        store.save_act_plan(JOB, {
            "stage": "act", "page_ids": [STALE_PAGE],
            "commit_sha": NEW_COMMIT, "session_id": "sess-that-died",
        })

    store = refresh(launcher=launcher, before_run=_resume)

    # The delta pass really did find nothing — this is the no-op path.
    assert "no file changed since the last index" in _logs(store)
    # …and stage 2 ran anyway, on the recovered list.
    assert [c["page_ids"] for c in launcher.calls] == [[STALE_PAGE]]
    assert _phases(store) == ["clone", "scan", "graph", "pages", "finalize"]
    assert "Resuming an unfinished act phase" in _logs(store)
    assert store.get_act_plan(JOB) == {
        "stage": "finalize", "page_ids": [STALE_PAGE],
        "commit_sha": NEW_COMMIT, "session_id": SESSION,
    }


def test_recovery_keeps_a_page_this_pass_scored_that_the_last_one_missed(
    refresh,
) -> None:
    """Union, not replace — a dropped page is dropped permanently.

    A resumed run whose manifest is only partly advanced can legitimately score
    something the earlier attempt never saw. Letting the recovered list replace
    the scored one would lose it by exactly the mechanism this recovery exists
    to fix, whereas the opposite error only costs one rewrite.
    """
    launcher = _FakeLauncher()

    def _resume(store: JsonWikiStore) -> None:
        store.save_act_plan(JOB, {
            "stage": "act", "page_ids": [SECOND_PAGE],
            "commit_sha": NEW_COMMIT, "session_id": "sess-that-died",
        })

    store = refresh(launcher=launcher, extra_pages=TWO_PAGES, before_run=_resume)

    # Recovered first, then what this pass scored and the record did not carry.
    assert launcher.calls[0]["page_ids"] == [SECOND_PAGE, STALE_PAGE]
    plan = store.get_act_plan(JOB)
    assert plan is not None and plan["page_ids"] == [SECOND_PAGE, STALE_PAGE]


def test_a_settled_record_is_not_treated_as_owed_work(refresh) -> None:
    """Only ``stage="act"`` means work is owed.

    A record left at ``finalize`` by a previous COMPLETED refresh must not
    resurrect that run's work-list on the next one — every subsequent refresh of
    the project would regenerate the same pages forever.
    """
    launcher = _FakeLauncher()

    def _settled(store: JsonWikiStore) -> None:
        store.save_act_plan(JOB, {
            "stage": "finalize", "page_ids": ["a-page-from-last-time"],
            "commit_sha": OLD_COMMIT, "session_id": "sess-old",
        })

    store = refresh(launcher=launcher, before_run=_settled)

    assert launcher.calls[0]["page_ids"] == [STALE_PAGE]
    assert "a-page-from-last-time" not in _logs(store)


def test_an_ok_outcome_that_wrote_nothing_is_treated_as_a_failure(refresh) -> None:
    """A settled session says the agent STOPPED, not that it did the work.

    Nothing gates the act agent on actually calling ``wiki_submit_page`` — QA
    has ``required_terminal=True`` on its answer tool and the page writer has no
    equivalent — so a model that narrates its submissions instead of making them
    ends ``completed`` and every layer above reports a refresh that regenerated
    zero pages as a success. Finalizing here would publish that lie under a
    ``complete`` event, on the exact surface this work exists to fix.
    """
    store = refresh(launcher=_FakeLauncher(submits=[]))

    job = store.get_job(JOB)
    assert job is not None and job.status == "failed"
    plan = store.get_act_plan(JOB)
    assert plan is not None
    assert plan["stage"] == "act"  # a resume retries stage 2, not the whole run
    assert plan["session_id"] == SESSION
    types = {e.get("type") for e in store.load_job_events(JOB, after_idx=-1)}
    assert "complete" not in types
    errors = [
        e for e in store.load_job_events(JOB, after_idx=-1) if e.get("type") == "error"
    ]
    assert "submitted no page" in errors[0]["error"]["message"]


def test_a_partial_act_phase_completes_but_names_what_it_missed(refresh) -> None:
    """Partial is real work, so it publishes — loudly, and by name.

    The pages that WERE rewritten genuinely improved and their doc notes are
    re-stamped, so failing would throw that away. But an unnamed shortfall is
    how a staleness signal decays into noise: the timeline has to say how many
    pages still describe the previous commit, and which.
    """
    store = refresh(launcher=_FakeLauncher(submits=[STALE_PAGE]), extra_pages=TWO_PAGES)

    job = store.get_job(JOB)
    assert job is not None and job.status == "complete"
    logs = _logs(store)
    assert "Regenerated 1 of 2 scored page(s)" in logs
    assert SECOND_PAGE in logs
    assert "still describe(s) the previous commit" in logs
    assert store.get_act_plan(JOB) == {
        "stage": "finalize", "page_ids": [STALE_PAGE, SECOND_PAGE],
        "commit_sha": NEW_COMMIT, "session_id": SESSION,
    }


def test_a_complete_act_phase_says_so_without_a_warning(refresh) -> None:
    """The all-done line must not read like the shortfall line.

    A warning on the healthy path is worse than no warning at all — it trains
    every reader to skip the one case that matters.
    """
    store = refresh(launcher=_FakeLauncher(), extra_pages=TWO_PAGES)

    logs = _logs(store)
    assert "Regenerated all 2 scored page(s)" in logs
    assert "NOT rewritten" not in logs


def test_a_raising_launcher_fails_the_job_and_leaves_stage_two_owed(
    refresh,
) -> None:
    """Dying in stage 2 must not read as "start again from the clone".

    The whole reason the sidecar is written BEFORE the launcher runs: a resume
    reads ``stage == "act"`` and knows clone, scan and the entire delta pass are
    already durable. Swallowing the failure and finalizing anyway would publish
    a wiki whose pages were scored stale and never rewritten, under a
    ``complete`` event that says otherwise.
    """
    store = refresh(launcher=_FakeLauncher(raises=RuntimeError("model refused")))

    assert store.get_act_plan(JOB) == {
        "stage": "act", "page_ids": [STALE_PAGE],
        "commit_sha": NEW_COMMIT, "session_id": None,
    }
    job = store.get_job(JOB)
    assert job is not None and job.status == "failed"
    types = {e.get("type") for e in store.load_job_events(JOB, after_idx=-1)}
    assert "complete" not in types
    assert "error" in types


def test_a_launcher_that_mints_no_session_degrades_instead_of_failing(
    refresh,
) -> None:
    """``start`` returning None is "no stage 2 here", never a failure.

    A registered launcher with no session-start body for this job has started
    nothing, so there is nothing half-done for a resume to pick up — the refresh
    publishes its deterministic half and says the pages were left alone. Failing
    would strand a job whose graph and memory deltas are already durable.
    """
    launcher = _FakeLauncher(session_id=None)

    store = refresh(launcher=launcher)

    assert launcher.waited == []
    assert _phases(store) == ["clone", "scan", "graph", "finalize"]
    job = store.get_job(JOB)
    assert job is not None and job.status == "complete"
    assert "no act session could be started" in _logs(store)
    assert store.get_act_plan(JOB) == {
        "stage": "finalize", "page_ids": [STALE_PAGE],
        "commit_sha": NEW_COMMIT, "session_id": None,
    }


# ── with no act tier registered ─────────────────────────────────────────────


def test_without_a_launcher_the_refresh_finalizes_and_says_what_it_skipped(
    refresh,
) -> None:
    """No registered tier is a supported deployment, not a broken one.

    The deterministic half is genuinely applied and must publish; the pages it
    scored stale are still the OLD pages, and nothing else in the timeline would
    say so. The phase vocabulary stays exactly what it was — ``pages`` is
    emitted by work that actually started, never by work that was skipped.
    """
    store = refresh()

    assert _phases(store) == ["clone", "scan", "graph", "finalize"]
    job = store.get_job(JOB)
    assert job is not None and job.status == "complete"
    assert "no page-regeneration tier is" in _logs(store)
    # Stage 1 still finished, so a resume must not re-drive it.
    assert store.get_act_plan(JOB) == {
        "stage": "finalize", "page_ids": [STALE_PAGE],
        "commit_sha": NEW_COMMIT, "session_id": None,
    }


def test_the_real_seam_with_no_impl_registered_reads_as_no_act_tier(
    refresh,
) -> None:
    """``available()`` False is the SAME answer as an absent module.

    Against the REAL :class:`ActLauncher` rather than a stub, because the module
    ships with this package while the concrete impl is registered by the api —
    "importable" and "usable" are different questions, and only the second may
    promise a regeneration. This is the state every graph-only install is in.
    """
    assert ActLauncher.available() is False  # pinned by ``_no_ambient_launcher``

    store = refresh()

    assert _phases(store) == ["clone", "scan", "graph", "finalize"]
    assert "no page-regeneration tier is" in _logs(store)


def test_an_import_error_on_the_seam_reads_as_no_act_tier(
    refresh, monkeypatch
) -> None:
    """A lean install without the module must degrade, never crash the refresh."""
    monkeypatch.setitem(
        sys.modules, "mewbo_graph.plugins.wiki.act_launcher", None
    )

    store = refresh()

    assert _phases(store) == ["clone", "scan", "graph", "finalize"]
    job = store.get_job(JOB)
    assert job is not None and job.status == "complete"


def test_a_clean_tree_writes_the_sidecar_with_an_empty_work_list(refresh) -> None:
    """Nothing to regenerate is a RESULT, and the record has to carry it.

    Withholding the sidecar on the empty case would make a resumed job re-clone,
    re-scan and re-run the whole delta pass only to rediscover that there was
    nothing to do — the exact cost the record exists to avoid.
    """
    launcher = _FakeLauncher()

    store = refresh(CHANGED_BEFORE, launcher=launcher)

    assert launcher.calls == []
    assert _phases(store) == ["clone", "scan", "graph", "finalize"]
    plan = store.get_act_plan(JOB)
    assert plan is not None
    assert plan["stage"] == "finalize"
    assert plan["page_ids"] == []
