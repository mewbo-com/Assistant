"""The on-demand wiki MAINTAINER session: ctx tier, mint, and endpoint.

Three surfaces, one feature: ``resolve_job_ctx``'s second tier (a session bound
to a project rather than to an indexing job), ``WikiMaintainerSession.open``'s
get-or-create, and the route that exposes it.

Two assertions here carry more weight than the happy paths. Indexing must be
untouched, so a session that HAS a job never reaches the maintainer tier and the
ctx it gets is unchanged. And the tier is gated on a server-stamped TAG, never
on the ``slug`` context key a caller can write for itself — the pair of tests
naming that property is what stops a later "simplification" back to the key.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.wiki.types import IndexingJob, Project

API_KEY = "test-key-123"
SLUG = "git.example.com/acme/beacon"


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeSessionStore:
    """Just the transcript surface the ctx resolvers read."""

    def __init__(self) -> None:
        self.transcripts: dict[str, list[dict]] = {}
        self.tags: dict[str, str] = {}

    def load_transcript(self, session_id: str) -> list[dict]:
        return self.transcripts.get(session_id, [])

    def tags_for_session(self, session_id: str) -> list[str]:
        return [t for t, sid in self.tags.items() if sid == session_id]

    def resolve_tag(self, tag: str) -> str | None:
        return self.tags.get(tag)

    def append_context(self, session_id: str, payload: dict) -> None:
        self.transcripts.setdefault(session_id, []).append(
            {"type": "context", "payload": payload}
        )


class _FakeRuntime:
    """A runtime double carrying both stores plus the session-mint seam."""

    def __init__(self, wiki_store) -> None:
        self.wiki_store = wiki_store
        self.session_store = _FakeSessionStore()
        self.terminated: set[str] = set()
        self._next = 0

    # -- ctx side -----------------------------------------------------------
    def bind_slug(self, session_id: str, slug: str) -> None:
        """Write a bare ``slug`` context key — what an untrusted caller can do."""
        self.session_store.append_context(session_id, {"slug": slug})

    def stamp_maintainer(self, session_id: str, slug: str) -> None:
        """Stamp the server-only maintainer tag — what authorizes the binding."""
        self.tag_session(session_id, f"wiki:maintain:{slug}")

    # -- mint side ----------------------------------------------------------
    def resolve_session(self, **kwargs) -> str:
        self._next += 1
        return f"sess-{self._next}"

    def tag_session(self, session_id: str, tag: str) -> None:
        self.session_store.tags[tag] = session_id

    def append_context_event(self, session_id: str, payload: dict) -> None:
        self.session_store.append_context(session_id, payload)

    def is_terminated(self, session_id: str) -> bool:
        return session_id in self.terminated


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def runtime(store) -> _FakeRuntime:
    return _FakeRuntime(store)


def _seed_project(store, slug: str = SLUG, commit_sha: str = "c0ffee1") -> Project:
    project = Project(
        slug=slug,
        source="github",
        lang="en",
        indexed_at="2026-01-01T00:00:00Z",
        pages=3,
        desc="Fictional service",
        commit_sha=commit_sha,
    )
    store.create_project(project)
    return project


def _seed_job(store, *, job_id: str = "job-1", slug: str = SLUG) -> IndexingJob:
    job = IndexingJob(
        job_id=job_id,
        slug=slug,
        status="queued",
        scanned_count=0,
        total_count=0,
        current_file=None,
        commit_sha="deadbee",
    )
    store.create_job(job)
    return job


# ── resolve_job_ctx: the job tier still wins ─────────────────────────────────


def test_job_binding_wins_over_a_maintainer_tag(store, runtime):
    """A session with BOTH a job and the tag resolves the JOB — indexing intact."""
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    _seed_project(store)
    _seed_job(store)
    store.attach_job_session("job-1", "sess-index")
    runtime.stamp_maintainer("sess-index", SLUG)

    ctx = resolve_job_ctx("sess-index", runtime)

    assert ctx is not None
    assert ctx.job_bound is True
    assert ctx.job_id == "job-1"
    # The job's own commit and checkout, never the project's.
    assert ctx.commit_sha == "deadbee"
    assert ctx.clone_dir.name == "job-1"


def test_maintainer_tier_reached_only_when_no_job_is_attached(store, runtime):
    """No job on the session ⇒ the project-bound ctx, explicitly not job-bound."""
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    _seed_project(store)
    runtime.stamp_maintainer("sess-maint", SLUG)

    ctx = resolve_job_ctx("sess-maint", runtime)

    assert ctx is not None
    assert ctx.job_bound is False
    assert ctx.job_id == ""
    assert ctx.slug == SLUG
    assert ctx.resume_plan is None
    # Stamped from the PROJECT — the generation the wiki currently describes.
    assert ctx.commit_sha == "c0ffee1"
    # No completed index's checkout survives, so the placeholder stands in — a
    # real Path (never the CWD) that fails every ``exists()`` guard.
    assert not ctx.clone_dir.exists()


def test_maintainer_binding_also_grounds_the_READ_tools(store, runtime):
    """The read side resolves the same binding — write-without-read is the bug."""
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    _seed_project(store)
    runtime.stamp_maintainer("sess-maint", SLUG)

    qa = resolve_qa_ctx("sess-maint", runtime)

    assert qa is not None
    assert qa.slug == SLUG
    # A slug-only ctx: the QA emit/event tools keep guarding on ``answer_id``.
    assert qa.answer_id is None


def test_read_side_job_binding_still_wins(store, runtime):
    """An indexing session's read ctx is unchanged by the new tier below it."""
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    _seed_project(store, slug="git.example.com/acme/other")
    _seed_project(store)
    _seed_job(store, slug=SLUG)
    store.attach_job_session("job-1", "sess-index")
    runtime.stamp_maintainer("sess-index", "git.example.com/acme/other")

    qa = resolve_qa_ctx("sess-index", runtime)

    assert qa is not None
    assert qa.slug == SLUG


