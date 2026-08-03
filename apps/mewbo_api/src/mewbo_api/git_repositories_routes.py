"""Product-wide repository registry — ``/v1/git/repositories*``.

A *repository* is a git remote Mewbo knows about, recorded independently of any
wiki ``Project`` row. That separation is the point: a ``Project`` is written
exclusively at index finalize, so binding the two would mean a repository could
not exist without first paying for an LLM indexing run, and every consumer that
merely wants to *name* a repo (the task composer, the wiki wizard, the
credential UI) would invent its own notion of one.

**Registration is inert, and that is the point.** ``POST`` normalizes the URL,
validates it, dedupes on the slug and persists a row. It does not clone, does
not ``ls-remote``, does not probe reachability and does not start an index. A
consumer opts into that work later, deliberately. The happy path touches no
network at all, which is what makes registering a repository free enough to be
the first thing a user does.

``POST .../<slug>/checkout`` is that later, deliberate opt-in for agentic tasks:
ONE explicit, labelled action that clones the repository into a managed project
and links it, surfacing on the next read as ``usage.tasks``. It is a separate
verb precisely so registration can stay free — a clone spends network, disk and
a credential, and nothing should spend those because a list was rendered or a
menu was opened.

This lives at top level rather than under ``wiki/`` for the same reason
``wiki/git_credentials_routes.py`` mounts at ``/v1/git/*``: the registry is a
product-level surface with several expected consumers, and the wiki is only one
of them. It goes further, though — it is registered from ``backend.py``
directly, NOT from ``init_wiki``, because that function returns early on an
install without the optional ``wiki`` extra. Agentic tasks run on exactly such a
base install, so a registry mounted from there would be missing precisely where
it is needed. Every reach into ``mewbo_graph`` is therefore confined to the two
I/O edges below, which answer its absence differently on purpose:
:class:`RepositoryUsageSources` decorates a READ, so each leg degrades
independently and the endpoint keeps serving with ``usage.wiki`` reading null;
:class:`RepositoryCheckout` IS the work of a write, so it refuses with a 503
naming the extra to install rather than reporting a checkout it did not make.

The domain lives DOWN in ``mewbo_core.workspaces.repositories`` (the ``RepositoryRef``
grammar, the ``Repository`` record and its ``to_wire`` DTO seam) and
``mewbo_core.workspaces.repository_store`` (idempotent ``register``, re-validating
``patch``). This module owns only what those cannot: the request bodies, the
refusal envelope, the join that turns three stores into a ``RepositoryUsage``,
and the create-project-then-clone ordering a checkout needs.

``/v1/git`` is a deliberate prefix reuse: the console's nginx and vite proxies
already forward that prefix, so no proxy change is needed to reach these routes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, final

from flask import Blueprint, jsonify, request
from mewbo_core.common import get_logger
from mewbo_core.workspaces.repositories import (
    Repository,
    RepositoryCredentialUsage,
    RepositoryPatch,
    RepositoryRef,
    RepositoryTaskUsage,
    RepositoryUsage,
    RepositoryWikiUsage,
)
from mewbo_core.workspaces.repository_store import RepositoryStoreBase, create_repository_store
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mewbo_api.auth.guard_registry import guard
from mewbo_api.errors import (
    CapabilityUnavailable,
    RequestInvalid,
    ResourceNotFound,
    StateConflict,
)
from mewbo_api.repo_identity import RepoIdentity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

logging = get_logger(name="api.git_repositories")

#: Bound to ``BaseModel`` so :meth:`GitRepositoriesController._parse` returns the
#: concrete wire model it was handed, not a widened base.
_WireModel = TypeVar("_WireModel", bound=BaseModel)


# ---------------------------------------------------------------------------
# Wire models — the request bodies, transport only
# ---------------------------------------------------------------------------


class RepositoryCreate(BaseModel):
    """``POST /v1/git/repositories`` body.

    ``extra="forbid"`` is load-bearing, not hygiene. ``slug``, ``platform``,
    ``origin``, ``createdAt``, ``updatedAt`` and ``usage`` are SERVER-owned: they
    are derived from the URL or from a join over data the server already holds.
    A client that sends one is telling us something we would have to ignore, and
    silently ignoring it is how a caller ends up believing it set a field it did
    not. The forbid turns each into a clean 400 naming the field.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    repo_url: str = Field(
        alias="repoUrl",
        description="Clone URL of the repository, e.g. https://github.com/owner/repo.",
    )
    default_branch: str | None = Field(
        default=None,
        alias="defaultBranch",
        description="Branch consumers should prefer. Recorded as-is, never verified.",
    )
    name: str | None = Field(
        default=None, description="Display name. Defaults to the repository's own name."
    )
    description: str | None = Field(default=None, description="Free-form description.")

    @field_validator("default_branch", "name", "description")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        """Strip surrounding whitespace; a whitespace-only value means absent."""
        if value is None:
            return None
        return value.strip() or None


