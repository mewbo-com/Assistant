"""Refresh path selection — which engine a refresh actually reaches, and why.

The property under test is a COST property, not a correctness one: a scoped
refresh that quietly minted an indexer session would return the right wiki and
bill for the full rebuild it exists to avoid. So these tests assert on the SEAM
that was reached (``_start_indexer_session`` vs ``_start_scoped_refresh``) and
on whether a session was minted at all — never on the returned job alone, which
looks identical either way.

The one I/O boundary stubbed here is ``jobs._current_index_fingerprint``: the
real probe reads installed package metadata and ``shutil.which``, so pinning it
is what lets a test reach ``fingerprint_mismatch`` without installing a
different tree-sitter grammar pack.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mewbo_api.wiki.jobs import WikiIndexingJob
from mewbo_api.wiki.resume import WikiResume
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    IndexFingerprint,
    IndexingJob,
    Project,
    RefreshDecision,
)

API_KEY = "test-key-123"

# What the probe reports "a refresh started now would build with". Every
# fingerprint case below is expressed as a delta from this one value.
CURRENT = IndexFingerprint(
    embeddingModel="fake/embedding-3",
    graphSchemaVersion="1",
    grammarPackVersion="1.12.0",
    resolverAvailable=True,
)


def _seed_project(
    store: JsonWikiStore,
    *,
    slug: str = "bearlike/Assistant",
    commit_sha: str | None = "abc123",
    fingerprint: IndexFingerprint | None = CURRENT,
    graph_only: bool = False,
) -> Project:
    """Seed a completed index whose record is the only input ``decide`` reads.

    Nothing else is seeded on purpose: with no settings record and no job
    sidecar, ``refresh`` falls to ``submission_from_project``, so these tests
    exercise the same reconstruction ladder a legacy project takes.

    ``repoUrl`` is NOT optional decoration here — the route reads a project with
    no clone URL and no git submission as a catalog project and refuses it
    before any decision is computed, so omitting it makes every route case below
    assert the catalog guard instead of the thing it names.
    """
    project = Project(
        slug=slug,
        source="github",
        lang="en",
        indexed_at="2026-01-01T00:00:00Z",
        pages=5,
        desc="Test repo",
        repoUrl=f"https://github.com/{slug}",
        commitSha=commit_sha,
        fingerprint=fingerprint,
        graphOnly=graph_only,
    )
    store.create_project(project)
    return project


@pytest.fixture()
def store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def runtime(store: JsonWikiStore) -> MagicMock:
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-abc"
    rt.start_async.return_value = "sess-abc:r1"
    rt.is_running.return_value = False
    return rt


@pytest.fixture()
def seams():
    """Record which engine a start path reached, without running either.

    Both are patched at ``jobs``' module scope, which is where
    ``WikiIndexingJob`` looks them up. ``resume`` binds its own references at
    import time and is patched separately in the resume tests below — patching
    one module's name does NOT reach the other's copy, and a test that assumed
    it did would pass while asserting nothing.
    """
    with (
        patch("mewbo_api.wiki.jobs._current_index_fingerprint", return_value=CURRENT),
        patch("mewbo_api.wiki.jobs._start_indexer_session") as indexer,
        patch("mewbo_api.wiki.jobs._start_scoped_refresh") as scoped,
        patch("mewbo_api.wiki.jobs._start_graph_only_index") as graph_only,
    ):
        yield {"indexer": indexer, "scoped": scoped, "graph_only": graph_only}


# ── The criterion: only a full rebuild may reach the indexer session ─────────


# Each row isolates ONE reason: every field the earlier arms of ``decide`` test
# is set to the value that would let the decision fall through to the next arm,
# so a row can only be explained by the reason it names.
REASON_CASES = [
    # (id, mode, commit_sha, fingerprint, graph_only, expected_reason)
    ("requested", "full", "abc123", CURRENT, False, "requested"),
    ("graph_only", "auto", "abc123", CURRENT, True, "graph_only"),
    ("no_prior_index", "auto", None, CURRENT, False, "no_prior_index"),
    ("fingerprint_unknown", "auto", "abc123", None, False, "fingerprint_unknown"),
    (
        "fingerprint_mismatch",
        "auto",
        "abc123",
        CURRENT.model_copy(update={"grammar_pack_version": "1.11.0"}),
        False,
        "fingerprint_mismatch",
    ),
]


@pytest.mark.parametrize(
    "case_id,mode,commit_sha,fingerprint,graph_only,expected_reason",
    REASON_CASES,
    ids=[c[0] for c in REASON_CASES],
)
def test_every_full_reason_is_reachable_through_refresh(
    store, runtime, seams, case_id, mode, commit_sha, fingerprint, graph_only, expected_reason
):
    """Each ``RefreshFullReason`` is minted by a real ``refresh`` call.

    Reaching them through the façade rather than calling ``decide`` directly is
    the point: a reason the pure policy can produce but no caller can ever
    trigger is dead vocabulary, and the console renders these verbatim.
    """
    _seed_project(
        store, commit_sha=commit_sha, fingerprint=fingerprint, graph_only=graph_only
    )

    job = WikiIndexingJob.refresh("bearlike/Assistant", mode=mode, runtime=runtime)

    assert job.refresh_decision == RefreshDecision(
        path="full",
        reason=expected_reason,
        mismatches=job.refresh_decision.mismatches,
    )
    assert job.refresh_decision.path == "full"
    assert job.refresh_decision.reason == expected_reason
    # The decision is DURABLE, not just returned — the resume branch and the
    # console both read it back off the stored job.
    assert store.get_job(job.job_id).refresh_decision.reason == expected_reason
    seams["scoped"].assert_not_called()


def test_fingerprint_mismatch_names_every_disagreeing_field(store, runtime, seams):
    """A mismatch carries the fields, not just the verdict — one pass, all reasons."""
    stale = CURRENT.model_copy(
        update={"grammar_pack_version": "1.11.0", "resolver_available": False}
    )
    _seed_project(store, fingerprint=stale)

    job = WikiIndexingJob.refresh("bearlike/Assistant", mode="auto", runtime=runtime)

    decision = job.refresh_decision
    assert decision.reason == "fingerprint_mismatch"
    assert {m.field for m in decision.mismatches} == {
        "grammar_pack_version",
        "resolver_available",
    }
    # ``expected`` is what BUILT the stored index; ``actual`` is what a refresh
    # would build with now. Getting these backwards renders the reason inverted.
    pack = next(m for m in decision.mismatches if m.field == "grammar_pack_version")
    assert (pack.expected, pack.actual) == ("1.11.0", "1.12.0")


@pytest.mark.parametrize(
    "case_id,mode,commit_sha,fingerprint,graph_only,expected_reason",
    REASON_CASES,
    ids=[c[0] for c in REASON_CASES],
)
def test_only_a_full_rebuild_ever_reaches_the_indexer_session(
    store, runtime, seams, case_id, mode, commit_sha, fingerprint, graph_only, expected_reason
):
    """THE criterion, asserted through a recorded seam rather than by reading source.

    One-directional on purpose: every path that reaches ``_start_indexer_session``
    is ``full``, but not every ``full`` reaches it — a graph-only project is a
    full rebuild that still runs the deterministic zero-LLM engine.
    """
    _seed_project(
        store, commit_sha=commit_sha, fingerprint=fingerprint, graph_only=graph_only
    )

    job = WikiIndexingJob.refresh("bearlike/Assistant", mode=mode, runtime=runtime)

    if seams["indexer"].called:
        assert job.refresh_decision.path == "full"
    # The scoped engine is never reached on a full rebuild, whatever the reason.
    seams["scoped"].assert_not_called()


def test_scoped_refresh_starts_the_runner_and_mints_no_session(store, runtime, seams):
    """An eligible project takes the cheap path — and pays for no session at all.

    The three negative assertions are the actual product claim. A scoped refresh
    that minted a session would still produce a correct wiki, so only the absence
    of the session, the capability advertisement, and the indexer start
    distinguishes "free" from "we rebuilt everything and returned the same JSON".
    """
    _seed_project(store, commit_sha="abc123", fingerprint=CURRENT)

    job = WikiIndexingJob.refresh("bearlike/Assistant", mode="auto", runtime=runtime)

    assert job.refresh_decision == RefreshDecision(path="scoped")
    assert job.refresh_decision.reason is None
    seams["scoped"].assert_called_once()
    assert seams["scoped"].call_args.kwargs["job_id"] == job.job_id
    assert seams["scoped"].call_args.kwargs["submission"].slug == "bearlike/Assistant"

    seams["indexer"].assert_not_called()
    runtime.resolve_session.assert_not_called()
    runtime.append_context_event.assert_not_called()
    runtime.start_async.assert_not_called()


def test_scoped_refresh_still_writes_the_shared_prologue(store, runtime, seams):
    """The cheap path is not a shortcut past the records every start path owes.

    The job, its immutable submission sidecar and the slug-keyed settings record
    are what recovery, the settings façade and the next refresh all read. A
    scoped arm that skipped them would strand its own job on restart.
    """
    _seed_project(store, commit_sha="abc123", fingerprint=CURRENT)

    job = WikiIndexingJob.refresh("bearlike/Assistant", mode="auto", runtime=runtime)

    assert store.get_job(job.job_id) is not None
    assert store.get_job_submission(job.job_id) is not None
    assert store.get_project_settings("bearlike/Assistant") is not None


def test_refresh_of_unknown_project_raises_before_probing(store, runtime, seams):
    """Existence is checked before the decision — no probe for a project that isn't there."""
    with pytest.raises(KeyError):
        WikiIndexingJob.refresh("no/such", mode="auto", runtime=runtime)
    seams["indexer"].assert_not_called()
    seams["scoped"].assert_not_called()


