#!/usr/bin/env python3
"""Product-level repository identity + the registry's domain models.

Three consumers parse a git remote into an identity — the wiki's
``CredentialScope``, the api's ``RepoIdentity`` and the console's ``slug.ts`` —
and a private grammar in each is a grammar they disagree on.
:class:`RepositoryRef` is the ONE they all
delegate to: :meth:`RepositoryRef.split_remote` recognises the three remote
shapes (``https://`` URL, ``ssh://`` URL, scp-style ``git@host:owner/repo``) plus
a bare slug, strips a trailing ``.git``, lowercases the host and drops leading
and trailing slashes (an INTERIOR empty segment is deliberately preserved — see
:meth:`RepositoryRef.split_remote`). Each consumer keeps its own documented
PROJECTION of those parts (a credential scope reads a lone token as a host; a
project reference reads it as a repo name) — only the grammar is shared, because
that is the part that actually duplicated.

This lives in ``mewbo_core`` and not in the graph library on purpose: the
repository registry is reachable from a BASE install (agentic tasks), while
``mewbo-graph`` is an optional extra of the api. Core is the only layer both the
wiki and the base-install api can import down into.

Registration is deliberately INERT. Nothing here clones, indexes, calls a model
or touches the network — a :class:`Repository` is normalized, validated and
persisted, and consumers (wiki, tasks) opt in later. Anything a consumer knows
about a repository rides :class:`RepositoryUsage`, a pure projection the REST
layer fills in and hands back; the models never reach out for it.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Git-hosting platform a repository lives on. Declared here (not in the graph
#: library) because both the wiki wire types and the base-install registry need
#: it; ``mewbo_graph.wiki.types`` re-exports this name so every existing
#: importer keeps working.
PlatformId = Literal["github", "gitlab", "bitbucket", "gitea", "azure", "git"]

#: Who introduced a repository to the registry. ``manual`` = a person added it,
#: ``wiki`` = a wiki index registered it on the way past, ``task`` = an agentic
#: task did. First writer wins — see :meth:`RepositoryStoreBase.register`.
RepositoryOrigin = Literal["manual", "wiki", "task"]

#: Public host → the platform running there. The ONE home for this mapping: the
#: wizard's platform catalogue (`mewbo_api.wiki.catalogues`) carries the same
#: hosts for its presentation cards, but that module is graph-gated and lives in
#: an app, so it cannot be the source a base-install registry reads. Matching is
#: EXACT host or dot-subdomain (``myorg.visualstudio.com`` → azure), never a
#: substring: ``gitea`` appearing in a hostname is not evidence, and a
#: self-hosted GitLab at ``git.example.com`` is genuinely unknowable — ``"git"``
#: is the honest answer there, which is why it is the fallback rather than a
#: guess. A persisted platform is server-owned and rendered to users, so a wrong
#: guess is a lie that sticks; the wizard can afford a guess because a user can
#: click a different tile, and a registry row cannot.
PLATFORM_HOSTS: dict[str, PlatformId] = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
    "gitea.com": "gitea",
    "codeberg.org": "gitea",
    "dev.azure.com": "azure",
    "visualstudio.com": "azure",
}


class RepositoryRef(BaseModel):
    """The canonical identity of one git repository: host, owner, repo.

    Frozen + validated at definition, so an invalid ref cannot exist: parsing a
    ref IS validating it. All three parts are required and non-empty — a
    registry entry with no host can neither compose a remote URL nor resolve a
    host-scoped credential, so a host-less reference (a bare ``owner/repo``
    slug, a bare repo name) is not a repository identity. Use :meth:`coerce`
    where that must degrade rather than raise.

    **``namespace`` is what keeps ``slug`` lossless.** A GitLab subgroup
    (``gitlab.com/group/sub/proj``) has more segments than the triple can hold,
    and dropping the intermediate ones would silently re-key the project's
    pages, jobs and credentials onto a different slug. So ``host`` stays a
    genuine DNS host (the FIRST segment — which is what a host-scoped credential
    is shared by), ``owner``/``repo`` stay the LAST TWO segments (the rule the
    wiki's finalize and credential store have always used), and everything in
    between is preserved here. ``slug`` re-joins all of it, so
    ``from_slug(s).slug == s`` for every normalized slug.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    owner: str
    repo: str
    namespace: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _reject_empty_parts(self) -> RepositoryRef:
        """Reject a ref missing any part of the triple, or bearing whitespace.

        Fails fast at the parse boundary rather than late and silently: a ref
        with an empty segment stringifies to a slug that misses every store
        lookup, which is exactly how a malformed credential scope used to
        degrade a private clone to anonymous with no visible cause.
        """
        for label, value in (("host", self.host), ("owner", self.owner), ("repo", self.repo)):
            if not value:
                raise ValueError(f"repository {label} must not be empty")
            if any(ch.isspace() for ch in value):
                raise ValueError(f"repository {label} must not contain whitespace: {value!r}")
        if not all(self.namespace):
            raise ValueError("repository namespace must not contain an empty segment")
        return self

    # ── behaviour (the rules live ON the model) ─────────────────────────────

    @property
    def slug(self) -> str:
        """The canonical ``host[/namespace…]/owner/repo`` slug.

        This IS the registry's primary key and the wiki's project slug — one
        string, so a repository resolves the same way on every surface.
        """
        return "/".join((self.host, *self.namespace, self.owner, self.repo))

    @property
    def platform(self) -> PlatformId:
        """The forge this ref's host implies; ``"git"`` when it implies none.

        On the model because the platform is intrinsic to the host and nothing
        else — see :data:`PLATFORM_HOSTS` for the matching rule and for why an
        unrecognised host resolves to the neutral ``"git"`` rather than a guess.
        """
        for host, platform in PLATFORM_HOSTS.items():
            if self.host == host or self.host.endswith(f".{host}"):
                return platform
        return "git"

    def https_url(self) -> str:
        """Compose the conventional ``https://`` clone URL for this ref.

        A fallback for a record with no stored ``remote_url``; a persisted
        remote always wins, since it carries the scheme/port the user actually
        used. Never guessed from the platform name — the host is the host.
        """
        return f"https://{self.slug}"

    def __str__(self) -> str:
        """A ref IS its slug in every wire/log context."""
        return self.slug

    # ── parsing (the ONE grammar every consumer delegates to) ───────────────

    @classmethod
    def split_remote(cls, value: str) -> tuple[str, tuple[str, ...]]:
        """Split a remote reference into ``(host, path_segments)``.

        THE shared grammar. Recognises the three shapes Mewbo sees in the wild
        plus a bare slug, and normalizes while it parses:

        - ``https://host/owner/repo(.git)`` / ``ssh://git@host:port/owner/repo``
          — a scheme URL; the host comes from :func:`urlparse` (which already
          lowercases it and drops the port and any credentials).
        - ``[user@]host:owner/repo(.git)`` — scp-style; the host ends at the
          FIRST colon. Detected by a colon in the first path segment rather than
          by the presence of ``@``: a colon there can only be an scp authority
          or a port, and both belong to the host.
        - ``host/owner/repo`` or ``owner/repo`` — no structural host at all, so
          the returned host is ``""`` and EVERY token comes back as a segment.
          Deciding whether a lone token is a host or a repo name is the caller's
          projection, not the grammar's — the two live consumers genuinely
          disagree, and pretending otherwise would break one of them.

        A trailing ``.git`` is stripped from the last segment only, and leading
        and trailing slashes are dropped, so ``https://host/o/repo.git/`` and
        ``host/o/repo`` yield identical parts. An INTERIOR empty segment
        (``host/o//repo``) is deliberately preserved rather than collapsed:
        each consumer already decides what it means (a credential scope refuses
        it outright, a project reference filters it out), and silently healing
        it here would change both.
        """
        raw = value.strip()
        if not raw:
            return "", ()
        if "://" in raw:
            parsed = urlparse(raw)
            return (parsed.hostname or "").lower(), cls._path_segments(parsed.path)
        first_segment = raw.split("/", 1)[0]
        if ":" in first_segment:
            authority, _, tail = raw.partition(":")
            return authority.rsplit("@", 1)[-1].lower(), cls._path_segments(tail)
        return "", cls._path_segments(raw)

    @staticmethod
    def _path_segments(path: str) -> tuple[str, ...]:
        """Split a remote's path into ordered segments, ``.git`` stripped."""
        stripped = path.strip().strip("/")
        if not stripped:
            return ()
        segments = stripped.split("/")
        segments[-1] = segments[-1].removesuffix(".git")
        return tuple(segments)

    @classmethod
    def from_parts(cls, host: str, segments: tuple[str, ...]) -> RepositoryRef:
        """Build a ref from an already-split ``(host, segments)`` pair.

        When *host* is empty the FIRST segment is taken as the host — a bare
        ``host/owner/repo`` slug names its own host. Raises when what is left
        cannot fill the triple.
        """
        parts = (host, *segments) if host else segments
        if len(parts) < 3:
            joined = "/".join(parts)
            raise ValueError(f"not a repository identity (need host/owner/repo): {joined!r}")
        return cls(
            host=parts[0].lower(),
            namespace=tuple(parts[1:-2]),
            owner=parts[-2],
            repo=parts[-1],
        )

    @classmethod
    def from_url(cls, url: str) -> RepositoryRef:
        """Parse a git remote — URL, scp-style, or bare slug — into a ref.

        The raising entry point for WRITE boundaries, where a reference that
        cannot name a repository is user input to reject, not a condition to
        degrade around.
        """
        host, segments = cls.split_remote(url)
        return cls.from_parts(host, segments)

    @classmethod
    def from_slug(cls, slug: str) -> RepositoryRef:
        """Parse a ``host/owner/repo`` slug. Raises on a URL or a short slug.

        A scheme belongs in :meth:`from_url`, not in a store key — accepting one
        here would let the same repository be registered under two spellings.
        """
        if "://" in slug:
            raise ValueError(f"expected a host/owner/repo slug, not a URL: {slug!r}")
        host, segments = cls.split_remote(slug)
        return cls.from_parts(host, segments)

    @classmethod
    def coerce(cls, value: str | RepositoryRef | None) -> RepositoryRef | None:
        """Tolerant parse for READ paths that must degrade, never raise.

        Returns ``None`` (⇒ "not a repository identity") for anything
        unparseable — a two-segment wiki slug, a blank, a stray token.
        The fail-fast lives at the WRITE boundaries (:meth:`from_url` /
        :meth:`from_slug`), mirroring ``CredentialScope.coerce``.
        """
        if isinstance(value, RepositoryRef):
            return value
        if not value:
            return None
        try:
            return cls.from_url(value)
        except ValueError:
            return None


class Repository(BaseModel):
    """One registered repository — the registry's persisted record.

    Registration is INERT: this record says a repository is known to Mewbo and
    nothing more. No clone, no index, no credential, no network. ``slug`` is the
    identity and the primary key; ``host``/``owner``/``repo`` are DERIVED from
    it by the validator below rather than accepted alongside it, so the two can
    never drift apart in the store (a stored document re-validates to the same
    triple it was written with).

    ``created_at``/``updated_at`` default to empty because this model owns no
    clock — the store stamps them at write time, the same seam
    ``CredentialStore.save`` uses. An empty pair reads as "not yet persisted".
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    slug: str
    host: str = ""
    owner: str = ""
    repo: str = ""
    remote_url: str | None = Field(default=None, alias="repoUrl")
    platform: PlatformId = "git"
    default_branch: str | None = Field(default=None, alias="defaultBranch")
    name: str | None = None
    description: str | None = None
    origin: RepositoryOrigin = "manual"
    created_at: str = Field(default="", alias="createdAt")
    updated_at: str = Field(default="", alias="updatedAt")

    @model_validator(mode="before")
    @classmethod
    def _derive_identity(cls, data: Any) -> Any:
        """Normalize ``slug`` and stamp what the slug implies, BEFORE validation.

        Runs on every construction AND every re-validation of a stored
        document, so ``host``/``owner``/``repo`` are consistent by construction
        rather than by the caller's care. A caller may omit them entirely; one
        supplied out of step with the slug is corrected rather than trusted,
        because the slug is the key everything else resolves through.

        ``platform`` is derived here too — it is a function of the host and of
        nothing else, so the server is the only thing that can know it, and
        deriving it beside the triple means it cannot drift from ``host`` and
        needs no migration for rows written before the mapping existed. Two
        rules keep it from overwriting a fact it does not own:

        - a platform the CALLER supplied is never touched, so an operator's
          explicit label on a self-hosted forge survives every re-validation;
        - a host that implies NOTHING stamps nothing, leaving the field at its
          neutral default and — critically — leaving it UNSET. A stamped value
          counts as set, and :meth:`merged_with` reads "unset" off exactly that,
          so stamping the neutral ``"git"`` would make every re-registration of
          a self-hosted repository clobber the operator's stored label.
        """
        if not isinstance(data, dict):
            return data
        raw_slug = data.get("slug")
        if not isinstance(raw_slug, str):
            return data
        ref = RepositoryRef.from_slug(raw_slug)
        derived = {"slug": ref.slug, "host": ref.host, "owner": ref.owner, "repo": ref.repo}
        if "platform" not in data and ref.platform != "git":
            derived["platform"] = ref.platform
        return {**data, **derived}

    @property
    def ref(self) -> RepositoryRef:
        """The identity this record is keyed by."""
        return RepositoryRef.from_slug(self.slug)

    @property
    def display_name(self) -> str:
        """The label to show — the operator's override, else ``owner/repo``."""
        return self.name or f"{self.owner}/{self.repo}"

    def clone_url(self) -> str:
        """The remote to clone: the persisted one, else the conventional https.

        A stored ``remote_url`` always wins — it carries the scheme, port and
        path the user actually pasted, which a composed URL cannot recover.
        """
        return self.remote_url or self.ref.https_url()

    def merged_with(self, incoming: Repository) -> Repository:
        """Fold a re-registration of the SAME slug onto this record.

        The idempotency rule, stated once: a field the incoming record leaves
        unset keeps the value already on file, a field it fills wins, and
        ``created_at``/``origin`` are immutable after the first write — they
        record WHEN and by WHOM the repository entered the registry, which a
        later re-registration by a different consumer must not rewrite. Clearing
        a field is :class:`RepositoryPatch`'s job, never registration's.

        Unset is read off ``exclude_unset``, NOT off the value, because
        ``platform`` has a meaningful non-empty default: a re-registration that
        says nothing about the platform would otherwise clobber a stored
        ``github`` back to the neutral ``git``.
        """
        updates = {
            key: value
            for key, value in incoming.model_dump(exclude_unset=True).items()
            if value is not None and value != ""
        }
        updates.pop("created_at", None)
        updates.pop("origin", None)
        return Repository.model_validate({**self.model_dump(), **updates})

    def to_wire(self, usage: RepositoryUsage | None = None) -> dict[str, Any]:
        """Serialize to the camelCase wire shape, with *usage* folded in.

        The ONE place the DTO is assembled, so no route hand-rolls the dict.
        *usage* arrives as an ARG rather than being resolved here: it is a join
        across the wiki store, the project store and the credential store, all
        of which are I/O this model must never reach for.
        """
        payload = self.model_dump(mode="json", by_alias=True)
        payload["usage"] = usage.model_dump(mode="json", by_alias=True) if usage else None
        return payload


class RepositoryPatch(BaseModel):
    """The editable subset of a :class:`Repository` — a partial update.

    ``extra="forbid"`` is load-bearing rather than hygiene: ``slug``/``host``/
    ``owner``/``repo``/``created_at`` are server-owned identity, and a client
    smuggling one gets a clean 400 instead of a silent no-op or a re-keyed row.
    Re-pointing a registered repository at a different remote identity is a
    delete + register, never a patch.

    Unset and explicitly-null are DISTINCT: an absent field leaves the stored
    value alone, an explicit ``null`` clears it. :meth:`apply` reads that
    distinction off ``exclude_unset``, so a caller must not pre-fill defaults.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    remote_url: str | None = Field(default=None, alias="repoUrl")
    platform: PlatformId | None = None
    default_branch: str | None = Field(default=None, alias="defaultBranch")
    name: str | None = None
    description: str | None = None

    def apply(self, repository: Repository) -> Repository:
        """Return *repository* with this patch's SET fields applied.

        Re-validates rather than ``model_copy``-ing: a copy skips validators, so
        an update that violated an invariant would land in the store unchecked.
        """
        updates = self.model_dump(exclude_unset=True)
        if not updates:
            return repository
        return Repository.model_validate({**repository.model_dump(), **updates})


# ---------------------------------------------------------------------------
# Usage — what each consumer knows about a repository (pure projection)
# ---------------------------------------------------------------------------


class RepositoryWikiUsage(BaseModel):
    """Whether MewboWiki has indexed this repository, and how much."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    indexed: bool = False
    indexed_at: str | None = Field(default=None, alias="indexedAt")
    pages: int | None = None


class RepositoryTaskUsage(BaseModel):
    """The managed project an agentic task would run this repository in."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project_id: str = Field(alias="projectId")
    name: str | None = None
    path: str | None = None


class RepositoryCredentialUsage(BaseModel):
    """The stored credential that covers this repository, if any.

    ``scope_type`` mirrors ``CredentialScope.kind``: ``host`` = a credential
    shared by every repository on that host, ``repo`` = one pinned to this slug.
    Served, never re-derived from the scope string at a call site.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    scope: str
    scope_type: Literal["host", "repo"] = Field(alias="scopeType")


class RepositoryUsage(BaseModel):
    """What every consumer currently knows about one repository.

    A PURE model with no I/O: each leg is resolved by whoever owns that store
    and handed in already-built. Every leg is nullable and defaults to absent,
    because "this repository has never been indexed" is the ordinary case for a
    registry whose registration is a no-op — an absent leg is a fact, not a
    failure to look.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    wiki: RepositoryWikiUsage | None = None
    tasks: RepositoryTaskUsage | None = None
    credential: RepositoryCredentialUsage | None = None


__all__ = [
    "PLATFORM_HOSTS",
    "PlatformId",
    "Repository",
    "RepositoryCredentialUsage",
    "RepositoryOrigin",
    "RepositoryPatch",
    "RepositoryRef",
    "RepositoryTaskUsage",
    "RepositoryUsage",
    "RepositoryWikiUsage",
]