class RepositoryCheckoutRequest(BaseModel):
    """``POST /v1/git/repositories/<slug>/checkout`` body — deliberately EMPTY.

    A checkout has no knobs, and the empty model is how that is ENFORCED rather
    than merely implied. Everything a caller might reach for is already decided:
    the remote comes from the registered record, the path is allocated by the
    project store, and no ref is pinned — a recorded ``defaultBranch`` is
    accepted as-is and never verified, so pinning it would turn one stale value
    into a hard clone failure when the remote's own HEAD is both correct and
    free. Without ``extra="forbid"`` a client sending ``{"branch": "main"}``
    would get a 201 and believe it had pinned a branch; with it, the same body
    is a 400 naming the field. An absent body and ``{}`` are both valid.
    """

    model_config = ConfigDict(extra="forbid")


class RepositoryUpdate(BaseModel):
    """``PATCH /v1/git/repositories/<slug>`` body — every field optional.

    Deliberately NARROWER than the core ``RepositoryPatch`` it converts into:
    that model also accepts ``repoUrl`` and ``platform``, which this surface
    treats as server-owned. The slug IS the repository's identity — pages,
    credentials and task bindings all resolve through it — so re-pointing a
    registered slug at a different remote is a delete-and-re-register, never a
    PATCH. ``extra="forbid"`` makes that refusal a clean 400 naming the field
    instead of a silent no-op.

    "Omitted" and "explicitly null" are distinguished via ``model_fields_set``
    (see :meth:`to_patch`), so ``{"description": null}`` clears a description
    while a body that never mentions it leaves the stored value alone.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    default_branch: str | None = Field(
        default=None,
        alias="defaultBranch",
        description="Branch consumers should prefer. Recorded as-is, never verified.",
    )
    name: str | None = Field(default=None, description="Display name.")
    description: str | None = Field(default=None, description="Free-form description.")

    @field_validator("default_branch", "name", "description")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        """Strip surrounding whitespace; a whitespace-only value clears the field."""
        if value is None:
            return None
        return value.strip() or None

    def to_patch(self) -> RepositoryPatch:
        """Convert to the core patch, carrying the set/unset distinction across.

        Built from the EXPLICITLY sent fields only, so the core model's
        ``exclude_unset`` read means the same thing it would have if the client
        had posted to core directly — an omitted field stays untouched, an
        explicit null clears.
        """
        return RepositoryPatch(**{name: getattr(self, name) for name in self.model_fields_set})


# ---------------------------------------------------------------------------
# The I/O edge — every optional-extra read lives here and only here
# ---------------------------------------------------------------------------


@final
class RepositoryUsageSources:
    """Reads the three surfaces a repository can already be used by.

    This is the ONLY class in the feature that touches an optional dependency,
    and that isolation is the design rather than a nicety. ``mewbo_graph`` (the
    ``wiki`` extra) supplies the wiki projects and the credential registry; a
    base install has neither. Each leg is guarded independently and degrades to
    an empty result, so a graph-less deployment serves the whole surface with
    ``usage.wiki`` and ``usage.credential`` reading null — never a 500, and
    never a route that silently fails to mount.

    Collaborators arrive as fields so a test drives the controller with a stub
    and no store, no graph and no filesystem.
    """

    def __init__(self, runtime: Any = None, *, project_store: Any = None) -> None:
        """Bind the runtime the wiki store hangs off, plus the managed-project store.

        The two are injected differently because they BECOME available at
        different times, not out of inconsistency. ``project_store`` exists from
        the top of ``backend.py`` and is passed directly, matching how
        ``InstructionValueSources`` already takes it. ``runtime.wiki_store`` is
        set by ``init_wiki``, which may run after this is constructed — so the
        runtime is held and the store resolved PER READ. Capturing it here would
        pin ``None`` forever and disable the wiki half of the projection on a
        machine that HAS the extra, a failure indistinguishable from not having
        it. Late resolution makes this independent of init ORDER as well as of
        the extra's presence.

        A test injects any object carrying a ``wiki_store`` attribute
        (``SimpleNamespace`` is enough); ``None`` for both is the honest
        base-install shape.
        """
        self._runtime = runtime
        self._project_store = project_store

    @property
    def _wiki_store(self) -> Any:
        """The wiki store if this install has one, else ``None``."""
        return getattr(self._runtime, "wiki_store", None)

    def wiki_usage(self) -> dict[str, RepositoryWikiUsage] | None:
        """Slug → wiki usage for every indexed project, or ``None`` if we can't know.

        **``None`` and ``{}`` are NOT interchangeable, and the difference is the
        whole reason this returns an Optional.** ``None`` means this deployment
        has no wiki surface to ask (no ``wiki`` extra, or the store read failed),
        so nothing can be said about any repository. ``{}`` means the surface
        answered and simply has no indexed projects — a repository absent from
        it is genuinely NOT INDEXED.

        The console branches on exactly that: it offers to generate a wiki for a
        repository that is merely un-indexed, and explains that it cannot for a
        deployment that has no wiki at all. Collapsing both onto ``{}`` (or onto
        ``null``) makes the pane offer an action the server would refuse, or
        withhold one it would accept.

        A store failure resolves to ``None`` rather than ``{}`` for the same
        reason: "we could not look" is not "there is no index", and usage is a
        best-effort decoration that must never take down the list it decorates.
        """
        store = self._wiki_store
        if store is None:
            return None
        try:
            projects = store.list_projects()
        except Exception as exc:
            logging.warning("repository usage: wiki project read failed ({})", exc)
            return None
        return {
            project.slug: RepositoryWikiUsage(
                indexed=True, indexed_at=project.indexed_at, pages=project.pages
            )
            for project in projects
        }

    def credential_scopes(self) -> list[str]:
        """Every stored credential scope, as a plain ``host`` or ``host/owner/repo``.

        Returns strings rather than ``CredentialScope`` objects deliberately: the
        matching rule the controller applies (a host scope covers every repo on
        that host) is expressible on strings, which keeps the pure join free of
        a ``mewbo_graph`` type it would otherwise have to import.
        """
        store = self._wiki_store
        if store is None:
            return []
        try:
            from mewbo_graph.wiki.credentials import CredentialStore  # noqa: PLC0415

            return [scope.value for scope in CredentialStore.list(store)]
        except ImportError:
            # The base-install path: no ``wiki`` extra, so no credential store.
            return []
        except Exception as exc:  # pragma: no cover - a store hiccup is not fatal here
            logging.warning("repository usage: credential read failed ({})", exc)
            return []

    def managed_projects(self) -> list[dict[str, Any]]:
        """Managed projects paired with the repo aliases their checkout resolves to.

        ``RepoIdentity.aliases_for_path`` shells out to ``git remote -v``, so
        this is the one genuinely I/O-bearing leg — and the reason the list
        endpoint reads it ONCE rather than per row. Worktrees are skipped: they
        are children of a parent project that is itself listed, and including
        them would report one repository as several bindings.
        """
        store = self._project_store
        if store is None:
            return []
        try:
            projects = store.list_projects()
        except Exception as exc:  # pragma: no cover - a store hiccup is not fatal here
            logging.warning("repository usage: managed project read failed ({})", exc)
            return []
        rows: list[dict[str, Any]] = []
        for project in projects:
            if getattr(project, "is_worktree", False):
                continue
            path = getattr(project, "path", "") or ""
            if not path:
                continue
            rows.append(
                {
                    "projectId": project.project_id,
                    "name": project.name,
                    "path": path,
                    "aliases": RepoIdentity.aliases_for_path(path),
                }
            )
        return rows


@final
class RepositoryCheckout:
    """Puts a registered repository ON DISK, as a managed project a task can run in.

    The ONE explicit action that turns an inert registry row into something an
    agent can work in, and it stays explicit: registration still clones nothing,
    and nothing here ever fires as a side effect of a read, a list or a menu
    opening. A clone spends network, disk and (on a private repo) a credential —
    a user must ask for it.

    Like :class:`RepositoryUsageSources` this is an I/O EDGE, and the same
    isolation rule applies for the same reason: it is the only other class here
    that reaches into ``mewbo_graph``, and it does so through a LAZY in-method
    import. A base install has no ``wiki`` extra, so a module-level import would
    take down the whole registry — including the read routes a task-only install
    genuinely uses — over a capability it was never going to call. Absent the
    extra this raises a 503 that NAMES the missing extra instead: a refusal an
    operator can act on, never a 500 and never a silent degrade. (Reaching for
    the git executor across the wiki plugin's package is the honest interim, not
    the end state — the hardened executor is a general git capability that
    happens to live inside an optional library, and moving it is tracked
    separately.)

    Collaborators are fields — the project store, the runtime the credential
    store hangs off, and the clone budget — so a test drives the whole contract
    with a fake store and a stub clone, no git and no network.
    """

    #: Wire error codes, spelled ONCE (mirroring the controller's own block).
    CHECKOUT_UNAVAILABLE: str = "checkout_unavailable"
    CHECKOUT_FAILED: str = "checkout_failed"

    #: Wall-clock cap on the clone, in seconds. A timeout is NOT an auth failure,
    #: so the executor aborts the whole credential chain on one rather than
    #: advancing to the next candidate — which is what makes this bound the
    #: REQUEST and not merely one attempt. Sized to sit inside both gunicorn's
    #: 300s worker timeout and the console proxy's 300s ``proxy_read_timeout``
    #: with headroom for the rest of the request. When it trips, the partial
    #: checkout is discarded, the managed project is deleted, and the caller gets
    #: a 503 saying the clone timed out. Synchronous-and-bounded is deliberate: a
    #: job/status machine for one operation a user is already waiting on would be
    #: more moving parts than the problem has.
    CLONE_TIMEOUT_SECONDS: int = 240

    def __init__(
        self,
        project_store: Any = None,
        *,
        runtime: Any = None,
        timeout: int | None = None,
    ) -> None:
        """Bind the managed-project store, the runtime, and the clone budget."""
        self._project_store = project_store
        self._runtime = runtime
        self._timeout = timeout or self.CLONE_TIMEOUT_SECONDS

    def run(self, repository: Repository) -> RepositoryTaskUsage:
        """Clone *repository* into a fresh managed project and return the binding.

        Order matters. The project is created FIRST because the store is what
        allocates the path — it is the only thing that knows the projects home
        and mints the id the path is named for — so cloning elsewhere and moving
        the result afterwards would mean re-implementing that allocation badly.

        ``create_project`` is called WITHOUT a path, which is not a detail: a
        ``path_source="provided"`` project is permanently deleted by the worktree
        reaper once it has no worktree children, so a checkout registered that
        way would evaporate under the user.

        **The seeded ``CLAUDE.md`` and the repository's own.** ``create_project``
        writes a Mewbo ``CLAUDE.md`` into the new folder, and ``git clone``
        refuses a non-empty target. The executor's ``reset_dir`` empties the
        directory before each attempt, so the seeded file is gone before git runs
        and the clone succeeds — and it is deliberately NOT written back
        afterwards. The repository's instructions are the repository's: if it
        ships its own ``CLAUDE.md`` that file arrives with the clone and wins,
        and if it ships none, synthesizing one would plant an untracked file in a
        working tree that every ``git status`` reports and an agent could commit
        upstream. A checkout is the repository, not a Mewbo folder with a
        repository inside it.

        On any failure the just-created project is deleted, so a failed checkout
        never leaves a managed project pointing at an empty directory.
        """
        clone = self._resolve_clone()
        if self._project_store is None:
            # Unreachable through ``init_git_repositories`` (``backend.py`` builds
            # the project store before it mounts this). Refused rather than left
            # to surface as an ``AttributeError`` 500 for a caller that composed
            # the controller itself.
            raise CapabilityUnavailable.for_capability(
                self.CHECKOUT_UNAVAILABLE,
                "checking out a repository needs the managed-project store, which "
                "is not configured on this deployment.",
            )
        project = self._project_store.create_project(
            repository.display_name, f"Checkout of {repository.slug}"
        )
        try:
            outcome = clone(
                repository.clone_url(),
                project.path,
                store=self._credential_store,
                slug=repository.slug,
                timeout=self._timeout,
            )
        except Exception as exc:
            self._discard(project)
            logging.warning("repository checkout raised slug={} ({})", repository.slug, exc)
            raise self._failed(repository, str(exc)) from exc
        if not outcome.ok:
            self._discard(project)
            raise self._failed(repository, outcome.stderr)
        logging.info(
            "repository checkout ready slug={} project={}", repository.slug, project.project_id
        )
        return RepositoryTaskUsage(
            project_id=project.project_id, name=project.name, path=project.path
        )

    # ── the optional-dependency edge ────────────────────────────────────────

    @property
    def _credential_store(self) -> Any:
        """The wiki store the credential chain resolves through, or ``None``.

        Resolved PER CALL off the runtime for the same reason
        :class:`RepositoryUsageSources` does it — ``init_wiki`` may run after
        this is constructed. ``None`` is a SUPPORTED value, not a failure:
        ``resolve_chain`` then yields the anonymous candidate alone, which is
        exactly right for a public repository on a deployment that stores no
        credentials.
        """
        return getattr(self._runtime, "wiki_store", None)

    def _resolve_clone(self) -> Any:
        """Return the hardened clone function, or raise the 503 naming the extra.

        The import is in-method BECAUSE it can fail: this is the only reach into
        the optional ``wiki`` extra on a write path, and the read routes sharing
        this module must neither pay for it nor be taken down by it.
        """
        try:
            from mewbo_graph.plugins.wiki.clone import clone_working_checkout  # noqa: PLC0415
        except ImportError as exc:
            raise CapabilityUnavailable.for_capability(
                self.CHECKOUT_UNAVAILABLE,
                "checking out a repository needs the optional git capability, which "
                "this deployment does not have installed: install the 'wiki' extra "
                "(mewbo-api[wiki]) and restart the API. Registering, listing and "
                "editing repositories are unaffected.",
            ) from exc
        return clone_working_checkout

    # ── cleanup + refusals ──────────────────────────────────────────────────

    def _discard(self, project: Any) -> None:
        """Delete a project whose checkout failed. Best-effort, never re-raises.

        The clone failure is what the caller must hear about; a store that then
        also fails to clean up must not replace that message with its own.
        """
        try:
            self._project_store.delete_project(project.project_id)
        except Exception as exc:  # pragma: no cover - cleanup must not mask the cause
            logging.warning(
                "repository checkout could not discard project={} ({})",
                project.project_id,
                exc,
            )

    def _failed(self, repository: Repository, detail: str) -> CapabilityUnavailable:
        """The 503 a failed clone earns, carrying git's own (redacted) reason.

        503 rather than 500: nothing in Mewbo malfunctioned — the remote refused,
        was unreachable, or took too long — and git's stderr rides along because
        it is the only thing that distinguishes "add a credential" from "fix the
        URL". The executor has already redacted every credential it tried out of
        that string; never hand a caller a raw stderr from anywhere else.
        """
        reason = (detail or "the clone failed").strip()
        return CapabilityUnavailable.for_capability(
            self.CHECKOUT_FAILED, f"could not check out {repository.slug}: {reason}"
        )


# ---------------------------------------------------------------------------
# The controller — one atomic class, collaborators injected as fields
# ---------------------------------------------------------------------------


@final
class GitRepositoriesController:
    """Owns the registry's read/validate/write/project orchestration.

    Collaborators are fields; every rule — the duplicate check, wiki adoption,
    the usage join, each refusal — is a method. The Flask surface below is a thin
    adapter that constructs nothing and decides nothing, so the whole contract is
    drivable from a test with two stubs and no app.

    **Wire error codes.** Refusals ride the canonical
    ``{"error": {code, reason, retryable}}`` envelope with SEMANTIC codes rather
    than the numeric status, so a client branches on meaning:
    ``invalid_repo_url`` (400), ``invalid_request`` (400 — a malformed or
    server-owned field), ``repository_exists`` (409), ``repository_not_found``
    (404).
    """

    #: What a repository looks like to a deployment that HAS a wiki surface but
    #: has never indexed it. ONE shared default rather than a fresh object per
    #: row — safe because a usage model is built, dumped and discarded, never
    #: mutated — and deliberately not ``None``; see :meth:`usage_for`. ``pages``
    #: is ``0``, never null: the count is known to be zero, and the console
    #: types it as a number.
    NOT_INDEXED: RepositoryWikiUsage = RepositoryWikiUsage(
        indexed=False, indexed_at=None, pages=0
    )

    #: The ONE place a wire error code is spelled. The console branches on these
    #: strings (``api/repositories.ts`` types the same closed set), so a route
    #: that mints one and a client that reads it cannot drift apart.
    INVALID_REPO_URL: str = "invalid_repo_url"
    INVALID_REQUEST: str = "invalid_request"
    REPOSITORY_EXISTS: str = "repository_exists"
    REPOSITORY_NOT_FOUND: str = "repository_not_found"

    def __init__(
        self,
        store: RepositoryStoreBase,
        *,
        usage: RepositoryUsageSources | None = None,
        checkout: RepositoryCheckout | None = None,
    ) -> None:
        """Bind the repository store and the two I/O edges (usage read, checkout)."""
        self._store = store
        self._usage = usage or RepositoryUsageSources()
        self._checkout = checkout or RepositoryCheckout()

    # ── reads ───────────────────────────────────────────────────────────────

    def list(self) -> dict[str, Any]:
        """Every registered repository, wiki-adopted and usage-decorated.

        Adoption runs here rather than in a migration because the two lists must
        not be allowed to diverge in the first place: a wiki-indexed repository
        is BY DEFINITION a repository Mewbo knows about, so a wiki project whose
        slug is missing from the registry is a registry that is simply wrong.
        Reconciling on read is idempotent and self-healing — it fixes rows a
        failed write dropped and rows a consumer forgets to register, with no
        migration to run and nothing to re-run if it fails.

        The three usage sources are read ONCE here and passed down, so the join
        stays O(rows) with no per-row fan-out.
        """
        wiki = self._usage.wiki_usage()
        self._adopt_wiki_projects(wiki)
        credentials = self._usage.credential_scopes()
        managed = self._usage.managed_projects()
        return {
            "repositories": [
                repository.to_wire(self.usage_for(repository, wiki, credentials, managed))
                for repository in self._store.list()
            ]
        }

    def read(self, slug: str) -> dict[str, Any]:
        """One repository's DTO, or raise the 404 envelope."""
        repository = self._require(slug)
        return repository.to_wire(
            self.usage_for(
                repository,
                self._usage.wiki_usage(),
                self._usage.credential_scopes(),
                self._usage.managed_projects(),
            )
        )

    # ── writes ──────────────────────────────────────────────────────────────

    def create(self, body: Any) -> tuple[dict[str, Any], int]:
        """Register a repository. Inert: no clone, no probe, no index.

        Raises 400 ``invalid_repo_url`` for a URL that does not parse into a
        ``host/owner/repo`` identity, 400 ``invalid_request`` for a malformed or
        server-owned field, and 409 ``repository_exists`` when the slug is taken.

        The duplicate check is explicit rather than delegated to the store,
        whose ``register`` is deliberately an idempotent MERGE — the right
        behaviour for a wiki index enriching a row, and the wrong answer for a
        person who believes they are adding something new.
        """
        payload = self._parse(RepositoryCreate, body)
        ref = self._parse_ref(payload.repo_url)
        if self._store.get(ref.slug) is not None:
            raise self._duplicate(ref.slug)
        # ``platform`` is deliberately NOT passed: it is a pure function of the
        # host, so it is derived on the model beside host/owner/repo rather than
        # stamped by each caller — the same reason this does not pass those three
        # either.
        stored = self._store.register(
            Repository(
                slug=ref.slug,
                remote_url=payload.repo_url.strip(),
                default_branch=payload.default_branch,
                name=payload.name or ref.repo,
                description=payload.description,
                origin="manual",
            )
        )
        logging.info("repository registered slug={} origin=manual", stored.slug)
        # The two STORE-backed usage legs are resolved (both are cheap reads, and
        # a credential saved before the repository was registered is the common
        # order). The ``tasks`` leg is deliberately NOT: it shells out to
        # ``git remote -v`` per managed project, and registration doing local git
        # I/O is precisely what "registration is inert" rules out. So ``tasks``
        # is null on this response and resolved on the next read — the one place
        # in this surface where a null leg means "not looked at" rather than
        # "not present".
        return (
            stored.to_wire(
                self.usage_for(
                    stored, self._usage.wiki_usage(), self._usage.credential_scopes(), []
                )
            ),
            201,
        )

    def patch(self, slug: str, body: Any) -> dict[str, Any]:
        """Apply a partial update to the descriptive fields; return the fresh DTO."""
        patch = self._parse(RepositoryUpdate, body).to_patch()
        if self._store.patch(slug, patch) is None:
            raise self._not_found(slug)
        logging.info("repository updated slug={} fields={}", slug, sorted(patch.model_fields_set))
        return self.read(slug)

    def checkout(self, slug: str, body: Any) -> tuple[dict[str, Any], int]:
        """Give *slug* a working checkout on disk, and return its fresh DTO.

        The endpoint that closes the gap registration deliberately leaves open: a
        registered repository is inert, so an agentic task has nothing to run in
        until someone asks for a checkout. 201 when one was made, 200 when the
        repository already had one.

        **Idempotent through the SAME rule the read path reports.** The existing
        binding is looked up with :meth:`_task_usage` over the live managed
        projects — not through a rule of its own — so a client can never see
        ``usage.tasks: null`` and then be refused a checkout, nor see a binding
        and get a second clone. Agreeing with what the UI renders IS the point:
        the button a user sees and the guard the server applies read one fact.
        (Corollary of reusing it: the match is :class:`RepoIdentity`'s alias
        rule, whose ``owner``/``repo`` are the LAST TWO slug segments — so a
        GitLab subgroup repository, whose slug carries more, is not recognised as
        already-checked-out and a second call clones again. That gap belongs to
        the projection and is shared with ``usage.tasks``; closing it for this
        one caller would make the two disagree, which is worse.)

        The DTO is returned by re-READING rather than by predicting it, so the
        response states what the next ``GET`` will state — including the case
        where the checkout landed but the projection cannot see it.
        """
        self._parse(RepositoryCheckoutRequest, body if body is not None else {})
        repository = self._require(slug)
        if self._task_usage(repository, self._usage.managed_projects()) is not None:
            return self.read(slug), 200
        self._checkout.run(repository)
        return self.read(slug), 201

    def delete(self, slug: str) -> None:
        """Deregister a repository. Deletes NOTHING else.

        Explicitly NOT a cascade. A wiki index is deleted through
        ``DELETE /v1/wiki/projects/<slug>``, and a credential through
        ``DELETE /v1/git/credentials/<scope>``. Making deregistration destroy
        either would mean a user tidying a list they thought was a bookmark
        silently threw away a paid indexing run — and a host-scoped credential
        is shared by every repo on that host, so cascading it would break repos
        the caller never named. Note that the next :meth:`list` re-adopts a slug
        that still has a wiki index; that is the reconciliation working, not a
        failed delete.
        """
        if not self._store.delete(slug):
            raise self._not_found(slug)
        logging.info("repository deregistered slug={}", slug)

    # ── the usage projection (pure — every input arrives as an argument) ─────

    def usage_for(
        self,
        repository: Repository,
        wiki: dict[str, RepositoryWikiUsage] | None,
        credentials: Iterable[str],
        # ``Sequence``, not ``list``: this class defines a ``list`` METHOD, which
        # shadows the builtin inside the class body — a bare ``list[...]``
        # annotation here resolves to that method rather than the type.
        managed: Sequence[dict[str, Any]],
    ) -> RepositoryUsage:
        """Join what every consumer knows about *repository* into one model.

        PURE: every input arrives as an argument, so this is exhaustively
        testable with three literals and no store. No network, no ``ls-remote``,
        no per-row fetch — the I/O already happened at the edge.

        The ``wiki`` leg carries a three-state answer, not a two-state one (see
        :meth:`RepositoryUsageSources.wiki_usage`). A deployment with no wiki
        surface reports ``null``; a deployment that HAS one reports a real
        ``indexed: false`` for a repository it has never indexed, rather than
        the same ``null``. Only the first is "we cannot say".
        """
        return RepositoryUsage(
            wiki=None if wiki is None else wiki.get(repository.slug, self.NOT_INDEXED),
            tasks=self._task_usage(repository, managed),
            credential=self._credential_usage(repository, credentials),
        )

    def _task_usage(
        self, repository: Repository, managed: Sequence[dict[str, Any]]
    ) -> RepositoryTaskUsage | None:
        """The managed project whose checkout IS this repository, if any.

        Matched through ``RepoIdentity`` aliases rather than by path or name, so
        one repository resolves whether the project was created from its SSH
        remote, its HTTPS remote or a mirror host.
        """
        for project in managed:
            if repository.slug in project["aliases"]:
                return RepositoryTaskUsage(
                    project_id=project["projectId"],
                    name=project["name"],
                    path=project["path"],
                )
        return None

    def _credential_usage(
        self, repository: Repository, credentials: Iterable[str]
    ) -> RepositoryCredentialUsage | None:
        """The narrowest stored credential scope covering this repository.

        Mirrors the resolution chain's host-covers-repo rule: an exact
        ``host/owner/repo`` scope wins over the bare ``host`` scope that also
        covers it, which is the order a clone would actually try them in.
        Reports the SCOPE only — never a value, never a hint.
        """
        host_scope: str | None = None
        for scope in credentials:
            if scope == repository.slug:
                return RepositoryCredentialUsage(scope=scope, scope_type="repo")
            if scope == repository.host:
                host_scope = scope
        if host_scope is None:
            return None
        return RepositoryCredentialUsage(scope=host_scope, scope_type="host")

    # ── adoption ────────────────────────────────────────────────────────────

    def _adopt_wiki_projects(self, wiki: dict[str, RepositoryWikiUsage] | None) -> None:
        """Register any wiki-indexed slug the registry does not already hold.

        ``None`` (no wiki surface, or the read failed) adopts nothing — there is
        no evidence to adopt from, and treating "we could not look" as "there
        are no projects" is inert here but would be a bug in any future caller
        that inverted it.

        Best-effort per slug: a project whose slug is not a parseable
        ``host/owner/repo`` identity (a bare two-segment slug) is skipped rather
        than failing the whole list —
        ``RepositoryRef.coerce`` is the tolerant read-path parse for exactly
        this. Registration goes through the store's idempotent ``register``, so
        a concurrent write converges instead of duplicating.
        """
        if not wiki:
            return
        known = {repository.slug for repository in self._store.list()}
        for slug in wiki:
            if slug in known:
                continue
            ref = RepositoryRef.coerce(slug)
            if ref is None:
                logging.debug("repository adoption skipped unparseable wiki slug={}", slug)
                continue
            self._store.register(Repository(slug=ref.slug, name=ref.repo, origin="wiki"))
            logging.info("repository adopted from wiki index slug={}", ref.slug)

    # ── helpers + refusals ──────────────────────────────────────────────────

    def _require(self, slug: str) -> Repository:
        """Return the repository for *slug*, or raise the 404 envelope."""
        repository = self._store.get(slug)
        if repository is None:
            raise self._not_found(slug)
        return repository

    def _parse_ref(self, repo_url: str) -> RepositoryRef:
        """Normalize a clone URL into its canonical identity, or raise a 400."""
        try:
            return RepositoryRef.from_url(repo_url)
        except ValueError as exc:
            raise self._invalid_repo_url(repo_url, str(exc)) from exc

    def _parse(self, model: type[_WireModel], body: Any) -> _WireModel:
        """Validate a request body against *model*, or raise the 400 envelope.

        The ``ValidationError`` unwrapping is the taxonomy's
        (``RequestInvalid.from_validation_error``), not a second copy: which
        failure is reported and how its location is rendered is one app-wide
        convention, and this surface differs only in the wire ``code`` it wants.
        """
        if not isinstance(body, dict):
            raise RequestInvalid.field_error(
                "body",
                "request body must be a JSON object",
                code=self.INVALID_REQUEST,
                shape="envelope",
            )
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise RequestInvalid.from_validation_error(
                exc, code=self.INVALID_REQUEST, shape="envelope"
            ) from exc

    def _invalid_repo_url(self, repo_url: str, detail: str) -> RequestInvalid:
        """The 400 an unparseable clone URL earns."""
        return RequestInvalid.field_error(
            "repoUrl",
            f"{repo_url!r} is not a parseable git repository URL: {detail}",
            code=self.INVALID_REPO_URL,
            shape="envelope",
        )

    def _duplicate(self, slug: str) -> StateConflict:
        """The 409 an already-registered slug earns."""
        return StateConflict.for_reason(
            f"repository {slug} is already registered",
            code=self.REPOSITORY_EXISTS,
            shape="envelope",
        )

    def _not_found(self, slug: str) -> ResourceNotFound:
        """The 404 an unknown slug earns."""
        return ResourceNotFound.for_reason(
            f"repository {slug} is not registered", code=self.REPOSITORY_NOT_FOUND
        )