# ── Resume: a stranded scoped job restarts scoped, never on the agent path ───


def _seed_scoped_job(store: JsonWikiStore, *, job_id: str = "job-scoped") -> IndexingJob:
    """An interrupted job that recorded a scoped decision at creation."""
    job = IndexingJob(
        jobId=job_id,
        slug="bearlike/Assistant",
        status="interrupted",
        scannedCount=0,
        totalCount=0,
        currentFile=None,
        model="anthropic/claude-sonnet-4-6",
        refreshDecision=RefreshDecision(path="scoped"),
    )
    store.create_job(job)
    store.save_job_submission(job_id, {
        "repoUrl": "https://github.com/bearlike/Assistant",
        "slug": "bearlike/Assistant",
        "platform": "github",
        "depth": "comprehensive",
        "language": "en",
        "model": "anthropic/claude-sonnet-4-6",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    })
    return job


@pytest.fixture()
def resume_seams():
    """Patch the seams as ``resume`` bound them, not as ``jobs`` exports them."""
    with (
        patch("mewbo_api.wiki.resume._start_scoped_refresh") as scoped,
        patch("mewbo_api.wiki.resume._start_indexer_session") as indexer,
    ):
        yield {"scoped": scoped, "indexer": indexer}


def test_resume_redrives_a_scoped_job_on_the_scoped_runner(store, runtime, resume_seams):
    """A sessionless scoped job restarts whole on its own engine.

    ``refresh_decision`` is the sticky signal that makes this answerable after a
    process restart: it is stamped once at creation and never rewritten, so it
    survives the death that stranded the job.
    """
    _seed_scoped_job(store)

    result = WikiResume.resume(store, runtime, "job-scoped")

    resume_seams["scoped"].assert_called_once()
    assert resume_seams["scoped"].call_args.kwargs["job_id"] == "job-scoped"
    resume_seams["indexer"].assert_not_called()
    # Sessionless, exactly like graph-only: there is nothing to watch, and the
    # console reads an empty session id as "render no jump affordance".
    assert result == {"job_id": "job-scoped", "session_id": "", "status": "scanning"}
    assert store.get_job("job-scoped").status == "scanning"


