"""The API's five project resolvers, once they all run through one catalog.

Covers the two behaviours that were genuinely broken before the delegation and
the two contracts that must survive it:

1. ``GET /api/projects`` wire shape — every key the console's ``ProjectSummary``
   and Aura's ``ProjectDto`` decode, on both entry kinds. This payload has three
   clients and no version negotiation, so it is asserted key-set-exactly rather
   than by spot-check.
2. ``GET /api/tools?project=`` / ``GET /api/skills?project=`` resolving a
   ``managed:<project_id>`` — they resolved CONFIGURED projects only and
   silently returned the unscoped list for anything else, so scoping either
   surface to a worktree-backed session read as "this project has the same tools
   as the whole deployment".
3. The ``auto`` sentinel resolving to no path (the session temp dir) instead of a
   refusal, and declaring ``project_autoselect`` down into the run — including on
   a LATER turn, after the model has already settled on a real project.
4. A channel ``/switch-project`` being visible to the shared cwd resolution every
   other surface reads, with the room-local keys still readable.

Stubs: ``SessionRuntime.start_async`` and the two registry loaders — the I/O
boundaries. Everything else runs the real code path.
"""

# mypy: ignore-errors

import os
import shutil

import pytest
from mewbo_api import backend
from mewbo_api.channels import routes as channel_routes
from mewbo_core.config import ProjectConfig, get_config
from mewbo_core.session.session_store import SessionStore
from mewbo_core.tooling.exit_plan_mode import session_temp_dir
from mewbo_core.workspaces.repositories import Repository
from mewbo_core.workspaces.repository_store import create_repository_store

