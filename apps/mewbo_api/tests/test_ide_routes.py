"""Tests for the ``ide_routes`` Flask-RESTX namespace."""

# mypy: ignore-errors
# ruff: noqa: D101, D102, D103
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from flask import Flask
from flask_restx import Api
from mewbo_api import ide_routes
from mewbo_api.ide import (
    DockerUnavailable,
    IdeInstance,
    MaxLifetimeReached,
)
from mewbo_core.config import ProjectConfig
from mewbo_core.workspaces.project_catalog import ProjectCatalog
from mewbo_core.workspaces.project_store import VirtualProject

UTC = timezone.utc
VALID_SID = "a" * 32
INVALID_SID = "not-a-valid-session-id"


def _make_instance(sid: str = VALID_SID) -> IdeInstance:
    now = datetime.now(UTC)
    return IdeInstance(
        session_id=sid,
        status="ready",
        project_name="demo",
        project_path="/tmp/demo",
        password="pw",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        max_deadline=now + timedelta(hours=8),
        extensions=0,
    )


@pytest.fixture
def fake_manager() -> MagicMock:
    return MagicMock()


@pytest.fixture
def fake_runtime() -> MagicMock:
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = [VALID_SID]
    # The launch path resolves the project through the bounded store read, not
    # a transcript scan — stubbing ``load_transcript`` here would leave the
    # MagicMock answering the real call with a truthy mock.
    rt.session_store.latest_event_of_type.return_value = {
        "type": "context",
        "payload": {"project": "demo"},
    }
    return rt


@pytest.fixture(autouse=True)
def bound_auth_kit(tmp_path) -> Any:
    """Bind an AuthKit for the bare app this module mounts ``ide_ns`` on.

    The ide views declare their requirement with ``@guard.requires``, which
    resolves the process-wide kit at REQUEST time; a bare Flask app runs no
    composition root, so the guards have nothing to resolve. The kit bound here
    is a real one and the guards run for real against it — it accepts the
    injected master credential, and auth-disabled settings make every permission
    pass. It is deliberately NOT a permissive stand-in: ``init_ide`` no longer
    accepts a guard, because a guard that a route never consults is worse than
    no guard at all.

    Rebound to the composed app's kit afterwards so the one process-wide guard
    is never left pointing at this fixture for the rest of the session.
    """
    from mewbo_api import backend
    from mewbo_api.auth import AuthKit
    from mewbo_api.auth.guard_registry import guard_registry
    from mewbo_core.secrets.key_store import KeyStore
    from mewbo_iam import AuthSettings

    guard_registry.bind(
        AuthKit(
            settings=AuthSettings(),
            key_store=KeyStore(path=str(tmp_path / "keys.json")),
            credential_reader=lambda *_a, **_kw: "ide-test-master",
            master_matcher=lambda token: token == "ide-test-master",
        )
    )
    yield
    guard_registry.bind(backend._auth_kit)


class _StubProjectStore:
    """The one store leg the catalog reads — ``list_projects`` and nothing else."""

    def __init__(self, projects: list[VirtualProject] | None = None) -> None:
        self.projects = projects or []

    def list_projects(self) -> list[VirtualProject]:
        return self.projects


@pytest.fixture
def demo_dir(tmp_path) -> Any:
    """A real directory, because the catalog reports availability from disk."""
    path = tmp_path / "demo"
    path.mkdir()
    return path


@pytest.fixture
def project_store() -> _StubProjectStore:
    return _StubProjectStore()


@pytest.fixture
def catalog(demo_dir: Any, project_store: _StubProjectStore) -> ProjectCatalog:
    """A REAL catalog over stubbed stores.

    The catalog is pure enough to construct, so the tier is exercised against
    the same resolver production uses rather than a mock that would agree with
    whatever this module happened to assume about it.
    """
    return ProjectCatalog(
        configured={"demo": ProjectConfig(path=str(demo_dir))},
        project_store=project_store,
    )


@pytest.fixture
def client(
    fake_manager: MagicMock, fake_runtime: MagicMock, catalog: ProjectCatalog
) -> Any:
    app = Flask("ide-test")
    api = Api(app)
    api.add_namespace(ide_routes.ide_ns, path="/api")
    ide_routes.init_ide(fake_manager, fake_runtime, lambda: catalog)
    return app.test_client()