def test_resume_of_a_full_job_still_takes_the_agent_path(store, runtime, resume_seams):
    """A job that rebuilt fully resumes the way it always did."""
    job = IndexingJob(
        jobId="job-full",
        slug="bearlike/Assistant",
        status="interrupted",
        scannedCount=0,
        totalCount=0,
        currentFile=None,
        model="anthropic/claude-sonnet-4-6",
        refreshDecision=RefreshDecision(path="full", reason="requested"),
    )
    store.create_job(job)

    WikiResume.resume(store, runtime, "job-full")

    resume_seams["indexer"].assert_called_once()
    resume_seams["scoped"].assert_not_called()


def test_resume_of_a_scoped_job_without_a_sidecar_degrades_to_the_agent_path(
    store, runtime, resume_seams
):
    """No submission to scope against ⇒ rebuild fully rather than guess at a scope.

    A full rebuild is always CORRECT and merely expensive; a delta computed from
    a submission nobody has is neither.
    """
    job = IndexingJob(
        jobId="job-orphan",
        slug="bearlike/Assistant",
        status="interrupted",
        scannedCount=0,
        totalCount=0,
        currentFile=None,
        model="anthropic/claude-sonnet-4-6",
        refreshDecision=RefreshDecision(path="scoped"),
    )
    store.create_job(job)  # no save_job_submission

    WikiResume.resume(store, runtime, "job-orphan")

    resume_seams["scoped"].assert_not_called()
    resume_seams["indexer"].assert_called_once()