def test_tag_naming_no_indexed_project_resolves_nothing(store, runtime):
    """An unindexed slug is not a binding — a page write must not land nowhere."""
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    runtime.stamp_maintainer("sess-maint", "git.example.com/acme/never-indexed")

    assert resolve_job_ctx("sess-maint", runtime) is None


def test_plain_session_resolves_nothing(store, runtime):
    """A session with neither a job nor a slug is unchanged: still ``None``."""
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    _seed_project(store)

    assert resolve_job_ctx("sess-plain", runtime) is None


def test_a_bare_slug_context_key_authorizes_NOTHING(store, runtime):
    """The security property: a context key is not a binding, the TAG is.

    ``backend.py``'s ``_build_context_payload`` merges a request's ``context``
    verbatim, so any caller can put a ``slug`` on a session it owns. Reading one
    here would let a client advertising the ``wiki`` capability name any project
    and reach that project's page-write tools. This asserts the PROPERTY — a
    slug key with no tag resolves nothing — so it would still fail if the tag
    check were deleted, which "no ctx for a plain session" would not.
    """
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx, resolve_qa_ctx

    _seed_project(store)
    runtime.bind_slug("sess-forged", SLUG)

    assert resolve_job_ctx("sess-forged", runtime) is None
    assert resolve_qa_ctx("sess-forged", runtime) is None

    # The same session, once the SERVER stamps the tag, resolves — which is what
    # proves the refusal above is about authorization and not about the fixture.
    runtime.stamp_maintainer("sess-forged", SLUG)
    ctx = resolve_job_ctx("sess-forged", runtime)
    assert ctx is not None and ctx.slug == SLUG


def test_the_tag_carries_the_address_so_a_later_turn_cannot_re_point_it(store, runtime):
    """A context key rewritten mid-session cannot move an authorized binding."""
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    _seed_project(store, slug="git.example.com/acme/one")
    _seed_project(store, slug="git.example.com/acme/two")
    runtime.stamp_maintainer("sess-maint", "git.example.com/acme/one")
    # A later request smuggles a different project onto the same session.
    runtime.bind_slug("sess-maint", "git.example.com/acme/two")

    ctx = resolve_job_ctx("sess-maint", runtime)

    assert ctx is not None
    assert ctx.slug == "git.example.com/acme/one"


