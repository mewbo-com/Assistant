"""``wiki.refresh.default_mode`` — the operator setting a refresh consults.

The property under test is that the SETTING is reachable, and the only way to
prove that is to leave ``mode`` off the request: a caller that names a mode is
indistinguishable from a default that merely parses. So every case here goes
through the real route with a real config file (the autouse ``app_config_file``
fixture writes one and points the loader at it), and asserts on the ENGINE that
was reached — ``_start_scoped_refresh`` vs ``_start_indexer_session`` — because
the returned job looks identical either way.

Nothing here mocks ``get_config_value``: mocking the read would assert that the
route calls a function, not that an operator's config file changes what a
refresh does. The one I/O boundary stubbed is ``_current_index_fingerprint``
(installed package metadata + ``shutil.which``), pinned so an eligible project
stays eligible, plus the two start seams so no engine actually runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mewbo_api.wiki.routes import _CONFIG_REFRESH_MODES, RefreshProjectRequest
from mewbo_core.config import get_config_value, set_app_config_path
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexFingerprint, Project

API_KEY = "test-key-123"

# What the probe reports "a refresh started now would build with". Seeding a
# project stamped with this exact value is what makes it scoped-ELIGIBLE, so a
# full rebuild in any case below can only be explained by the mode.
CURRENT = IndexFingerprint(
    embeddingModel="fake/embedding-3",
    graphSchemaVersion="1",
    grammarPackVersion="1.12.0",
    resolverAvailable=True,
)

SLUG = "bearlike/Assistant"
SLUG_PATH = "bearlike%2FAssistant"


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
    """Record which engine the refresh reached, without running either."""
    with (
        patch("mewbo_api.wiki.jobs._current_index_fingerprint", return_value=CURRENT),
        patch("mewbo_api.wiki.jobs._start_indexer_session") as indexer,
        patch("mewbo_api.wiki.jobs._start_scoped_refresh") as scoped,
    ):
        yield {"indexer": indexer, "scoped": scoped}


@pytest.fixture()
def client(monkeypatch, runtime):
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


@pytest.fixture()
def set_default_mode(app_config_file: Path):
    """Rewrite ``wiki.refresh.default_mode`` in the live config file.

    Writes the real file the loader is pointed at, so the route reads the value
    through the same chain a deployment does.

    **Re-point the loader; never ``reset_config()`` here.** That helper clears
    ``_APP_CONFIG_PATH_OVERRIDE`` along with the cache, so the next read falls
    back to the repo's own ``configs/app.json`` — which carries the shipped
    default and therefore makes a test asserting the default look like it
    passed while reading a file it never wrote. ``set_app_config_path`` drops
    the cache too, without unbinding the path.
    """

    def _set(mode: str) -> None:
        payload = json.loads(app_config_file.read_text())
        payload["wiki"]["refresh"]["default_mode"] = mode
        app_config_file.write_text(json.dumps(payload))
        set_app_config_path(app_config_file)
        assert get_config_value("wiki", "refresh", "default_mode") == mode

    return _set


def _seed_eligible_project(store: JsonWikiStore) -> None:
    """A completed, fingerprint-matching index — the scoped-eligible case.

    ``repoUrl`` is load-bearing: a project with no clone URL and no git
    submission reads as a CATALOG project and the route refuses it before a
    decision is ever computed, so omitting it would make every case below
    assert the catalog guard instead of the mode.
    """
    store.create_project(
        Project(
            slug=SLUG,
            source="github",
            lang="en",
            indexed_at="2026-01-01T00:00:00Z",
            pages=5,
            desc="Test repo",
            repoUrl=f"https://github.com/{SLUG}",
            commitSha="abc123",
            fingerprint=CURRENT,
            graphOnly=False,
        )
    )


def _post_refresh(client, body=None):
    kwargs = {"headers": {"X-Api-Key": API_KEY}}
    if body is not None:
        kwargs["json"] = body
    return client.post(f"/v1/wiki/projects/{SLUG_PATH}/refresh", **kwargs)


# ── The setting is honoured when the caller names no mode ────────────────────


@pytest.mark.parametrize("body", [None, {}], ids=["no-body", "empty-body"])
def test_configured_full_rebuilds_when_no_mode_is_requested(
    client, store, seams, set_default_mode, body
):
    """``default_mode: "full"`` reaches the indexer on a scoped-ELIGIBLE project.

    Eligibility is what makes this a real assertion: ``auto`` would take the
    scoped path here, so the indexer session can only be explained by the
    setting. Both spellings of "the caller named nothing" are covered — the
    console sends no body, other callers send ``{}``.
    """
    set_default_mode("full")
    _seed_eligible_project(store)

    resp = _post_refresh(client, body)

    assert resp.status_code == 200
    assert resp.get_json()["refresh"]["reason"] == "requested"
    seams["indexer"].assert_called_once()
    seams["scoped"].assert_not_called()


def test_the_mode_map_is_total_over_the_config_literal():
    """Every value the config schema advertises has an explicit mapping.

    This is the tripwire for the actual defect shape: a knob the schema OFFERS
    landing in the unrecognised-value fallback, so an operator sets a mode they
    were told existed and silently gets a different one. Read straight off
    ``WikiRefreshConfig.default_mode``'s own annotation rather than a hand-typed
    list — a literal that gains a member must fail HERE, not in production.
    """
    from typing import get_args

    from mewbo_core.config import WikiRefreshConfig

    advertised = set(get_args(WikiRefreshConfig.model_fields["default_mode"].annotation))
    assert advertised == set(_CONFIG_REFRESH_MODES)
    assert _CONFIG_REFRESH_MODES["incremental"] == "auto"


@pytest.mark.parametrize("configured", ["auto", "incremental"])
def test_configured_incremental_and_auto_both_take_the_scoped_path(
    client, store, seams, set_default_mode, configured
):
    """``incremental`` maps onto ``auto`` rather than degrading to ``full``.

    The config literal carries a third value ``RefreshMode`` does not, and
    ``auto`` IS the incremental strategy — it takes the scoped delta pass
    wherever reuse is justified. Mapping it to ``full`` would give an operator
    who asked for the cheap path the expensive one. Asserted end-to-end through
    the route, not just on the map above, so the mapping is proven to REACH the
    engine rather than merely to exist.
    """
    set_default_mode(configured)
    _seed_eligible_project(store)

    resp = _post_refresh(client)

    assert resp.status_code == 200
    assert resp.get_json()["refresh"]["path"] == "scoped"
    seams["scoped"].assert_called_once()
    seams["indexer"].assert_not_called()


# ── An explicitly requested mode always wins ─────────────────────────────────


def test_requested_auto_overrides_a_configured_full(client, store, seams, set_default_mode):
    """A caller asking for ``auto`` gets the scoped path under ``default_mode: full``.

    This is the direction that proves the setting is a DEFAULT and not a policy:
    it applies only where the caller expressed no preference.
    """
    set_default_mode("full")
    _seed_eligible_project(store)

    resp = _post_refresh(client, {"mode": "auto"})

    assert resp.status_code == 200
    assert resp.get_json()["refresh"]["path"] == "scoped"
    seams["scoped"].assert_called_once()
    seams["indexer"].assert_not_called()


def test_requested_full_overrides_a_configured_auto(client, store, seams, set_default_mode):
    """The mirror image — ``full`` still rebuilds under ``default_mode: auto``."""
    set_default_mode("auto")
    _seed_eligible_project(store)

    resp = _post_refresh(client, {"mode": "full"})

    assert resp.status_code == 200
    assert resp.get_json()["refresh"]["reason"] == "requested"
    seams["indexer"].assert_called_once()
    seams["scoped"].assert_not_called()


def test_an_unknown_mode_is_still_a_400_not_the_configured_default(
    client, store, seams, set_default_mode
):
    """Making the field optional must not turn a typo into a silent fall-through.

    ``mode: None`` now MEANS something (consult config), so the risk this pins is
    that an unparseable value quietly acquires that meaning instead of being
    refused — the exact failure ``extra="forbid"`` plus the closed literal exist
    to prevent.
    """
    set_default_mode("auto")
    _seed_eligible_project(store)

    resp = _post_refresh(client, {"mode": "scoped"})

    assert resp.status_code == 400
    seams["scoped"].assert_not_called()
    seams["indexer"].assert_not_called()


# ── The model's own half of the decision ─────────────────────────────────────


def test_resolve_mode_consults_the_default_only_when_the_field_is_absent():
    """The wire model decides; the config value arrives as an ARGUMENT.

    Asserted directly because it is the whole reason ``mode`` is optional: an
    omitted field and a requested ``auto`` are different states, and a model
    that defaulted the field at validation could no longer tell them apart.
    """
    assert RefreshProjectRequest().mode is None
    assert RefreshProjectRequest().resolve_mode("full") == "full"
    assert RefreshProjectRequest().resolve_mode("auto") == "auto"
    assert RefreshProjectRequest(mode="auto").resolve_mode("full") == "auto"
    assert RefreshProjectRequest(mode="full").resolve_mode("auto") == "full"
