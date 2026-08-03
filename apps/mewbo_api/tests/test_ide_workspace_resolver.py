"""Tests for the Web IDE mount resolver's tiers (``ide_routes``).

Drives each tier from the caller site with real objects — a real ``AppSpec`` in a
real ``JsonAppStore``, a real on-disk wiki checkout under a tmp clone root, real
``IdeWorkspace`` validation — and stubs only what is genuinely I/O the tier does
not own: the session transcript/tag reads and the wiki store.

What the three tiers must guarantee, and what these assert:

* the CONFIG tier is unchanged and still wins first;
* a wiki maintainer session mounts its project's surviving checkout, addressed by
  the server-stamped ``wiki:maintain:<slug>`` TAG — a context ``slug`` key alone
  resolves NOTHING, because any caller can write one;
* a wiki project whose checkout is gone refuses with a specific sentence rather
  than falling through to the broker's undiagnosable ``workspace_denied``;
* an app maintainer session mounts its app's staging directory, MATERIALIZED on
  demand (staging is ephemeral, so a tier that only mounted an existing directory
  would work almost never).
"""

# mypy: ignore-errors
# ruff: noqa: D103
from __future__ import annotations

from types import SimpleNamespace

import pytest
from mewbo_api.apps import store as store_mod
from mewbo_api.apps.models import AppFrontend, AppSpec, WorkspaceRef
from mewbo_api.apps.store import JsonAppStore
from mewbo_api.ide_routes import (
    AppStagingMount,
    ConfigProjectMount,
    IdeWorkspaceResolver,
    IdeWorkspaceUnavailable,
    WikiCheckoutMount,
)

SESSION_ID = "b" * 32
SLUG = "git.example.com/acme/beacon"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _runtime(*, tags=(), context_event=None):
    """A session runtime stand-in exposing only the two reads the tiers make."""
    return SimpleNamespace(
        session_store=SimpleNamespace(
            tags_for_session=lambda _sid: list(tags),
            latest_event_of_type=lambda *_a, **_kw: context_event,
        )
    )


class _FakeWikiStore:
    """The two reads ``WikiJobCtx.for_maintainer`` makes, and nothing else."""

    def __init__(self, *, project: object | None, jobs: list[object]) -> None:
        self._project = project
        self._jobs = jobs

    def get_project(self, slug: str) -> object | None:
        return self._project if slug == SLUG else None

    def list_jobs(self, *, slug: str) -> list[object]:
        return list(self._jobs) if slug == SLUG else []

    def find_job_by_session(self, _session_id: str) -> None:
        return None


def _app(*, maintainer: str = SESSION_ID) -> AppSpec:
    return AppSpec(
        app_id="app1",
        title="Beacon Dashboard",
        summary="Renders the daily rollup.",
        owner_session_id="owner-sess",
        maintainer_session_id=maintainer,
        workspace_ref=WorkspaceRef(kind="own", key="k"),
        frontend=AppFrontend(
            files={
                "app.py": "import streamlit as st\n",
                "pipelines/ingest.py": "def run(params, ctx):\n    return {}\n",
            }
        ),
        status="live",
    )


# ---------------------------------------------------------------------------
# tier 1 — the configured project
# ---------------------------------------------------------------------------


def test_config_tier_resolves_a_configured_project(monkeypatch) -> None:
    monkeypatch.setattr(
        "mewbo_core.config.get_config",
        lambda: SimpleNamespace(projects={"demo": SimpleNamespace(path="/tmp/demo")}),
    )
    runtime = _runtime(context_event={"type": "context", "payload": {"project": "demo"}})
    workspace = ConfigProjectMount().resolve(SESSION_ID, runtime)
    assert workspace is not None
    assert (workspace.project_name, workspace.project_path) == ("demo", "/tmp/demo")


# ---------------------------------------------------------------------------
# tier 2 — the wiki checkout
# ---------------------------------------------------------------------------


def _wire_wiki(monkeypatch, store) -> None:
    """Point the tier's down-only seam at *store* (it imports it per call)."""
    monkeypatch.setattr(
        "mewbo_graph.plugins.wiki._ctx.resolve_runtime",
        lambda: SimpleNamespace(wiki_store=store, session_store=None),
    )