def test_jobless_ctx_writes_no_job_events(store, runtime):
    """The phase/log emitters are silent without a job — no orphan event log."""
    from mewbo_graph.plugins.wiki._ctx import emit_log, emit_phase, resolve_job_ctx

    _seed_project(store)
    runtime.stamp_maintainer("sess-maint", SLUG)
    ctx = resolve_job_ctx("sess-maint", runtime)
    assert ctx is not None

    emit_phase(ctx, "pages")
    emit_log(ctx, "wrote a page")

    assert store.load_job_events("") == []


# ── WikiMaintainerSession.open ───────────────────────────────────────────────


def test_open_mints_a_bound_session(store, runtime):
    """The mint writes the whole purpose binding — and no job row."""
    from mewbo_api.wiki.jobs import MAINTAIN_TOOLS, WikiMaintainerSession

    _seed_project(store)

    session_id, created = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store)

    assert created is True
    assert runtime.session_store.tags[f"wiki:maintain:{SLUG}"] == session_id
    payload = runtime.session_store.transcripts[session_id][-1]["payload"]
    assert payload["slug"] == SLUG
    assert payload["client_capabilities"] == ["wiki"]
    assert payload["mcp_tools"] == MAINTAIN_TOOLS
    assert payload["strict_tool_scope"] is True
    assert "wiki_finalize" not in payload["mcp_tools"]
    assert payload["skill_instructions"]
    # No sham job: nothing was created for this slug to attach to.
    assert store.list_jobs(slug=SLUG) == []


def test_open_is_idempotent(store, runtime):
    """A second call resolves the SAME session rather than accumulating one."""
    from mewbo_api.wiki.jobs import WikiMaintainerSession

    _seed_project(store)

    first, created_first = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store)
    second, created_second = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store)

    assert (created_first, created_second) == (True, False)
    assert first == second


def test_open_replaces_a_terminated_session(store, runtime):
    """Termination is one-way, so the tag is re-pointed at a fresh session."""
    from mewbo_api.wiki.jobs import WikiMaintainerSession

    _seed_project(store)
    first, _ = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store)
    runtime.terminated.add(first)

    second, created = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store)

    assert created is True
    assert second != first
    assert runtime.session_store.tags[f"wiki:maintain:{SLUG}"] == second


def test_maintainer_session_resolves_a_write_ctx(store, runtime):
    """End to end: the mint's context is what the ctx tier reads back."""
    from mewbo_api.wiki.jobs import WikiMaintainerSession
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    _seed_project(store)
    session_id, _ = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store)

    ctx = resolve_job_ctx(session_id, runtime)

    assert ctx is not None
    assert ctx.slug == SLUG
    assert ctx.job_bound is False


# ── open(fresh=True): an ADDITIONAL session, not the canonical one ───────────


def test_fresh_mints_a_distinct_session_every_time(store, runtime):
    """The defect this exists to close: a composer asked for a NEW session."""
    from mewbo_api.wiki.jobs import WikiMaintainerSession

    _seed_project(store)

    first, created_first = WikiMaintainerSession.open(
        SLUG, runtime=runtime, store=store, fresh=True
    )
    second, created_second = WikiMaintainerSession.open(
        SLUG, runtime=runtime, store=store, fresh=True
    )

    assert (created_first, created_second) == (True, True)
    assert first != second


def test_a_fresh_mint_does_not_steal_the_canonical_tag(store, runtime):
    """A tag maps to ONE session, so a second claimant would revoke the first.

    The canonical three-segment tag must keep resolving the canonical session
    after any number of fresh mints — that tag is what the project card's "open"
    button resolves, and it is the authorization the first session holds.
    """
    from mewbo_api.wiki.jobs import WikiMaintainerSession

    _seed_project(store)
    canonical, _ = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store)

    fresh, _ = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store, fresh=True)

    assert fresh != canonical
    assert runtime.session_store.resolve_tag(f"wiki:maintain:{SLUG}") == canonical
    # And the canonical session still HOLDS it — a steal shows up here, not above.
    assert f"wiki:maintain:{SLUG}" in runtime.session_store.tags_for_session(canonical)
    # The fresh session's own tag is keyed by its id, so it can never collide.
    assert runtime.session_store.tags_for_session(fresh) == [
        f"wiki:maintain:{SLUG}:{fresh}"
    ]


