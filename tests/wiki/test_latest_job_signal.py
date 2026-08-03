"""Regression coverage for two stacked ``/jobs/recoverable`` defects.

``GET /v1/wiki/jobs/recoverable`` used to keep serving a ``cancelled``/
``failed`` job as still-worth-resuming long after a LATER index for the same
slug had already completed, or after resume would refuse it outright:

- **The supersession gap** — the route never checked whether a newer attempt had
  already succeeded, and both store drivers ordered "latest job for a slug"
  by ``job_id`` — a uuid4 hex, which sorts randomly with respect to when a
  job actually ran — instead of ``phase_started_at``. Pinned below: the
  ordering bug at the store primitive (``WikiStoreBase.latest_job``), the
  supersession bug at the route.
- **The resumability mismatch** — the route's own private ``RECOVERABLE_STATUS``
  literal included ``cancelled``, which ``IndexingJob.is_resumable`` explicitly
  excludes (a deliberate user stop is not a resume candidate) — so the
  surface advertised jobs that ``POST .../resume`` then refused with a
  ``validation`` error. Pinned below: the route now calls ``is_resumable``
  instead of re-spelling the status set, and ``DELETE /v1/wiki/index/<id>``
  now shares the one ``_job_wire`` serialiser every other job response uses.
"""
from __future__ import annotations

from mewbo_graph.wiki.types import IndexingJob

from .test_routes import (  # noqa: F401  (fixtures + helper, reused by name)
    API_KEY,
    _seed_resumable_job,
    client,
    runtime_stub,
    store,
    valid_submission,
    wiki_app,
)

SLUG = "org/repo"


def _job(job_id: str, *, status: str, phase_started_at: str | None, **extra) -> IndexingJob:
    return IndexingJob(
        jobId=job_id,
        slug=SLUG,
        status=status,
        scannedCount=0,
        totalCount=0,
        currentFile=None,
        phaseStartedAt=phase_started_at,
        **extra,
    )


# ── Store primitive: ``latest_job`` must never let job_id decide "latest" ──


def test_latest_job_orders_by_phase_started_at_not_job_id(store):  # noqa: F811
    """A uuid4 ``job_id`` that sorts BEFORE an older job's id must not win.

    Chosen so a job_id sort would pick the WRONG (older) job — only
    ``phase_started_at`` may decide which attempt is "latest".
    """
    store.create_job(_job("aaaa-newer", status="complete", phase_started_at="2026-06-01T00:00:00Z"))
    store.create_job(_job("zzzz-older", status="complete", phase_started_at="2026-01-01T00:00:00Z"))

    latest = store.latest_job(SLUG)
    assert latest is not None
    assert latest.job_id == "aaaa-newer"


def test_latest_job_status_filter_and_missing_timestamp_sorts_oldest(store):  # noqa: F811
    """``statuses`` narrows the candidates; a job with no ``phase_started_at``
    (never even cloned) never outranks one that has, and an unmatched slug or
    status returns ``None`` rather than raising."""
    store.create_job(_job("never-cloned", status="cancelled", phase_started_at=None))
    store.create_job(
        _job("real-complete", status="complete", phase_started_at="2026-01-01T00:00:00Z")
    )

    assert store.latest_job(SLUG).job_id == "real-complete"
    assert store.latest_job(SLUG, statuses={"cancelled"}).job_id == "never-cloned"
    assert store.latest_job(SLUG, statuses={"failed"}) is None
    assert store.latest_job("no/such/slug") is None


# ── Route: a later success must suppress an earlier terminal signal ───────


def test_recoverable_jobs_excludes_one_superseded_by_a_later_success(client):  # noqa: F811
    """A cancelled job with reusable checkpoints must drop off
    ``/jobs/recoverable`` once a LATER index for the same slug completed —
    this is the reported defect: the landing card kept reporting a stale
    cancellation after a fresh index had already succeeded."""
    c, job_store = client
    job_id = _seed_resumable_job(job_store, job_id="cancelled-old", slug=SLUG, status="cancelled")
    job_store.update_job(job_id, phase_started_at="2026-01-01T00:00:00Z")
    job_store.create_job(_job(
        "complete-new", status="complete",
        phase_started_at="2026-06-01T00:00:00Z", commitSha="freshsha",
    ))

    resp = c.get("/v1/wiki/jobs/recoverable", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    ids = {j["jobId"] for j in resp.get_json()}
    assert "cancelled-old" not in ids


def test_recoverable_jobs_keeps_an_attempt_newer_than_the_last_success(client):  # noqa: F811
    """Supersession compares timestamps, not "any complete job exists" — a
    stopped attempt genuinely newer than the slug's last completed index (a
    second try that failed after a prior success) must still be offered."""
    c, job_store = client
    job_store.create_job(_job(
        "complete-old", status="complete",
        phase_started_at="2026-01-01T00:00:00Z", commitSha="oldsha",
    ))
    job_id = _seed_resumable_job(
        job_store, job_id="interrupted-new", slug=SLUG, status="interrupted"
    )
    job_store.update_job(job_id, phase_started_at="2026-06-01T00:00:00Z")

    resp = c.get("/v1/wiki/jobs/recoverable", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    ids = {j["jobId"] for j in resp.get_json()}
    assert "interrupted-new" in ids


# ── Route: never advertise a job resume would refuse ──────────────────────


def test_recoverable_jobs_excludes_cancelled_even_with_no_later_index(client):  # noqa: F811
    """A ``cancelled`` job with reusable checkpoints must NEVER appear, even
    with no later index for the slug at all — isolates the status-set fix
    from the supersession fix: this failure mode has no "later success" in
    play, only ``cancelled`` not being a genuine resume candidate
    (``IndexingJob.is_resumable`` excludes it; ``POST .../resume`` refuses
    it with a ``validation`` error today)."""
    c, job_store = client
    _seed_resumable_job(
        job_store, job_id="cancelled-only", slug="org/cancelled-only", status="cancelled"
    )

    resp = c.get("/v1/wiki/jobs/recoverable", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    ids = {j["jobId"] for j in resp.get_json()}
    assert "cancelled-only" not in ids


def test_recoverable_jobs_keeps_failed_with_no_later_index(client):  # noqa: F811
    """Regression guard against over-exclusion: a ``failed`` job with reusable
    checkpoints and no later index for its slug is a genuine resume
    candidate and must still be offered."""
    c, job_store = client
    _seed_resumable_job(job_store, job_id="failed-only", slug="org/failed-only", status="failed")

    resp = c.get("/v1/wiki/jobs/recoverable", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    ids = {j["jobId"] for j in resp.get_json()}
    assert "failed-only" in ids


def test_delete_index_response_matches_job_wire_shape(client, runtime_stub, valid_submission):  # noqa: F811
    """``DELETE /v1/wiki/index/<id>`` must route through the same ``_job_wire``
    seam every other job response uses, so its payload carries ``isActive``
    like ``GET /index/<id>`` and ``GET /jobs/active`` do — previously it
    returned a bare ``model_dump`` missing that (and any ``sessionId``)."""
    c, _ = client
    create = c.post("/v1/wiki/index", json=valid_submission, headers={"X-Api-Key": API_KEY})
    job_id = create.get_json()["jobId"]

    resp = c.delete(f"/v1/wiki/index/{job_id}", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "cancelled"
    # A cancelled job is not active — the same flag GET /index/<id> would report.
    assert body["isActive"] is False
