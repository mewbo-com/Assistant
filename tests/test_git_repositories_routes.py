#!/usr/bin/env python3
"""Contract tests for the repository registry — ``/v1/git/repositories*``.

Driven through a real Flask app with the real blueprint, the real permission
guard and the real in-memory store, so the assertions are about the SHIPPED
contract rather than a mock of it. The only stubs are the two I/O edges a
registry must not depend on: the wiki store and the managed-project store.

The load-bearing property under test is that **registration is inert** — no
clone, no ``ls-remote``, no reachability probe. That is asserted structurally
(``subprocess`` and ``socket`` are poisoned for the duration) rather than by
reading the implementation, because "we didn't call the network" is exactly the
kind of claim that rots the moment someone adds a convenience check.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask
from mewbo_api.auth.guard_registry import guard_registry
from mewbo_api.errors import register_api_error_handler
from mewbo_api.git_repositories_routes import (
    GitRepositoriesController,
    RepositoryCheckout,
    RepositoryUsageSources,
    build_blueprint,
)
from mewbo_api.repo_identity import RepoIdentity
from mewbo_core.workspaces.repositories import Repository, RepositoryWikiUsage
from mewbo_core.workspaces.repository_store import InMemoryRepositoryStore

# ---------------------------------------------------------------------------
# Fixtures — a real app, real guard, real store; stubs only at the I/O edges
# ---------------------------------------------------------------------------


class _AllowAllKit:
    """A disabled-auth ``AuthKit`` stand-in — what a default deployment resolves to.

    Returning ``None`` from every guard is exactly what the real kit does with
    ``api.auth.enabled`` false. Used by the contract tests, whose subject is the
    registry's behaviour rather than its access control; enforcement gets its
    own test below against :class:`_DenyingKit`.
    """

    def require_api_key(self) -> None:
        return None

    def require_master_token(self) -> None:
        return None

    def require_permission(self, permission: str):
        def _guard() -> None:
            return None

        return _guard


class _DenyingKit(_AllowAllKit):
    """Authenticates, then refuses every permission — and RECORDS what was asked.

    The point is the recording. Asserting only that a denied caller gets a 403
    would still pass if a route were bound to the wrong permission, or to a
    permission from an unrelated domain; capturing the requested ids is what
    turns "the guard refuses" into "the guard refuses THIS verb".
    """

    def __init__(self) -> None:
        """Start with an empty record of requested permission ids."""
        self.requested: list[str] = []

    def require_permission(self, permission: str):
        def _guard() -> tuple[dict, int]:
            self.requested.append(permission)
            return {"message": "insufficient role"}, 403

        return _guard


class _FakeWikiProject:
    """The two fields the usage projection reads off a wiki ``Project``."""

    def __init__(self, slug: str, *, pages: int = 3, indexed_at: str = "2020-01-01T00:00:00Z"):
        self.slug = slug
        self.pages = pages
        self.indexed_at = indexed_at


class _FakeWikiStore:
    """Stands in for ``WikiStoreBase`` — the graph-gated half of the projection."""

    def __init__(self, projects: list[_FakeWikiProject] | None = None) -> None:
        self.projects = projects or []

    def list_projects(self) -> list[_FakeWikiProject]:
        return list(self.projects)


class _StubUsage(RepositoryUsageSources):
    """A usage edge whose three legs are literals — no graph, no git, no store.

    Subclasses the real class rather than duck-typing it so the controller is
    driven through the genuine interface; only the I/O bodies are replaced.
    """

    def __init__(
        self,
        *,
        wiki: dict[str, RepositoryWikiUsage] | None = None,
        credentials: list[str] | None = None,
        managed: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        # ``None`` is passed straight through, NOT coerced to ``{}``: for the
        # wiki leg those mean different things (no wiki surface at all vs a
        # surface with nothing indexed), so the default no-arg stub is the
        # honest base-install shape rather than an empty-but-present wiki.
        self._wiki = wiki
        self._credentials = credentials or []
        self._managed = managed or []

    def wiki_usage(self) -> dict[str, RepositoryWikiUsage] | None:
        return self._wiki

    def credential_scopes(self) -> list[str]:
        return self._credentials

    def managed_projects(self) -> list[dict[str, Any]]:
        return self._managed


@pytest.fixture(autouse=True)
def _bind_guard():
    """Bind the process-wide guard for the test, then restore what was there."""
    with guard_registry.rebound(_AllowAllKit()):
        yield


@pytest.fixture
def store() -> InMemoryRepositoryStore:
    """A real store — the registry semantics under test are its own."""
    return InMemoryRepositoryStore()


def build_app(controller: GitRepositoriesController) -> Flask:
    """Mount the real blueprint + the real error handler on a bare Flask app."""
    app = Flask(__name__)
    register_api_error_handler(app)
    app.register_blueprint(build_blueprint(controller), url_prefix="/v1/git")
    return app


@pytest.fixture
def client(store: InMemoryRepositoryStore):
    """A test client over the registry with empty usage sources."""
    controller = GitRepositoriesController(store, usage=_StubUsage())
    return build_app(controller).test_client()


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch):
    """Poison every subprocess + socket entry point for the duration.

    This is what makes "registration is inert" a MEASURED property instead of a
    claim: a future convenience ``ls-remote`` or reachability probe fails the
    test loudly rather than silently making registration cost a network round
    trip. Poisoning the module attributes covers the indirect callers too
    (``RepoIdentity`` shells out through ``subprocess.run``).
    """

    def _forbidden(*args: Any, **kwargs: Any):
        raise AssertionError(f"registration must not touch the network: {args!r}")

    for name in ("run", "check_output", "Popen", "call", "check_call"):
        monkeypatch.setattr(subprocess, name, _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "socket", _forbidden)
    return None


# ---------------------------------------------------------------------------
# Registration is inert
# ---------------------------------------------------------------------------


def test_registration_touches_no_network(client, no_network):
    """POST normalizes + persists and does nothing else — no git, no sockets."""
    resp = client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["slug"] == "github.com/acme/beacon"
    assert body["host"] == "github.com"
    assert body["owner"] == "acme"
    assert body["repo"] == "beacon"
    assert body["origin"] == "manual"


def test_registration_derives_identity_and_defaults_the_name(client):
    """``slug``/``host``/``owner``/``repo``/``name`` are all server-derived."""
    resp = client.post(
        "/v1/git/repositories", json={"repoUrl": "git@git.example.com:acme/beacon.git"}
    )

    body = resp.get_json()
    assert resp.status_code == 201
    assert body["slug"] == "git.example.com/acme/beacon"
    assert body["name"] == "beacon"
    assert body["usage"] == {"wiki": None, "tasks": None, "credential": None}


@pytest.mark.parametrize(
    "repo_url,expected",
    [
        ("https://github.com/acme/beacon", "github"),
        ("https://gitlab.com/acme/beacon", "gitlab"),
        ("https://gitea.com/acme/beacon", "gitea"),
        ("https://codeberg.org/acme/beacon", "gitea"),
        ("https://myorg.visualstudio.com/acme/beacon", "azure"),
        # A self-hosted forge is genuinely unknowable from its host, so the
        # neutral "git" is the honest answer — never a substring guess.
        ("https://git.example.com/acme/beacon", "git"),
    ],
)
def test_platform_is_derived_from_the_host(client, repo_url: str, expected: str):
    """``platform`` is server-owned and derived — the client cannot set it.

    Pins the derivation at the REST boundary. The mapping itself lives on
    ``RepositoryRef``/``Repository`` in ``mewbo_core`` (it is a function of the
    host and nothing else); this asserts the route actually surfaces it, which
    is what the console renders.
    """
    body = client.post("/v1/git/repositories", json={"repoUrl": repo_url}).get_json()

    assert body["platform"] == expected


def test_registration_normalizes_equivalent_urls_onto_one_slug(client):
    """``.git``, a trailing slash and host case all converge on one identity."""
    first = client.post(
        "/v1/git/repositories", json={"repoUrl": "https://GitHub.com/acme/beacon.git/"}
    )
    assert first.status_code == 201
    assert first.get_json()["slug"] == "github.com/acme/beacon"

    again = client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})
    assert again.status_code == 409


# ---------------------------------------------------------------------------
# Refusals — the four wire codes
# ---------------------------------------------------------------------------


def test_duplicate_slug_is_409_repository_exists(client):
    """A second registration of the same identity is a conflict, not a merge."""
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})
    resp = client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "repository_exists"


def test_unparseable_url_is_400_invalid_repo_url(client):
    """A reference that cannot name a repository is refused at the boundary."""
    resp = client.post("/v1/git/repositories", json={"repoUrl": "not-a-repo"})

    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_repo_url"


@pytest.mark.parametrize(
    "field,value",
    [
        ("slug", "github.com/acme/beacon"),
        ("platform", "github"),
        ("origin", "wiki"),
        ("createdAt", "2020-01-01T00:00:00Z"),
        ("usage", {}),
    ],
)
def test_server_owned_field_on_create_is_400(client, field: str, value: Any):
    """``extra="forbid"`` turns a smuggled server-owned field into a clean 400.

    The whole point of the forbid: silently ignoring these is how a caller ends
    up believing it set a field the server actually derived.
    """
    resp = client.post(
        "/v1/git/repositories",
        json={"repoUrl": "https://github.com/acme/beacon", field: value},
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "invalid_request"
    assert field in body["error"]["reason"]


@pytest.mark.parametrize("field,value", [("repoUrl", "https://github.com/acme/other"),
                                         ("platform", "gitlab"),
                                         ("slug", "github.com/acme/other")])
def test_server_owned_field_on_patch_is_400(client, field: str, value: Any):
    """PATCH is narrower than the core patch model: identity is not editable.

    ``repoUrl``/``platform`` ARE accepted by ``mewbo_core``'s ``RepositoryPatch``;
    this surface deliberately refuses them, so re-pointing a slug at a different
    remote stays a delete-and-re-register.
    """
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    resp = client.patch("/v1/git/repositories/github.com/acme/beacon", json={field: value})

    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_request"


def test_unknown_slug_is_404_repository_not_found(client):
    """GET/PATCH/DELETE on an unregistered slug all report the same code."""
    for call in (
        lambda: client.get("/v1/git/repositories/github.com/acme/ghost"),
        lambda: client.patch("/v1/git/repositories/github.com/acme/ghost", json={"name": "x"}),
        lambda: client.delete("/v1/git/repositories/github.com/acme/ghost"),
    ):
        resp = call()
        assert resp.status_code == 404
        assert resp.get_json()["error"]["code"] == "repository_not_found"


# ---------------------------------------------------------------------------
# Read / update / delete
# ---------------------------------------------------------------------------


def test_slug_with_slashes_routes_through_the_path_converter(client):
    """``<path:slug>`` is what makes a three-segment slug addressable at all."""
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    resp = client.get("/v1/git/repositories/github.com/acme/beacon")

    assert resp.status_code == 200
    assert resp.get_json()["slug"] == "github.com/acme/beacon"


def test_patch_updates_only_the_fields_sent(client):
    """An omitted field keeps its stored value; an explicit null clears it."""
    client.post(
        "/v1/git/repositories",
        json={"repoUrl": "https://github.com/acme/beacon", "description": "original"},
    )

    resp = client.patch(
        "/v1/git/repositories/github.com/acme/beacon", json={"name": "Beacon"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Beacon"
    assert resp.get_json()["description"] == "original"

    cleared = client.patch(
        "/v1/git/repositories/github.com/acme/beacon", json={"description": None}
    )
    assert cleared.get_json()["description"] is None


def test_delete_deregisters_and_returns_204(client, store):
    """DELETE removes the row and nothing else."""
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    resp = client.delete("/v1/git/repositories/github.com/acme/beacon")

    assert resp.status_code == 204
    assert store.get("github.com/acme/beacon") is None


def test_delete_leaves_the_wiki_project_intact(store):
    """Deregistration must NEVER destroy a paid indexing run.

    The wiki store is the authority on the index; the registry only points at
    it. After a DELETE the wiki project is untouched — and the next list
    re-adopts the slug, which is the reconciliation working, not a failed
    delete.
    """
    wiki_store = _FakeWikiStore([_FakeWikiProject("github.com/acme/beacon")])
    controller = GitRepositoriesController(
        store, usage=RepositoryUsageSources(SimpleNamespace(wiki_store=wiki_store))
    )
    client = build_app(controller).test_client()
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    assert client.delete("/v1/git/repositories/github.com/acme/beacon").status_code == 204

    assert [project.slug for project in wiki_store.projects] == ["github.com/acme/beacon"]
    readopted = client.get("/v1/git/repositories").get_json()["repositories"]
    assert [row["slug"] for row in readopted] == ["github.com/acme/beacon"]
    assert readopted[0]["origin"] == "wiki"


# ---------------------------------------------------------------------------
# Adoption + the usage projection
# ---------------------------------------------------------------------------


def test_list_adopts_wiki_projects_missing_from_the_registry(store):
    """A wiki-indexed repository IS a known repository — the lists cannot diverge."""
    wiki_store = _FakeWikiStore([_FakeWikiProject("github.com/acme/beacon", pages=7)])
    controller = GitRepositoriesController(
        store, usage=RepositoryUsageSources(SimpleNamespace(wiki_store=wiki_store))
    )
    client = build_app(controller).test_client()

    rows = client.get("/v1/git/repositories").get_json()["repositories"]

    assert len(rows) == 1
    assert rows[0]["slug"] == "github.com/acme/beacon"
    assert rows[0]["origin"] == "wiki"
    assert rows[0]["usage"]["wiki"] == {
        "indexed": True,
        "indexedAt": "2020-01-01T00:00:00Z",
        "pages": 7,
    }


def test_adoption_is_idempotent_and_preserves_manual_origin(store):
    """Re-listing converges; adoption never rewrites who registered a row first.

    ``origin`` records WHO brought the repository into the registry, so a wiki
    index finding a hand-added row must enrich it, never claim it.
    """
    wiki_store = _FakeWikiStore([_FakeWikiProject("github.com/acme/beacon")])
    controller = GitRepositoriesController(
        store, usage=RepositoryUsageSources(SimpleNamespace(wiki_store=wiki_store))
    )
    client = build_app(controller).test_client()
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    first = client.get("/v1/git/repositories").get_json()["repositories"]
    second = client.get("/v1/git/repositories").get_json()["repositories"]

    assert len(first) == len(second) == 1
    assert second[0]["origin"] == "manual"


def test_adoption_skips_a_legacy_two_segment_slug(store):
    """An unparseable wiki slug is skipped, never allowed to fail the whole list."""
    wiki_store = _FakeWikiStore(
        [_FakeWikiProject("acme/legacy"), _FakeWikiProject("github.com/acme/beacon")]
    )
    controller = GitRepositoriesController(
        store, usage=RepositoryUsageSources(SimpleNamespace(wiki_store=wiki_store))
    )
    client = build_app(controller).test_client()

    rows = client.get("/v1/git/repositories").get_json()["repositories"]

    assert [row["slug"] for row in rows] == ["github.com/acme/beacon"]


def test_create_resolves_the_store_backed_usage_legs_but_not_tasks(store):
    """The 201 reports an existing credential; ``tasks`` is left for the next read.

    Pins the one asymmetry in the surface. The wiki and credential legs are
    cheap store reads, and saving a credential BEFORE registering the repository
    is the common order — reporting it null there would be wrong. The ``tasks``
    leg shells out to ``git remote -v`` per managed project, and registration
    doing local git I/O is exactly what "registration is inert" rules out, so it
    is the one null that means "not looked at" rather than "not present".
    """
    controller = GitRepositoriesController(
        store,
        usage=_StubUsage(
            credentials=["github.com"],
            managed=[
                {
                    "projectId": "proj-1",
                    "name": "beacon",
                    "path": "/srv/beacon",
                    "aliases": ["github.com/acme/beacon"],
                }
            ],
        ),
    )
    client = build_app(controller).test_client()

    created = client.post(
        "/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"}
    ).get_json()

    assert created["usage"]["credential"] == {"scope": "github.com", "scopeType": "host"}
    assert created["usage"]["tasks"] is None

    # ...and the very next read resolves it.
    read = client.get("/v1/git/repositories/github.com/acme/beacon").get_json()
    assert read["usage"]["tasks"] == {
        "projectId": "proj-1",
        "name": "beacon",
        "path": "/srv/beacon",
    }


def test_wiki_usage_distinguishes_no_wiki_surface_from_not_indexed(store):
    """``usage.wiki`` is THREE-state, and the console branches on the difference.

    ``null`` means this deployment has no wiki surface to ask, so nothing can be
    said. A real ``indexed: false`` means the surface answered and has never
    indexed this repository. Collapsing them would make the pane either offer to
    generate a wiki the server cannot build, or withhold one it would accept.

    ``pages`` is ``0`` rather than null in the un-indexed case: the count is
    known to be zero, and the console types it as a number.
    """
    # Extra PRESENT, nothing indexed → a real "not indexed" answer.
    present = GitRepositoriesController(
        store, usage=RepositoryUsageSources(SimpleNamespace(wiki_store=_FakeWikiStore([])))
    )
    client = build_app(present).test_client()
    created = client.post(
        "/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"}
    ).get_json()
    assert created["usage"]["wiki"] == {"indexed": False, "indexedAt": None, "pages": 0}

    # Extra ABSENT → null, meaning "cannot say".
    absent = GitRepositoriesController(
        InMemoryRepositoryStore(), usage=RepositoryUsageSources(SimpleNamespace())
    )
    bare = build_app(absent).test_client()
    bare_body = bare.post(
        "/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"}
    ).get_json()
    assert bare_body["usage"]["wiki"] is None


def test_wiki_store_failure_reads_as_unknown_not_as_not_indexed(store):
    """A store hiccup resolves to ``null``, never to a confident ``indexed: false``.

    "We could not look" is not "there is no index" — reporting the latter would
    invite the console to offer indexing based on a read that never happened.
    """

    class _BrokenWikiStore:
        def list_projects(self):
            raise RuntimeError("mongo is down")

    controller = GitRepositoriesController(
        store, usage=RepositoryUsageSources(SimpleNamespace(wiki_store=_BrokenWikiStore()))
    )
    client = build_app(controller).test_client()
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    row = client.get("/v1/git/repositories").get_json()["repositories"][0]

    assert row["usage"]["wiki"] is None


def test_timestamps_are_always_present_iso_strings(client):
    """``createdAt``/``updatedAt`` are never null — the store stamps both on write.

    A nullable timestamp is a shape this backend cannot produce, and the console
    types them non-null.
    """
    body = client.post(
        "/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"}
    ).get_json()

    for field in ("createdAt", "updatedAt"):
        assert isinstance(body[field], str) and body[field]
        # Parseable ISO 8601 — asserted by parsing, not by regex-matching a shape.
        datetime.fromisoformat(body[field])


def test_repo_scoped_credential_wins_over_the_host_scope(store):
    """The narrowest covering scope is reported — the order a clone would try."""
    controller = GitRepositoriesController(
        store,
        usage=_StubUsage(credentials=["github.com", "github.com/acme/beacon"]),
    )
    client = build_app(controller).test_client()
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    usage = client.get("/v1/git/repositories/github.com/acme/beacon").get_json()["usage"]

    assert usage["credential"] == {"scope": "github.com/acme/beacon", "scopeType": "repo"}


def test_host_scoped_credential_covers_a_repo_with_no_own_scope(store):
    """A bare host scope is shared by every repository on that host."""
    controller = GitRepositoriesController(store, usage=_StubUsage(credentials=["github.com"]))
    client = build_app(controller).test_client()
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    usage = client.get("/v1/git/repositories/github.com/acme/beacon").get_json()["usage"]

    assert usage["credential"] == {"scope": "github.com", "scopeType": "host"}


def test_task_usage_matches_a_managed_project_by_repo_alias(store):
    """A managed checkout is matched via ``RepoIdentity`` aliases, not by path."""
    controller = GitRepositoriesController(
        store,
        usage=_StubUsage(
            managed=[
                {
                    "projectId": "proj-1",
                    "name": "beacon",
                    "path": "/srv/beacon",
                    "aliases": ["github.com/acme/beacon", "acme/beacon", "beacon"],
                }
            ]
        ),
    )
    client = build_app(controller).test_client()
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    usage = client.get("/v1/git/repositories/github.com/acme/beacon").get_json()["usage"]

    assert usage["tasks"] == {"projectId": "proj-1", "name": "beacon", "path": "/srv/beacon"}


# ---------------------------------------------------------------------------
# The base install — mewbo_graph absent
# ---------------------------------------------------------------------------


def _run_probe(code: str) -> dict:
    """Run *code* in a FRESH interpreter and return the JSON it prints.

    Mirrors ``test_iam_architecture._run_probe``, and for the same reason: an
    in-process assertion about the import graph is worthless because pytest has
    already imported half the world, so ``"mewbo_graph" in sys.modules`` is true
    regardless of what the module under test does. The claim "this surface
    mounts on a base install" is precisely an import-graph claim, so it can only
    be settled in an interpreter that never imported the extra.
    """
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, f"probe failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_module_imports_without_pulling_in_the_graph_extra():
    """Importing the routes module must not drag ``mewbo_graph`` in with it.

    THE base-install invariant, and the one an in-process test cannot check.
    ``init_wiki`` returns early without the ``wiki`` extra, which is why this
    blueprint is registered from ``backend.py`` instead — but that placement
    only helps if the module itself is importable there. A module-scope
    ``mewbo_graph`` import would make the registry fail to mount on exactly the
    base install that agentic tasks run on.
    """
    probe = _run_probe(
        "import json, sys\n"
        "import mewbo_api.git_repositories_routes as m\n"
        "print(json.dumps({\n"
        "    'graph_imported': any(k == 'mewbo_graph' or k.startswith('mewbo_graph.')\n"
        "                          for k in sys.modules),\n"
        "    'has_controller': hasattr(m, 'GitRepositoriesController'),\n"
        "}))"
    )

    assert probe["has_controller"] is True
    assert probe["graph_imported"] is False, (
        "importing git_repositories_routes pulled in mewbo_graph — the registry "
        "would not mount on a base install"
    )


def test_blueprint_mounts_with_the_graph_extra_blocked():
    """The whole surface builds and serves in an interpreter where the extra is absent.

    Blocks ``mewbo_graph`` at the meta-path (a genuinely uninstalled extra, not a
    patched ``sys.modules``) and drives every verb, so this covers the mount AND
    the request path rather than the import alone.
    """
    probe = _run_probe(
        "import json, sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'mewbo_graph' or name.startswith('mewbo_graph.'):\n"
        "            raise ImportError('No module named ' + repr(name))\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "from flask import Flask\n"
        "from mewbo_core.workspaces.repository_store import InMemoryRepositoryStore\n"
        "from mewbo_api.auth.guard_registry import guard_registry\n"
        "from mewbo_api.errors import register_api_error_handler\n"
        "from mewbo_api.git_repositories_routes import (\n"
        "    GitRepositoriesController, RepositoryUsageSources, build_blueprint)\n"
        "class _Kit:\n"
        "    def require_api_key(self): return None\n"
        "    def require_master_token(self): return None\n"
        "    def require_permission(self, p): return lambda: None\n"
        "guard_registry.bind(_Kit())\n"
        "app = Flask(__name__)\n"
        "register_api_error_handler(app)\n"
        "ctrl = GitRepositoriesController(\n"
        "    InMemoryRepositoryStore(), usage=RepositoryUsageSources(None))\n"
        "app.register_blueprint(build_blueprint(ctrl), url_prefix='/v1/git')\n"
        "c = app.test_client()\n"
        "created = c.post('/v1/git/repositories',\n"
        "                 json={'repoUrl': 'https://github.com/acme/beacon'})\n"
        "listed = c.get('/v1/git/repositories')\n"
        "got = c.get('/v1/git/repositories/github.com/acme/beacon')\n"
        "patched = c.patch('/v1/git/repositories/github.com/acme/beacon',\n"
        "                  json={'name': 'Beacon'})\n"
        "deleted = c.delete('/v1/git/repositories/github.com/acme/beacon')\n"
        "print(json.dumps({\n"
        "    'statuses': [created.status_code, listed.status_code, got.status_code,\n"
        "                 patched.status_code, deleted.status_code],\n"
        "    'usage': created.get_json()['usage'],\n"
        "    'graph_imported': any(k == 'mewbo_graph' or k.startswith('mewbo_graph.')\n"
        "                          for k in sys.modules),\n"
        "}))"
    )

    assert probe["statuses"] == [201, 200, 200, 200, 204]
    assert probe["usage"] == {"wiki": None, "tasks": None, "credential": None}
    assert probe["graph_imported"] is False


def test_whole_surface_works_with_the_graph_extra_absent(store, monkeypatch):
    """Every route serves on a base install; the wiki/credential legs read null.

    Simulates the graph-less install by making any ``mewbo_graph`` import raise
    ``ImportError`` — which is what an install without the ``wiki`` extra
    genuinely does — AND by leaving ``runtime.wiki_store`` unset, the state
    ``init_wiki`` returning False actually produces.
    """
    import builtins

    real_import = builtins.__import__

    def _no_graph(name: str, *args: Any, **kwargs: Any):
        if name.startswith("mewbo_graph"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_graph)

    controller = GitRepositoriesController(
        store, usage=RepositoryUsageSources(SimpleNamespace())
    )
    client = build_app(controller).test_client()

    created = client.post(
        "/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"}
    )
    assert created.status_code == 201
    assert created.get_json()["usage"]["wiki"] is None

    listed = client.get("/v1/git/repositories")
    assert listed.status_code == 200
    row = listed.get_json()["repositories"][0]
    assert row["usage"] == {"wiki": None, "tasks": None, "credential": None}

    assert client.get("/v1/git/repositories/github.com/acme/beacon").status_code == 200
    assert (
        client.patch(
            "/v1/git/repositories/github.com/acme/beacon", json={"name": "Beacon"}
        ).status_code
        == 200
    )
    assert client.delete("/v1/git/repositories/github.com/acme/beacon").status_code == 204


def test_usage_sources_degrade_when_the_wiki_store_raises(store):
    """A wiki store hiccup empties one leg — it never takes down the registry."""

    class _BrokenWikiStore:
        def list_projects(self):
            raise RuntimeError("mongo is down")

    controller = GitRepositoriesController(
        store, usage=RepositoryUsageSources(SimpleNamespace(wiki_store=_BrokenWikiStore()))
    )
    client = build_app(controller).test_client()
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"})

    resp = client.get("/v1/git/repositories")

    assert resp.status_code == 200
    assert resp.get_json()["repositories"][0]["usage"]["wiki"] is None


# ---------------------------------------------------------------------------
# The permission bindings
# ---------------------------------------------------------------------------


def test_every_route_is_refused_when_its_permission_is_denied(store):
    """A principal without the permission is REFUSED on all five routes.

    Asserts the property, not the implementation: a test that only drove an
    allow-all kit would pass identically if the ``@guard.requires`` decorators
    were deleted, which is exactly the failure mode ``tests/CLAUDE.md`` warns
    about. Here the guard is the only thing standing between the caller and the
    handler, so removing a decorator turns its 403 into a 2xx and fails.
    """
    kit = _DenyingKit()
    controller = GitRepositoriesController(store, usage=_StubUsage())
    app = build_app(controller)
    client = app.test_client()

    with guard_registry.rebound(kit):
        calls = {
            "list": client.get("/v1/git/repositories"),
            "create": client.post(
                "/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"}
            ),
            "read": client.get("/v1/git/repositories/github.com/acme/beacon"),
            "patch": client.patch(
                "/v1/git/repositories/github.com/acme/beacon", json={"name": "x"}
            ),
            "delete": client.delete("/v1/git/repositories/github.com/acme/beacon"),
        }

    assert [resp.status_code for resp in calls.values()] == [403] * 5
    # The refusal happened BEFORE the handler ran — nothing was written.
    assert store.list() == []
    # And each verb asked for the RIGHT half of the pair.
    assert set(kit.requested) == {"repositories.read", "repositories.write"}


def test_read_routes_do_not_require_the_write_permission(store):
    """The read/write split is real: a GET never asks for ``repositories.write``.

    Pins the least-privilege boundary — a reader must not need the verb that
    lets them mutate the registry.
    """
    kit = _DenyingKit()
    client = build_app(GitRepositoriesController(store, usage=_StubUsage())).test_client()

    with guard_registry.rebound(kit):
        client.get("/v1/git/repositories")
        client.get("/v1/git/repositories/github.com/acme/beacon")

    assert kit.requested == ["repositories.read", "repositories.read"]


def test_routes_declare_the_repository_permissions():
    """Both catalog ids exist and the surface is bound to them.

    ``guard.requires`` validates its ids against the closed catalog at
    DECORATION, so an import of this module is itself the assertion that
    ``repositories.read``/``repositories.write`` are real. This pins the pair
    against a future rename of one half.
    """
    from mewbo_iam import PermissionCatalog

    assert PermissionCatalog.is_valid("repositories.read")
    assert PermissionCatalog.is_valid("repositories.write")


def test_member_role_grants_the_repository_pair():
    """A plain member can manage repositories, mirroring the git-credential pair."""
    from mewbo_iam.roles import BUILTIN_ROLES

    member = next(role for role in BUILTIN_ROLES if role.name == "member")
    assert {"repositories.read", "repositories.write"} <= set(member.permissions)


def test_registered_repository_round_trips_through_the_store(store):
    """The persisted record re-validates to the same identity it was written with."""
    controller = GitRepositoriesController(store, usage=_StubUsage())
    build_app(controller).test_client().post(
        "/v1/git/repositories", json={"repoUrl": "https://github.com/acme/beacon"}
    )

    stored = store.get("github.com/acme/beacon")

    assert isinstance(stored, Repository)
    assert stored.slug == "github.com/acme/beacon"
    assert stored.created_at and stored.updated_at


# ---------------------------------------------------------------------------
# Checkout — the ONE explicit action that puts a repository on disk
# ---------------------------------------------------------------------------


class _FakeProject:
    """The three fields the checkout reads off a ``VirtualProject``."""

    def __init__(self, project_id: str, name: str, path: str) -> None:
        self.project_id = project_id
        self.name = name
        self.path = path


class _FakeProjectStore:
    """A managed-project store that records every call. No disk, no git.

    Records the ``path`` argument rather than asserting on it, so the
    never-a-provided-path rule reads as an explicit assertion in the test that
    cares about it instead of a failure buried in a fixture.
    """

    def __init__(self) -> None:
        self.projects: dict[str, _FakeProject] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def create_project(
        self, name: str, description: str, path: str | None = None
    ) -> _FakeProject:
        project_id = f"proj-{len(self.created) + 1}"
        self.created.append({"name": name, "description": description, "path": path})
        project = _FakeProject(project_id, name, f"/srv/managed/{project_id}")
        self.projects[project_id] = project
        return project

    def delete_project(self, project_id: str) -> None:
        self.deleted.append(project_id)
        self.projects.pop(project_id, None)


class _StubCheckout(RepositoryCheckout):
    """The REAL checkout with only the git call replaced.

    Subclassing at ``_resolve_clone`` — the single seam that reaches the
    optional extra — leaves the ordering, the cleanup and the refusals under
    test rather than stubbed over. A fake that replaced ``run`` would assert
    nothing about the class that ships.
    """

    def __init__(
        self,
        project_store: _FakeProjectStore,
        *,
        ok: bool = True,
        stderr: str = "",
        raises: Exception | None = None,
    ) -> None:
        super().__init__(project_store)
        self.calls: list[dict[str, Any]] = []
        self._ok = ok
        self._stderr = stderr
        self._raises = raises

    def _resolve_clone(self) -> Any:
        def _clone(url: str, clone_dir: Any, *, store: Any, slug: str, timeout: int):
            self.calls.append(
                {"url": url, "dir": str(clone_dir), "slug": slug, "timeout": timeout}
            )
            if self._raises is not None:
                raise self._raises
            return SimpleNamespace(ok=self._ok, stderr=self._stderr)

        return _clone


class _ProjectStoreUsage(_StubUsage):
    """A usage edge whose managed leg TRACKS the fake project store.

    Models the real read path — a project the checkout creates shows up in the
    next usage read — which is what makes "the idempotency guard agrees with
    what ``usage.tasks`` reports" a measured property rather than a claim about
    two code paths that merely look alike.
    """

    def __init__(self, project_store: _FakeProjectStore, slug: str) -> None:
        super().__init__()
        self._project_store = project_store
        self._slug = slug

    def managed_projects(self) -> list[dict[str, Any]]:
        return [
            {
                "projectId": project.project_id,
                "name": project.name,
                "path": project.path,
                "aliases": [self._slug],
            }
            for project in self._project_store.projects.values()
        ]


SLUG = "github.com/acme/beacon"
CHECKOUT_URL = f"/v1/git/repositories/{SLUG}/checkout"


@pytest.fixture
def checkout_client(store: InMemoryRepositoryStore):
    """A registered repository plus a client whose checkout stubs only git."""

    def _build(**kwargs: Any):
        projects = _FakeProjectStore()
        checkout = _StubCheckout(projects, **kwargs)
        controller = GitRepositoriesController(
            store,
            usage=_ProjectStoreUsage(projects, SLUG),
            checkout=checkout,
        )
        client = build_app(controller).test_client()
        client.post("/v1/git/repositories", json={"repoUrl": f"https://{SLUG}"})
        return client, checkout, projects

    return _build


def test_checkout_creates_a_managed_project_and_links_it(checkout_client):
    """201, the clone runs once, and the binding surfaces as ``usage.tasks``."""
    client, checkout, projects = checkout_client()

    resp = client.post(CHECKOUT_URL)

    assert resp.status_code == 201
    assert resp.get_json()["usage"]["tasks"] == {
        "projectId": "proj-1",
        "name": "beacon",
        "path": "/srv/managed/proj-1",
    }
    assert len(checkout.calls) == 1
    assert checkout.calls[0]["url"] == f"https://{SLUG}"
    assert checkout.calls[0]["slug"] == SLUG
    assert checkout.calls[0]["timeout"] == RepositoryCheckout.CLONE_TIMEOUT_SECONDS
    # The clone target IS the project's own path — nothing is cloned elsewhere
    # and moved.
    assert checkout.calls[0]["dir"] == "/srv/managed/proj-1"


def test_checkout_never_asks_for_a_provided_path(checkout_client):
    """The auto-path form, because the reaper deletes childless provided-path parents.

    A ``path_source="provided"`` managed project is permanently deleted once it
    has no worktree children, so a checkout registered that way would evaporate
    under the user. Asserted on the call rather than trusted to a comment.
    """
    client, _checkout, projects = checkout_client()

    client.post(CHECKOUT_URL)

    assert projects.created == [
        {"name": "beacon", "description": f"Checkout of {SLUG}", "path": None}
    ]


def test_checkout_is_idempotent_and_never_clones_twice(checkout_client):
    """A repository that already has a checkout returns the existing one, 200."""
    client, checkout, projects = checkout_client()

    first = client.post(CHECKOUT_URL)
    second = client.post(CHECKOUT_URL)

    assert first.status_code == 201
    assert second.status_code == 200
    assert len(checkout.calls) == 1, "the second call must not re-clone"
    assert len(projects.projects) == 1
    assert first.get_json()["usage"]["tasks"] == second.get_json()["usage"]["tasks"]


def test_a_failed_clone_leaves_no_managed_project_behind(checkout_client):
    """503 carrying git's reason, and the half-made project is deleted.

    The cleanup is the point: without it a failed checkout leaves a managed
    project pointing at an empty directory, which then reads as a real checkout
    on the next list and can never be retried.
    """
    client, checkout, projects = checkout_client(
        ok=False, stderr="fatal: could not read Username for 'https://github.com'"
    )

    resp = client.post(CHECKOUT_URL)

    assert resp.status_code == 503
    body = resp.get_json()["error"]
    assert body["code"] == RepositoryCheckout.CHECKOUT_FAILED
    assert "could not read Username" in body["reason"]
    assert body["retryable"] is False
    assert projects.deleted == ["proj-1"]
    assert projects.projects == {}
    # And the repository is still registered, still with no task binding.
    assert client.get(f"/v1/git/repositories/{SLUG}").get_json()["usage"]["tasks"] is None


def test_a_raising_clone_is_a_refusal_not_a_500(checkout_client):
    """An exception out of the git layer is caught, cleaned up, and refused."""
    client, _checkout, projects = checkout_client(raises=OSError("git binary missing"))

    resp = client.post(CHECKOUT_URL)

    assert resp.status_code == 503
    assert resp.get_json()["error"]["code"] == RepositoryCheckout.CHECKOUT_FAILED
    assert projects.deleted == ["proj-1"]


def test_checkout_names_the_missing_capability_on_a_base_install(store, monkeypatch):
    """No ``wiki`` extra ⇒ a 503 an operator can act on, and NOTHING is created.

    Drives the REAL ``_resolve_clone`` against a genuinely failing import — the
    state an install without the extra is actually in — because the whole value
    of the refusal is that it happens instead of an ImportError 500. The
    ordering assertion matters as much as the status: the refusal lands before
    the project store is touched, so a base install cannot accrete empty managed
    projects from callers probing an endpoint it will never serve.
    """
    import builtins

    real_import = builtins.__import__

    def _no_graph(name: str, *args: Any, **kwargs: Any):
        if name.startswith("mewbo_graph"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_graph)

    projects = _FakeProjectStore()
    controller = GitRepositoriesController(
        store,
        usage=RepositoryUsageSources(SimpleNamespace()),
        checkout=RepositoryCheckout(projects),
    )
    client = build_app(controller).test_client()
    client.post("/v1/git/repositories", json={"repoUrl": f"https://{SLUG}"})

    resp = client.post(CHECKOUT_URL)

    assert resp.status_code == 503
    body = resp.get_json()["error"]
    assert body["code"] == RepositoryCheckout.CHECKOUT_UNAVAILABLE
    assert "wiki" in body["reason"], "the refusal must name what to install"
    assert body["retryable"] is False
    assert projects.created == []
    # The rest of the surface is unaffected — this is not a broken deployment.
    assert client.get("/v1/git/repositories").status_code == 200


def test_checkout_of_an_unregistered_slug_is_404_and_clones_nothing(checkout_client):
    """An unknown repository is a 404 before any project or clone happens."""
    client, checkout, projects = checkout_client()

    resp = client.post("/v1/git/repositories/github.com/acme/ghost/checkout")

    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == GitRepositoriesController.REPOSITORY_NOT_FOUND
    assert checkout.calls == []
    assert projects.created == []


@pytest.mark.parametrize("field,value", [("branch", "main"), ("path", "/srv/x"), ("ref", "v1")])
def test_a_knob_on_the_checkout_body_is_400(checkout_client, field: str, value: str):
    """A checkout has no knobs, and sending one is refused rather than ignored.

    Silently accepting ``{"branch": "main"}`` would hand back a 201 to a caller
    who believes they pinned a branch. The forbid makes the disagreement visible
    at the boundary.
    """
    client, checkout, _projects = checkout_client()

    resp = client.post(CHECKOUT_URL, json={field: value})

    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == GitRepositoriesController.INVALID_REQUEST
    assert field in resp.get_json()["error"]["reason"]
    assert checkout.calls == []


def test_an_absent_body_is_valid(checkout_client):
    """No body and ``{}`` both mean "check it out" — neither is a malformed request."""
    client, _checkout, _projects = checkout_client()

    assert client.post(CHECKOUT_URL).status_code == 201


def test_no_read_of_the_registry_ever_triggers_a_checkout(checkout_client):
    """Registration, listing, reading and patching stay inert — nothing clones.

    The product rule this endpoint exists to preserve: a clone is an action a
    user asks for, never a side effect of a surface rendering. Asserted over
    every other verb, because "we don't clone implicitly" is exactly the kind of
    claim a convenience call quietly breaks.
    """
    client, checkout, projects = checkout_client()

    client.get("/v1/git/repositories")
    client.get(f"/v1/git/repositories/{SLUG}")
    client.patch(f"/v1/git/repositories/{SLUG}", json={"name": "Beacon"})
    client.post("/v1/git/repositories", json={"repoUrl": "https://github.com/acme/relay"})

    assert checkout.calls == []
    assert projects.created == []


def test_checkout_requires_the_write_permission(store):
    """A reader cannot cause a clone: the checkout is bound to ``repositories.write``."""
    kit = _DenyingKit()
    projects = _FakeProjectStore()
    checkout = _StubCheckout(projects)
    controller = GitRepositoriesController(
        store, usage=_ProjectStoreUsage(projects, SLUG), checkout=checkout
    )
    client = build_app(controller).test_client()

    with guard_registry.rebound(kit):
        resp = client.post(CHECKOUT_URL)

    assert resp.status_code == 403
    assert kit.requested == ["repositories.write"]
    assert checkout.calls == []


def test_a_real_checkout_is_recognised_by_the_usage_projection(tmp_path):
    """A REAL clone's remotes resolve back to the registry slug.

    The idempotency guard and the ``usage.tasks`` a client renders both key off
    ``RepoIdentity.aliases_for_path``, which shells out to ``git remote -v``
    against the checkout on disk. Every other test here hands that projection a
    literal alias list, so the step where a real ``.git/config`` becomes a slug
    was pure inference — and if it were wrong the symptom is not a crash but a
    SECOND clone of a repository that already has one.

    Driven against a real git checkout (cloned from a local bare origin, then
    pointed at the canonical remote the registry would hold) so the assertion is
    about what git actually wrote, not about a fixture.
    """
    import shutil

    if shutil.which("git") is None:  # pragma: no cover — CI always has git
        pytest.skip("git not installed")

    def _git(*args: str) -> None:
        subprocess.run(list(args), capture_output=True, text=True, check=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("git", "-C", str(seed), "init", "-b", "main")
    _git("git", "-C", str(seed), "config", "user.email", "t@e.com")
    _git("git", "-C", str(seed), "config", "user.name", "t")
    (seed / "README.md").write_text("hi\n")
    _git("git", "-C", str(seed), "add", "-A")
    _git("git", "-C", str(seed), "commit", "-m", "init")

    checkout = tmp_path / "checkout"
    _git("git", "clone", str(seed), str(checkout))
    # What the checkout route leaves behind: origin pointing at the repository's
    # own clone URL, credential-free.
    _git("git", "-C", str(checkout), "remote", "set-url", "origin", "https://github.com/acme/beacon")

    aliases = RepoIdentity.aliases_for_path(str(checkout))

    assert "github.com/acme/beacon" in aliases, (
        "the registry slug must appear in the aliases, or a checked-out "
        "repository reads as un-checked-out and gets cloned again"
    )