def test_the_default_path_still_reuses_after_a_fresh_mint(store, runtime):
    """The "open" button is untouched: it resolves the canonical session."""
    from mewbo_api.wiki.jobs import WikiMaintainerSession

    _seed_project(store)
    canonical, _ = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store)
    WikiMaintainerSession.open(SLUG, runtime=runtime, store=store, fresh=True)

    again, created = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store)

    assert (again, created) == (canonical, False)


def test_a_fresh_session_resolves_a_write_ctx(store, runtime):
    """The four-segment tag authorizes identically — the whole point of the shape."""
    from mewbo_api.wiki.jobs import WikiMaintainerSession
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    _seed_project(store)
    session_id, _ = WikiMaintainerSession.open(
        SLUG, runtime=runtime, store=store, fresh=True
    )

    ctx = resolve_job_ctx(session_id, runtime)

    assert ctx is not None
    assert ctx.slug == SLUG
    assert ctx.job_bound is False


def test_a_fresh_session_carries_the_same_purpose_binding(store, runtime):
    """One mint, so the capability, ceiling and playbook cannot drift."""
    from mewbo_api.wiki.jobs import MAINTAIN_TOOLS, WikiMaintainerSession

    _seed_project(store)
    canonical, _ = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store)
    fresh, _ = WikiMaintainerSession.open(SLUG, runtime=runtime, store=store, fresh=True)

    canonical_ctx = runtime.session_store.transcripts[canonical][-1]["payload"]
    fresh_ctx = runtime.session_store.transcripts[fresh][-1]["payload"]

    assert fresh_ctx == canonical_ctx
    assert fresh_ctx["client_capabilities"] == ["wiki"]
    assert fresh_ctx["mcp_tools"] == MAINTAIN_TOOLS
    assert fresh_ctx["strict_tool_scope"] is True
    assert fresh_ctx["slug"] == SLUG


# ── POST /v1/wiki/projects/<slug>/session ────────────────────────────────────


@pytest.fixture()
def client(tmp_path: Path, monkeypatch, store, runtime):
    """Flask test app with the wiki blueprint mounted over the fake runtime."""
    monkeypatch.setenv("MEWBO_MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    routes_mod.register(flask_app, runtime)

    yield flask_app.test_client()

    routes_mod._runtime = None


def _post(client, slug: str):
    return client.post(
        f"/v1/wiki/projects/{slug}/session", headers={"X-API-KEY": API_KEY}
    )


def test_endpoint_requires_auth(client):
    assert _post_unauthenticated(client).status_code == 401


def _post_unauthenticated(client):
    return client.post(f"/v1/wiki/projects/{SLUG}/session")


def test_endpoint_404s_on_an_unindexed_slug(client, store):
    resp = _post(client, "git.example.com/acme/never-indexed")

    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"


def test_endpoint_is_idempotent(client, store):
    _seed_project(store)

    first = _post(client, SLUG).get_json()
    second = _post(client, SLUG).get_json()

    assert first["created"] is True
    assert second["created"] is False
    assert first["sessionId"] == second["sessionId"]


def _post_fresh(client, slug: str, body: dict):
    return client.post(
        f"/v1/wiki/projects/{slug}/session", headers={"X-API-KEY": API_KEY}, json=body
    )


def test_endpoint_fresh_never_reuses(client, store, runtime):
    """The composer's shape: every call is a session of its own."""
    _seed_project(store)

    first = _post_fresh(client, SLUG, {"newSession": True}).get_json()
    second = _post_fresh(client, SLUG, {"newSession": True}).get_json()

    assert first["created"] is True and second["created"] is True
    assert first["sessionId"] != second["sessionId"]
    # And neither took the canonical tag away from a later default call.
    canonical = _post(client, SLUG).get_json()
    assert canonical["created"] is True
    assert canonical["sessionId"] not in (first["sessionId"], second["sessionId"])


def test_endpoint_fresh_false_is_the_default_reuse(client, store):
    """An explicit false is the "open" button's behaviour, unchanged."""
    _seed_project(store)

    first = _post_fresh(client, SLUG, {"newSession": False}).get_json()
    second = _post(client, SLUG).get_json()

    assert first["sessionId"] == second["sessionId"]
    assert second["created"] is False


def test_endpoint_refuses_an_unknown_body_field(client, store):
    """``extra="forbid"``: a misspelled field must not silently reuse a session."""
    _seed_project(store)

    resp = _post_fresh(client, SLUG, {"nweSession": True})

    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"