# ---------------------------------------------------------------------------
# Flask surface — a thin adapter over the one injected controller
# ---------------------------------------------------------------------------


def build_blueprint(controller: GitRepositoriesController) -> Blueprint:
    """Build the ``/v1/git`` repositories blueprint bound to *controller*.

    Every handler is two lines: read the request, delegate. The controller's
    ``ApiError`` raises map to the wire through the already-registered
    ``register_api_error_handler``, so no handler catches or renders anything.

    ``<path:slug>`` (not ``<string:slug>``) because a slug is
    ``host/owner/repo`` — it contains slashes.
    """
    bp = Blueprint("git_repositories", __name__)

    @bp.route("/repositories", methods=["GET"])
    @guard.requires("repositories.read")
    def list_repositories():
        """List every registered repository with its usage projection."""
        return jsonify(controller.list())

    @bp.route("/repositories", methods=["POST"])
    @guard.requires("repositories.write")
    def create_repository():
        """Register a repository. Does not clone, probe or index it."""
        body, status = controller.create(request.get_json(silent=True))
        return jsonify(body), status

    @bp.route("/repositories/<path:slug>", methods=["GET"])
    @guard.requires("repositories.read")
    def get_repository(slug: str):
        """Read one registered repository by its ``host/owner/repo`` slug."""
        return jsonify(controller.read(slug))

    @bp.route("/repositories/<path:slug>", methods=["PATCH"])
    @guard.requires("repositories.write")
    def patch_repository(slug: str):
        """Update a repository's default branch, display name or description."""
        return jsonify(controller.patch(slug, request.get_json(silent=True)))

    @bp.route("/repositories/<path:slug>/checkout", methods=["POST"])
    @guard.requires("repositories.write")
    def checkout_repository(slug: str):
        """Clone the repository into a managed project an agentic task can run in.

        The trailing literal is unambiguous only because POST has no sibling on
        ``/repositories/<slug>``: a repository whose own name is ``checkout``
        reads here as the action on its parent, and answers the 404 naming the
        truncated slug it looked up.
        """
        body, status = controller.checkout(slug, request.get_json(silent=True))
        return jsonify(body), status

    @bp.route("/repositories/<path:slug>", methods=["DELETE"])
    @guard.requires("repositories.write")
    def delete_repository(slug: str):
        """Deregister a repository. Never deletes its wiki index or credential."""
        controller.delete(slug)
        return "", 204

    return bp


def init_git_repositories(app, runtime=None, *, project_store=None) -> GitRepositoriesController:
    """Mount ``/v1/git/repositories*`` and return the composed controller.

    Called from ``backend.py`` unconditionally — this surface must exist on a
    base install (see the module docstring). *runtime* is handed to
    :class:`RepositoryUsageSources`, which resolves the optional wiki store off
    it lazily, so this may be called before or after ``init_wiki`` with the same
    result.
    """
    controller = GitRepositoriesController(
        create_repository_store(),
        usage=RepositoryUsageSources(runtime, project_store=project_store),
        checkout=RepositoryCheckout(project_store, runtime=runtime),
    )
    app.register_blueprint(build_blueprint(controller), url_prefix="/v1/git")
    return controller


__all__ = [
    "GitRepositoriesController",
    "RepositoryCheckout",
    "RepositoryCheckoutRequest",
    "RepositoryCreate",
    "RepositoryUpdate",
    "RepositoryUsageSources",
    "build_blueprint",
    "init_git_repositories",
]