# ---------------------------------------------------------------------------
# POST /ide
# ---------------------------------------------------------------------------


def test_post_creates_returns_201(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.ensure.return_value = (_make_instance(), True)
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["session_id"] == VALID_SID
    assert body["url"] == f"/ide/{VALID_SID}/"
    assert body["project_name"] == "demo"
    assert body["password"] == "pw"


def test_post_reconnect_returns_200(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.ensure.return_value = (_make_instance(), False)
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 200


def test_post_rejects_invalid_session_id(client: Any) -> None:
    resp = client.post(f"/api/sessions/{INVALID_SID}/ide")
    assert resp.status_code == 404


def test_post_404_when_session_unknown(client: Any, fake_runtime: MagicMock) -> None:
    fake_runtime.session_store.list_sessions.return_value = []
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 404


def test_post_409_when_session_has_no_project(client: Any, fake_runtime: MagicMock) -> None:
    # No context event carries a project: the ``payload_key`` narrowing means
    # the store answers ``None`` rather than handing back a projectless event.
    fake_runtime.session_store.latest_event_of_type.return_value = None
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 409


def test_post_asks_the_store_for_the_bounded_read(
    client: Any, fake_runtime: MagicMock
) -> None:
    """The narrowing must reach the STORE, or it is decoration.

    A filter applied after a full read shrinks the response and leaves the work
    proportional to the session's whole history — the defect this replaced.
    """
    client.post(f"/api/sessions/{VALID_SID}/ide")
    fake_runtime.session_store.latest_event_of_type.assert_called_once_with(
        VALID_SID, "context", payload_key="project"
    )
    fake_runtime.session_store.load_transcript.assert_not_called()


def _managed(project_id: str, name: str, path: str) -> VirtualProject:
    return VirtualProject(
        project_id=project_id,
        name=name,
        description="",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        path=path,
    )


def test_post_mounts_a_managed_project_by_key(
    client: Any,
    fake_manager: MagicMock,
    fake_runtime: MagicMock,
    project_store: _StubProjectStore,
    demo_dir: Any,
) -> None:
    """A ``managed:<uuid>`` context key mounts the store's directory.

    The defect this closes: the key resolved everywhere else in the app and was
    refused only here, so every console session anchored to a managed project
    read as "session has no project in context".
    """
    project_store.projects = [_managed("abc-123", "claude-code-plugin", str(demo_dir))]
    fake_runtime.session_store.latest_event_of_type.return_value = {
        "type": "context",
        "payload": {"project": "managed:abc-123"},
    }
    fake_manager.ensure.return_value = (_make_instance(), True)

    resp = client.post(f"/api/sessions/{VALID_SID}/ide")

    assert resp.status_code == 201
    # The DISPLAY name, not the raw key — it is persisted and shown in the console.
    assert fake_manager.ensure.call_args.args == (
        VALID_SID,
        "claude-code-plugin",
        str(demo_dir),
    )


def test_post_409_carries_the_catalog_refusal_verbatim(
    client: Any, fake_runtime: MagicMock, project_store: _StubProjectStore, tmp_path
) -> None:
    """A recognised project whose directory is gone refuses with the catalog's sentence.

    ``unavailable`` means the tier BOUND the session and still cannot mount it,
    so the walk stops here — and the catalog's own message already names the
    directory and the Docker mount rule, which is what a console user acts on.
    """
    missing = tmp_path / "gone"
    project_store.projects = [_managed("abc-123", "vanished", str(missing))]
    fake_runtime.session_store.latest_event_of_type.return_value = {
        "type": "context",
        "payload": {"project": "managed:abc-123"},
    }

    resp = client.post(f"/api/sessions/{VALID_SID}/ide")

    assert resp.status_code == 409
    message = resp.get_json()["message"]
    assert str(missing) in message
    assert "does not exist on this host" in message


def test_post_unknown_project_falls_through_to_the_next_tier(
    client: Any, fake_manager: MagicMock, fake_runtime: MagicMock
) -> None:
    """``not_found`` is "not my kind of session", so the walk continues.

    Asserted through a downstream tier rather than a status code: a 409 alone
    cannot tell "fell through and nothing else bound" from "refused here".
    """
    fake_runtime.session_store.latest_event_of_type.return_value = {
        "type": "context",
        "payload": {"project": "nobody-registered-this"},
    }
    fake_manager.ensure.return_value = (_make_instance(), True)

    class _BindsAnything:
        def resolve(self, _session_id: str, _runtime: Any) -> ide_routes.IdeWorkspace:
            return ide_routes.IdeWorkspace(project_name="next", project_path="/tmp/next")

    assert ide_routes._resolver is not None
    catalog_tier = ide_routes._resolver.tiers[0]
    ide_routes._resolver.tiers = (catalog_tier, _BindsAnything())

    resp = client.post(f"/api/sessions/{VALID_SID}/ide")

    assert resp.status_code == 201
    assert fake_manager.ensure.call_args.args == (VALID_SID, "next", "/tmp/next")


def test_post_409_when_the_store_read_fails(client: Any, fake_runtime: MagicMock) -> None:
    """A storage failure degrades to "no project" — never a raised error."""
    fake_runtime.session_store.latest_event_of_type.side_effect = RuntimeError("mongo down")
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 409


def test_post_409_carries_a_tier_refusal_verbatim(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recognised-but-unmountable session gets the SPECIFIC sentence.

    The generic "no project in context" body is for a session no tier binds. A
    wiki project whose checkout is gone must say so here rather than falling
    through to the broker's ``workspace_denied`` 403, which names nothing a
    console user can act on.
    """

    class _Refuses:
        def resolve(self, _session_id: str, _runtime: Any) -> None:
            raise ide_routes.IdeWorkspaceUnavailable("the checkout is gone")

    monkeypatch.setattr(
        ide_routes, "_resolver", ide_routes.IdeWorkspaceResolver(tiers=(_Refuses(),))
    )
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 409
    assert resp.get_json()["message"] == "the checkout is gone"


def test_post_503_when_docker_down(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.ensure.side_effect = DockerUnavailable("no sock")
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /ide
# ---------------------------------------------------------------------------


def test_get_returns_200(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.get.return_value = _make_instance()
    resp = client.get(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 200
    assert resp.get_json()["session_id"] == VALID_SID


def test_get_returns_404_when_absent(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.get.return_value = None
    resp = client.get(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 404


def test_get_invalid_session_id_returns_404(client: Any) -> None:
    resp = client.get(f"/api/sessions/{INVALID_SID}/ide")
    assert resp.status_code == 404


def test_get_503_when_docker_down(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.get.side_effect = DockerUnavailable("nope")
    resp = client.get(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# DELETE /ide
# ---------------------------------------------------------------------------


def test_delete_returns_204(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.stop.return_value = True
    resp = client.delete(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 204


def test_delete_returns_404_when_nothing(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.stop.return_value = False
    resp = client.delete(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 404


def test_delete_invalid_session_id_returns_404(client: Any) -> None:
    resp = client.delete(f"/api/sessions/{INVALID_SID}/ide")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /ide/extend
# ---------------------------------------------------------------------------


def test_extend_hours_returns_200(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.extend.return_value = _make_instance()
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"hours": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    fake_manager.extend.assert_called_once()
    call_kwargs = fake_manager.extend.call_args.kwargs
    assert call_kwargs["hours"] == 1


def test_extend_absolute_returns_200(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.extend.return_value = _make_instance()
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"expires_at": future}),
        content_type="application/json",
    )
    assert resp.status_code == 200


def test_extend_rejects_both_fields(client: Any) -> None:
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"hours": 1, "expires_at": future}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_extend_rejects_neither(client: Any) -> None:
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_extend_409_at_cap(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.extend.side_effect = MaxLifetimeReached(datetime.now(UTC) + timedelta(hours=8))
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"hours": 100}),
        content_type="application/json",
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error"] == "max_lifetime_reached"
    assert "max_deadline" in body


def test_extend_404_when_missing(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.extend.side_effect = LookupError("missing")
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"hours": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 404


def test_extend_invalid_session_id_returns_404(client: Any) -> None:
    resp = client.post(
        f"/api/sessions/{INVALID_SID}/ide/extend",
        data=json.dumps({"hours": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 404


def test_extend_503_when_docker_down(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.extend.side_effect = DockerUnavailable("no sock")
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"hours": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 503