# The keys each downstream model declares. Console:
# ``apps/mewbo_console/src/api/contracts.ts:ProjectSummary``; Aura:
# ``apps/mewbo_aura/.../data/api/AuraApi.kt:ProjectDto``. ``repo``/``aliases``
# are decorated by ``_enrich_project_identity`` and are absent — not null — for a
# path with no git remotes, which is why they are not in either set below.
_CONFIG_PROJECT_KEYS = {"name", "path", "description", "available", "source"}
_MANAGED_PROJECT_KEYS = _CONFIG_PROJECT_KEYS | {
    "project_id",
    "is_worktree",
    "parent_project_id",
    "branch",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh temp-dir stores plus one configured and one managed project.

    Points every store the catalog reads at *tmp_path*, including the project
    store (which is otherwise the real ``~/.mewbo`` file — projects created by
    any test would leak into every other one's project list) and the repository
    store the catalog's fourth source reads.
    """
    monkeypatch.setattr(get_config().runtime, "config_dir", str(tmp_path), raising=False)
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.share_store = backend.ShareStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store,
        backend.runtime.session_store,
    )
    backend.project_store = backend.create_project_store()
    # The catalog's one collaborator ``_catalog()`` does not re-point, so the
    # test does it — the sanctioned "aim the one composed instance at fresh
    # stores" seam rather than a second catalog.
    monkeypatch.setattr(
        backend._project_catalog, "repository_store", create_repository_store()
    )
    # The channel command runs against whatever runtime this module holds.
    monkeypatch.setattr(channel_routes, "_runtime", backend.runtime)

    config_dir = tmp_path / "configured-project"
    config_dir.mkdir()
    monkeypatch.setitem(
        get_config().projects,
        "configured-project",
        ProjectConfig(path=str(config_dir), description="A configured one"),
    )

    managed_dir = tmp_path / "managed-project"
    managed_dir.mkdir()
    managed = backend.project_store.create_project(
        name="managed-project", description="A managed one", path=str(managed_dir)
    )
    return {
        "config_name": "configured-project",
        "config_path": str(config_dir),
        "managed": managed,
        "managed_key": f"managed:{managed.project_id}",
        "managed_path": str(managed_dir),
    }


class _RecordingStart:
    """Stub for ``SessionRuntime.start_async`` that records the kwargs it got."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    def __call__(self, **kwargs) -> str:
        self.kwargs = kwargs
        return f"{kwargs.get('session_id', 'sess')}:r1"


def _new_session(client, auth_headers, **payload) -> str:
    resp = client.post("/api/sessions", headers=auth_headers, json=payload)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["session_id"]


# ---------------------------------------------------------------------------
# 1. GET /api/projects — the wire shape three clients decode
# ---------------------------------------------------------------------------


class TestProjectsWireShape:
    def test_config_and_managed_entries_carry_their_declared_keys(
        self, client, auth_headers, env
    ):
        resp = client.get("/api/projects", headers=auth_headers)
        assert resp.status_code == 200
        rows = {row["name"]: row for row in resp.get_json()["projects"]}

        config_row = rows[env["config_name"]]
        assert set(config_row) - {"repo", "aliases"} == _CONFIG_PROJECT_KEYS
        assert config_row["source"] == "config"
        assert config_row["path"] == env["config_path"]
        assert config_row["description"] == "A configured one"
        assert config_row["available"] is True

        managed_row = rows["managed-project"]
        assert set(managed_row) - {"repo", "aliases"} == _MANAGED_PROJECT_KEYS
        assert managed_row["source"] == "managed"
        assert managed_row["project_id"] == env["managed"].project_id
        assert managed_row["path"] == env["managed_path"]
        assert managed_row["description"] == "A managed one"
        assert managed_row["available"] is True
        assert managed_row["is_worktree"] is False
        assert managed_row["parent_project_id"] is None
        assert managed_row["branch"] is None

    def test_configured_projects_are_listed_before_managed_ones(
        self, client, auth_headers, env
    ):
        """Order is part of the contract: a picker renders the list as given."""
        sources = [row["source"] for row in client.get(
            "/api/projects", headers=auth_headers
        ).get_json()["projects"]]
        assert "config" in sources and "managed" in sources
        assert sources.index("managed") > sources.index("config")

    def test_a_registered_repository_is_not_listed_as_a_project(
        self, client, auth_headers, env
    ):
        """The catalog carries repositories; this payload deliberately does not.

        A repository with no checkout has no ``path``, and all three clients
        anchor on one. ``/v1/git/repositories`` is where that list lives.
        """
        backend._project_catalog.repository_store.register(
            Repository(slug="git.example.com/acme/beacon")
        )
        rows = client.get("/api/projects", headers=auth_headers).get_json()["projects"]
        assert all(row["source"] in {"config", "managed"} for row in rows)
        assert all(row.get("path") for row in rows)


# ---------------------------------------------------------------------------
# 2. Tool + skill scoping now resolves a managed project (the bug fix)
# ---------------------------------------------------------------------------


class TestCatalogScoping:
    def test_tools_scope_to_a_managed_project(self, client, auth_headers, env, monkeypatch):
        seen = {}

        class _EmptyRegistry:
            def list_specs(self, include_disabled: bool = False):
                return []

        def _record(cwd=None, extra_mcp_servers=None, **_kw):
            seen["trust_cwd"] = _kw.get("trust_cwd")
            seen["cwd"] = cwd
            return _EmptyRegistry()

        monkeypatch.setattr(backend, "load_registry", _record)
        resp = client.get(
            f"/api/tools?project={env['managed_key']}", headers=auth_headers
        )
        assert resp.status_code == 200
        assert seen["cwd"] == env["managed_path"]
        # The catalog vetted the NAME, not the contents — a managed project can
        # be a repository checkout, so its own `.mcp.json` must not name a
        # server that building the registry would then spawn.
        assert seen["trust_cwd"] is False

    def test_tools_still_scope_to_a_configured_project(
        self, client, auth_headers, env, monkeypatch
    ):
        seen = {}

        class _EmptyRegistry:
            def list_specs(self, include_disabled: bool = False):
                return []

        def _record(cwd=None, extra_mcp_servers=None, **_kw):
            seen["trust_cwd"] = _kw.get("trust_cwd")
            seen["cwd"] = cwd
            return _EmptyRegistry()

        monkeypatch.setattr(backend, "load_registry", _record)
        client.get(f"/api/tools?project={env['config_name']}", headers=auth_headers)
        assert seen["cwd"] == env["config_path"]

    def test_tools_ignore_an_unknown_project(self, client, auth_headers, env, monkeypatch):
        """An unresolvable key stays SILENTLY unscoped — no error, no scope."""
        seen = {}

        class _EmptyRegistry:
            def list_specs(self, include_disabled: bool = False):
                return []

        def _record(cwd=None, extra_mcp_servers=None, **_kw):
            seen["trust_cwd"] = _kw.get("trust_cwd")
            seen["cwd"] = cwd
            return _EmptyRegistry()

        monkeypatch.setattr(backend, "load_registry", _record)
        resp = client.get("/api/tools?project=no-such-project", headers=auth_headers)
        assert resp.status_code == 200
        assert seen["cwd"] is None

    def test_skills_scope_to_a_managed_project(self, client, auth_headers, env, monkeypatch):
        from mewbo_core.tooling import skills as core_skills

        seen = {}
        original = core_skills.SkillRegistry.load

        def _record(self, cwd=None, *args, **kwargs):
            seen["cwd"] = cwd
            return original(self, cwd, *args, **kwargs)

        monkeypatch.setattr(core_skills.SkillRegistry, "load", _record)
        resp = client.get(
            f"/api/skills?project={env['managed_key']}", headers=auth_headers
        )
        assert resp.status_code == 200
        assert seen["cwd"] == env["managed_path"]


# ---------------------------------------------------------------------------
# 3. The `auto` sentinel
# ---------------------------------------------------------------------------


class TestAutoSentinel:
    def test_resolver_returns_no_path_and_does_not_raise(self, env):
        assert backend._resolve_project_cwd({"project": "auto"}) is None
        assert backend._resolve_project_cwd({"context": {"project": "auto"}}) is None

    def test_external_cwd_policy_leaves_the_sentinel_to_project_resolution(self, env):
        """No explicit ``cwd`` means the policy returns no path AND no error."""
        policy = backend.ExternalCwdPolicy(get_config())
        assert policy.resolve({"project": "auto"}) == (None, None)

    def test_query_runs_in_the_session_temp_dir_and_declares_autoselect(
        self, client, auth_headers, env, monkeypatch
    ):
        start = _RecordingStart()
        monkeypatch.setattr(backend.runtime, "start_async", start)
        session_id = _new_session(client, auth_headers, project="auto")
        resp = client.post(
            f"/api/sessions/{session_id}/query",
            headers=auth_headers,
            json={"query": "hello"},
        )
        assert resp.status_code == 202
        assert start.kwargs["cwd"] == session_temp_dir(session_id)
        assert start.kwargs["project_autoselect"] is True

    def test_a_real_project_does_not_declare_autoselect(
        self, client, auth_headers, env, monkeypatch
    ):
        start = _RecordingStart()
        monkeypatch.setattr(backend.runtime, "start_async", start)
        session_id = _new_session(client, auth_headers, project=env["config_name"])
        client.post(
            f"/api/sessions/{session_id}/query",
            headers=auth_headers,
            json={"query": "hello"},
        )
        assert start.kwargs["cwd"] == env["config_path"]
        assert start.kwargs["project_autoselect"] is False

    def test_a_later_turn_runs_in_the_project_the_session_settled_on(
        self, client, auth_headers, env, monkeypatch
    ):
        """The binding keeps ``auto``; the context event carries the real project.

        This is the whole cross-turn contract: the session stays in auto mode (so
        the switching tools stay bound and the model may move again) while the
        directory it is currently working in comes from the context event a
        switch wrote.
        """
        start = _RecordingStart()
        monkeypatch.setattr(backend.runtime, "start_async", start)
        session_id = _new_session(client, auth_headers, project="auto")
        # Stand in for what a switch writes: a plain context event naming a real
        # project, carrying no spec mirror.
        backend.runtime.append_context_event(
            session_id, {"project": env["managed_key"], "cwd": env["managed_path"]}
        )
        resp = client.post(
            f"/api/sessions/{session_id}/query",
            headers=auth_headers,
            json={"query": "second turn"},
        )
        assert resp.status_code == 202
        assert start.kwargs["cwd"] == env["managed_path"]
        # Still auto: the model can switch again.
        assert start.kwargs["project_autoselect"] is True

    def test_resending_the_settled_project_keeps_the_session_in_auto_mode(
        self, client, auth_headers, env, monkeypatch
    ):
        """The console and Aura both resend where the session currently IS.

        That must move the working directory without ending auto mode — taking
        it as "stop auto-selecting" would unbind the switching tools after
        exactly one switch.
        """
        start = _RecordingStart()
        monkeypatch.setattr(backend.runtime, "start_async", start)
        session_id = _new_session(client, auth_headers, project="auto")
        resp = client.post(
            f"/api/sessions/{session_id}/query",
            headers=auth_headers,
            json={"query": "turn two", "context": {"project": env["managed_key"]}},
        )
        assert resp.status_code == 202
        assert start.kwargs["cwd"] == env["managed_path"]
        assert start.kwargs["project_autoselect"] is True
        # And the BINDING still reads auto, so the next turn does too.
        assert backend._session_specs.load(session_id).project == "auto"

    def test_message_re_engage_still_declares_autoselect(
        self, client, auth_headers, env, monkeypatch
    ):
        start = _RecordingStart()
        monkeypatch.setattr(backend.runtime, "start_async", start)
        session_id = _new_session(client, auth_headers, project="auto")
        resp = client.post(
            f"/api/sessions/{session_id}/message",
            headers=auth_headers,
            json={"text": "carry on"},
        )
        assert resp.status_code in (200, 202), resp.get_json()
        assert start.kwargs["project_autoselect"] is True

    def test_session_cwd_resolution_skips_the_sentinel(self, client, auth_headers, env):
        session_id = _new_session(client, auth_headers, project="auto")
        backend.runtime.append_context_event(session_id, {"project": env["config_name"]})
        backend.runtime.append_context_event(session_id, {"project": "auto"})
        assert backend._resolve_session_cwd(session_id) == env["config_path"]


# ---------------------------------------------------------------------------
# 4. The channels divergence
# ---------------------------------------------------------------------------


def _switch(session_id: str, args: str) -> str:
    """Drive ``/switch-project`` the way the inbound pipeline does."""
    message = channel_routes.InboundMessage(
        platform="nextcloud-talk",
        channel_id="room1",
        thread_id=None,
        message_id="1",
        sender_id="u1",
        sender_name="User",
        text=f"/switch-project {args}",
        timestamp="2020-01-01T00:00:00+00:00",
    )
    ctx = channel_routes.CommandContext(
        session_id=session_id,
        args=args,
        message=message,
        tag="nextcloud-talk:room:room1",
    )
    return channel_routes._cmd_switch_project(ctx)


class TestChannelSwitchProject:
    def test_switch_is_visible_to_the_shared_session_cwd_resolution(
        self, client, auth_headers, env
    ):
        session_id = _new_session(client, auth_headers)
        reply = _switch(session_id, env["config_name"])
        assert env["config_name"] in reply
        # The point of the convergence: every other surface reads this.
        assert backend._resolve_session_cwd(session_id) == env["config_path"]

    def test_switch_resolves_a_managed_project_too(self, client, auth_headers, env):
        session_id = _new_session(client, auth_headers)
        _switch(session_id, env["managed_key"])
        assert backend._resolve_session_cwd(session_id) == env["managed_path"]

    def test_switch_writes_only_the_shared_keys(self, client, auth_headers, env):
        session_id = _new_session(client, auth_headers)
        _switch(session_id, env["config_name"])
        payloads = [
            event["payload"]
            for event in backend.runtime.session_store.load_transcript(session_id)
            if event.get("type") == "context"
        ]
        switched = [p for p in payloads if p.get("project") == env["config_name"]]
        assert len(switched) == 1
        assert switched[0]["cwd"] == env["config_path"]
        assert "active_project" not in switched[0]
        assert "active_project_cwd" not in switched[0]

    def test_unknown_project_lists_what_is_switchable(self, client, auth_headers, env):
        session_id = _new_session(client, auth_headers)
        reply = _switch(session_id, "no-such-project")
        assert "Unknown project" in reply
        assert f"`{env['config_name']}`" in reply
        assert f"`{env['managed_key']}`" in reply

    def test_a_legacy_transcript_still_resolves(self, client, auth_headers, env):
        """A room that switched BEFORE the convergence keeps working.

        Read both spellings, write only the new one — there is nothing to
        migrate, so an existing transcript carrying only ``active_project_cwd``
        must still answer.
        """
        session_id = _new_session(client, auth_headers)
        backend.runtime.session_store.append_event(
            session_id,
            {
                "type": "context",
                "payload": {
                    "source_platform": "nextcloud-talk",
                    "active_project": env["config_name"],
                    "active_project_cwd": env["config_path"],
                },
            },
        )
        # The shared resolution alone cannot see the legacy keys …
        assert backend._resolve_session_cwd(session_id) is None
        # … and the channel context is what bridges them.
        resolved = channel_routes._projects.cwd_for(
            session_id,
            lambda: backend.runtime.session_store.load_transcript(session_id),
        )
        assert resolved == env["config_path"]

    def test_the_shared_resolution_wins_over_a_legacy_key(
        self, client, auth_headers, env
    ):
        session_id = _new_session(client, auth_headers)
        backend.runtime.session_store.append_event(
            session_id,
            {
                "type": "context",
                "payload": {"active_project_cwd": str(env["managed_path"])},
            },
        )
        _switch(session_id, env["config_name"])
        resolved = channel_routes._projects.cwd_for(
            session_id,
            lambda: backend.runtime.session_store.load_transcript(session_id),
        )
        assert resolved == env["config_path"]


# ---------------------------------------------------------------------------
# 5. Managed-project directory creation survives the delegation
# ---------------------------------------------------------------------------


class TestManagedDirectoryCreation:
    def test_a_managed_project_whose_directory_vanished_is_recreated(self, env):
        """Mewbo owns a managed project's directory; the store row is authority."""
        shutil.rmtree(env["managed_path"])
        assert not os.path.isdir(env["managed_path"])
        resolved = backend._resolve_project_cwd({"project": env["managed_key"]})
        assert resolved == env["managed_path"]
        assert os.path.isdir(env["managed_path"])

    def test_a_configured_project_whose_directory_vanished_still_refuses(self, env):
        """An operator's directory is not Mewbo's to create."""
        os.rmdir(env["config_path"])
        with pytest.raises(ValueError):
            backend._resolve_project_cwd({"project": env["config_name"]})

    def test_an_unknown_project_refuses_and_names_the_alternatives(self, env):
        with pytest.raises(ValueError) as excinfo:
            backend._resolve_project_cwd({"project": "no-such-project"})
        assert env["config_name"] in str(excinfo.value)