# ── Route contract ───────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch, store, runtime):
    """Flask test client with the wiki blueprint mounted on the stub runtime."""
    monkeypatch.setenv("MEWBO_MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    routes_mod.register(flask_app, runtime)
    yield flask_app.test_client()
    # ``register`` sets a module-level runtime handle; leaving the MagicMock in
    # place leaks the stub into every later test that imports this module.
    routes_mod._runtime = None


def _post_refresh(client, body=None, slug: str = "bearlike%2FAssistant"):
    kwargs = {"headers": {"X-Api-Key": API_KEY}}
    if body is not None:
        kwargs["json"] = body
    return client.post(f"/v1/wiki/projects/{slug}/refresh", **kwargs)


def test_route_absent_body_means_auto(client, store, seams):
    """The route predates having a body at all — sending none must still work.

    An eligible project proves the default is genuinely ``auto`` rather than a
    default that merely parses: a ``full`` default would reach the indexer.
    """
    _seed_project(store, commit_sha="abc123", fingerprint=CURRENT)

    resp = _post_refresh(client)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["queued"] is True
    assert body["jobId"]
    assert body["refresh"] == {"path": "scoped", "reason": None, "mismatches": []}
    seams["scoped"].assert_called_once()
    seams["indexer"].assert_not_called()


def test_route_empty_body_means_auto(client, store, seams):
    """``{}`` is the same as no body — the console sends one or the other."""
    _seed_project(store, commit_sha="abc123", fingerprint=CURRENT)

    resp = _post_refresh(client, {})

    assert resp.status_code == 200
    assert resp.get_json()["refresh"]["path"] == "scoped"


def test_route_explicit_full_forces_a_rebuild(client, store, seams):
    """``full`` is the escape hatch — an eligible project rebuilds anyway."""
    _seed_project(store, commit_sha="abc123", fingerprint=CURRENT)

    resp = _post_refresh(client, {"mode": "full"})

    assert resp.status_code == 200
    assert resp.get_json()["refresh"] == {
        "path": "full", "reason": "requested", "mismatches": [],
    }
    seams["indexer"].assert_called_once()
    seams["scoped"].assert_not_called()


@pytest.mark.parametrize(
    "body", [{"mode": "nonsense"}, {"mode": "scoped"}, {"unknown": 1}, {"mode": None}]
)
def test_route_refuses_a_malformed_body_and_starts_nothing(client, store, seams, body):
    """A bad body is a 400 naming the field, never a silent fall-through to ``auto``.

    ``{"mode": "scoped"}`` is in the set deliberately: ``scoped`` is a real
    ``RefreshPath`` but not a ``RefreshMode`` — a caller cannot DEMAND reuse of
    artifacts nothing fingerprinted, so asking for it must fail loudly rather
    than being quietly honoured as ``auto``.
    """
    _seed_project(store, commit_sha="abc123", fingerprint=CURRENT)

    resp = _post_refresh(client, body)

    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["code"] == "validation"
    assert payload.get("fields")
    # Refusing must also mean refusing to WORK — a 400 that already queued an
    # index would be the expensive half of a fail-open.
    seams["indexer"].assert_not_called()
    seams["scoped"].assert_not_called()


def test_route_rejects_a_catalog_project_before_any_decision(client, store, seams):
    """A non-git project is refused ahead of the decision — hence no ``catalog`` reason.

    ``RefreshDecision`` deliberately has no member for this case, so the guard
    running FIRST is what keeps that vocabulary honest rather than incomplete.
    """
    project = Project(
        slug="docs/handbook",
        source="github",
        lang="en",
        indexed_at="2026-01-01T00:00:00Z",
        pages=3,
        desc="Catalog",
        repoUrl=None,
    )
    store.create_project(project)

    resp = _post_refresh(client, {"mode": "auto"}, slug="docs%2Fhandbook")

    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"
    assert "refresh" in resp.get_json()["message"]
    seams["indexer"].assert_not_called()
    seams["scoped"].assert_not_called()


def test_route_unknown_project_is_404_not_a_decision(client, seams):
    resp = _post_refresh(client, {"mode": "auto"}, slug="no%2Fsuch")

    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"