def test_wiki_tier_mounts_the_surviving_checkout(monkeypatch, tmp_path) -> None:
    clone_root = tmp_path / "clones"
    (clone_root / "job-1").mkdir(parents=True)
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(clone_root))
    store = _FakeWikiStore(
        project=SimpleNamespace(slug=SLUG, commit_sha="abc"),
        jobs=[SimpleNamespace(job_id="job-1", slug=SLUG, status="complete")],
    )
    _wire_wiki(monkeypatch, store)

    workspace = WikiCheckoutMount().resolve(
        SESSION_ID, _runtime(tags=[f"wiki:maintain:{SLUG}"])
    )
    assert workspace is not None
    # The display name is the slug, never the uuid-named clone directory.
    assert workspace.project_name == SLUG
    assert workspace.project_path == str(clone_root / "job-1")


def test_wiki_tier_refuses_when_no_checkout_survives(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _FakeWikiStore(project=SimpleNamespace(slug=SLUG), jobs=[])
    _wire_wiki(monkeypatch, store)

    with pytest.raises(IdeWorkspaceUnavailable) as excinfo:
        WikiCheckoutMount().resolve(SESSION_ID, _runtime(tags=[f"wiki:maintain:{SLUG}"]))
    message = str(excinfo.value)
    assert SLUG in message
    assert "no checkout on disk" in message


def test_wiki_tier_ignores_a_session_with_no_maintainer_tag(monkeypatch, tmp_path) -> None:
    """A context ``slug`` is not an authorization — only the stamped tag is."""
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _FakeWikiStore(project=SimpleNamespace(slug=SLUG), jobs=[])
    _wire_wiki(monkeypatch, store)

    runtime = _runtime(
        tags=["nextcloud-talk:room:abc"],
        context_event={"type": "context", "payload": {"slug": SLUG}},
    )
    assert WikiCheckoutMount().resolve(SESSION_ID, runtime) is None


# ---------------------------------------------------------------------------
# tier 3 — the app staging directory
# ---------------------------------------------------------------------------


@pytest.fixture
def app_store(tmp_path):
    store = JsonAppStore(root_dir=tmp_path / "apps")
    store_mod.set_stores_for_tests(app_store=store)
    yield store
    store_mod.set_stores_for_tests()


def test_apps_tier_materializes_the_staging_directory(monkeypatch, tmp_path, app_store) -> None:
    monkeypatch.setenv("MEWBO_APPS_ROOT", str(tmp_path / "staging"))
    app = _app()
    app_store.save(app)

    workspace = AppStagingMount().resolve(SESSION_ID, _runtime())
    assert workspace is not None
    # A human-meaningful name, not the app_id and not the path.
    assert workspace.project_name == "Beacon Dashboard"
    staged = tmp_path / "staging" / SESSION_ID / "app1"
    assert workspace.project_path == str(staged)
    # Materialized on demand: the directory did not exist before this call, and
    # the pipeline SOURCE is present, not just the served frontend.
    assert (staged / "app.py").read_text(encoding="utf-8") == "import streamlit as st\n"
    assert (staged / "pipelines" / "ingest.py").exists()


def test_apps_tier_ignores_a_session_bound_to_no_app(monkeypatch, tmp_path, app_store) -> None:
    monkeypatch.setenv("MEWBO_APPS_ROOT", str(tmp_path / "staging"))
    app_store.save(_app(maintainer="someone-else"))
    assert AppStagingMount().resolve(SESSION_ID, _runtime()) is None


# ---------------------------------------------------------------------------
# the resolver's tier ordering + failure isolation
# ---------------------------------------------------------------------------


class _Boom:
    def resolve(self, _session_id, _runtime):
        raise RuntimeError("store is down")


class _Refuses:
    def resolve(self, _session_id, _runtime):
        raise IdeWorkspaceUnavailable("nothing to mount here")


class _Mounts:
    def resolve(self, _session_id, _runtime):
        from mewbo_api.ide_routes import IdeWorkspace

        return IdeWorkspace(project_name="later", project_path="/tmp/later")


def test_a_broken_tier_falls_through_to_the_next() -> None:
    resolver = IdeWorkspaceResolver(tiers=(_Boom(), _Mounts()))
    workspace = resolver.resolve(SESSION_ID, _runtime())
    assert workspace is not None
    assert workspace.project_name == "later"


def test_a_deliberate_refusal_stops_the_walk() -> None:
    resolver = IdeWorkspaceResolver(tiers=(_Refuses(), _Mounts()))
    with pytest.raises(IdeWorkspaceUnavailable):
        resolver.resolve(SESSION_ID, _runtime())


def test_no_tier_binding_is_none_not_a_refusal() -> None:
    class _Blank:
        def resolve(self, _session_id, _runtime):
            return None

    assert IdeWorkspaceResolver(tiers=(_Blank(),)).resolve(SESSION_ID, _runtime()) is None
