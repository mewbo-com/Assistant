"""Durable repository-credential store + the canonical git-auth resolution chain.

The domain model is Pydantic end-to-end — :class:`CredentialScope` (what a
credential is keyed by), :class:`CredentialSource` (which tier resolved it), and
:class:`CredentialCandidate` (one entry of the chain). Each owns its own
validation and behaviour, so the rules below are stated ONCE here and mirrored
nowhere:

``CredentialStore`` is an atomic class: all state lives in the injected
``WikiStoreBase``; this class is the single read/write chokepoint, and its
identity ``_encode``/``_decode`` seam is the ONE place a cipher would land —
nothing else in the tree reads a stored blob, so no other call site would
change. The SEAM is the cheap part; encryption-at-rest is not. It additionally
needs a key to exist somewhere, and this codebase has no key-management
substrate to draw one from, plus a way to keep reading the plaintext blobs
already written. Scope it as a project, not a swap.
Keyed by :class:`CredentialScope` — either a full slug
(``host/owner/repo``, repo-specific) or a bare host (``git.example.home``,
shared by every repo on that host).

**The sharing rule lives on the model** (:meth:`CredentialScope.covers`): a HOST
scope covers every repo scope on that host; a REPO scope covers only itself.
That single predicate IS the "shared vs pinned" semantics the store, the chain,
and the API registry all rely on — never re-derive it from a ``"/" in scope``
check at a call site.

``resolve_chain`` is the ONE place git-auth precedence is defined, for every
consumer (clone, ls-remote/branches, freshness, description fetch):

    arg token → repo-scoped store → host-scoped store → ambient git
    credential (``git credential fill``, read-only) → anonymous

The database and the ambient (built-in) git credential store are the only two
sources of truth. There is deliberately no in-process token cache: the durable
store is written at submission time and survives the process, so a cache adds
a third source that can drift — and that drift, a revoked stored token
shadowing a valid ambient one with no fallback, makes re-indexes fail in
cascade.

SECURITY: credentials are plaintext-at-rest in their own isolated store
(mode 0600 on the JSON driver, dedicated collection on Mongo) but MUST be
redacted in-flight — never log ``RepoCredential.value``, never echo it into an
SSE event, a session transcript, or a tool result.
"""
from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from mewbo_core.common import get_logger
from mewbo_core.workspaces.repositories import RepositoryRef
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mewbo_graph.wiki.types import RepoCredential

if TYPE_CHECKING:
    from mewbo_graph.wiki.store import WikiStoreBase

logging = get_logger(name="mewbo_graph.wiki.credentials")

#: Case-insensitive stderr substrings that classify a git failure as an
#: AUTH failure (worth retrying with the next credential in the chain). Every
#: marker is ANCHORED to genuine git/HTTP auth text — a bare ``401``/``403``/
#: ``not found`` substring misfired on unrelated network noise (e.g. a
#: ``port 8403: Connection refused`` was read as a 403 auth rejection, burning
#: the chain and emitting a spurious "credential rejected" warning). ``repository
#: not found`` is kept deliberately: GitHub masks private repos as 404 for
#: unauthenticated/unauthorized clients (``remote: Repository not found.``).
_AUTH_FAILURE_MARKERS = (
    "authentication failed",
    "failed to authenticate",
    "could not read username",
    "could not read password",
    "invalid username or password",
    "http basic: access denied",
    "terminal prompts disabled",
    "could not read from remote repository",
    "permission denied (publickey)",
    "repository not found",
    "error: 401",
    "error: 403",
    "http 401",
    "http 403",
)


# ---------------------------------------------------------------------------
# CredentialScope — what a credential is keyed by
# ---------------------------------------------------------------------------


class CredentialScope(BaseModel):
    """The key a credential is stored under: a bare host, or a repo slug.

    Frozen + validated at definition, so an invalid scope cannot exist: parsing
    a scope IS validating it. The three shapes a caller can throw at
    :meth:`from_repo_url` — an ``https://`` URL, an scp-style ``git@host:o/r``
    remote, and a bare ``host/owner/repo`` slug — are recognised by the ONE
    shared grammar in ``mewbo_core.workspaces.repositories.RepositoryRef.split_remote``;
    what a scope does with those parts is defined HERE and only here (the FE
    mirrors the same rules in ``api/git.ts``; the backend keeps the one
    authoritative definition the wire reflects).

    **Depth is deliberately uncapped past ``host/owner/repo``.** A GitLab
    subgroup (``gitlab.com/group/subgroup/project``) is a legitimate 4-segment
    slug, and a 2-segment ``owner/repo`` slug is stored in the wild too — so
    ``owner``/``repo`` read the LAST TWO segments (the rule
    ``finalize._split_owner_repo`` shares) rather than fixed indices.
    Rejecting deeper scopes would make a subgroup repo's credential
    unresolvable, silently downgrading its clone to anonymous.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str

    @field_validator("value", mode="before")
    @classmethod
    def _normalise(cls, v: Any) -> Any:
        """Normalise BEFORE validation: trim, drop a trailing ``.git``, lowercase host.

        A scope typed/derived as ``https://Git.Home/o/repo.git/`` must key the
        SAME credential the clone chain looks up under ``git.home/o/repo`` — the
        slug form never carries a ``.git`` suffix, so without this a credential
        saved from a copy-pasted remote URL would be stored but never resolve.
        Only the HOST segment is lowercased: owner/repo are case-sensitive on
        some forges, and lowercasing them would re-key existing credentials.
        """
        if not isinstance(v, str):
            return v
        s = v.strip().strip("/")
        s = s.removesuffix(".git").strip("/")
        segments = s.split("/")
        segments[0] = segments[0].lower()
        return "/".join(segments)

    @model_validator(mode="after")
    def _reject_malformed(self) -> CredentialScope:
        """Reject ONLY the genuinely broken — a scope that no lookup could ever key.

        Unrejected, a malformed scope fails SILENTLY and late: it simply misses
        every store lookup, so the chain falls through to anonymous and a private
        repo dies with an opaque git error. Fail fast instead.

        The rejected set is deliberately NARROW, because the store holds real
        rows and an over-strict rule would make an existing credential
        un-listable and un-deletable. Rejected: empty/whitespace-only, embedded
        whitespace, a URL scheme (that belongs in :meth:`from_repo_url`, not a
        store key), an empty interior segment (``host//repo``), and an un-parsed
        scp remote (``git@host:o/r``) leaking in as a key.

        NOT rejected, all first-class:

        - **A bare host with no dot** — private TLDs (``.home``/``.local``) are
          first-class, and a single-label hostname is legal. A no-slash scope IS
          the shared-host shape (and a catalog project's slug, e.g.
          ``my-workspace``, is shape-identical).
        - **``host:port``** — a legal authority, and the FE's ``hostOf`` derives a
          scope from ``u.host``, which KEEPS the port. Rejecting it would hide an
          already-stored credential from the registry.
        - **Any segment depth** — see the class docstring (GitLab subgroups,
          2-segment ``owner/repo``).
        """
        value = self.value
        if not value:
            raise ValueError("scope must not be empty")
        if any(ch.isspace() for ch in value):
            raise ValueError(f"scope must not contain whitespace: {value!r}")
        if "://" in value:
            raise ValueError(
                f"scope must be a host or host/owner/repo slug, not a URL: {value!r}"
            )
        segments = value.split("/")
        if not all(segments):
            raise ValueError(f"scope must not contain an empty segment: {value!r}")
        if "@" in segments[0]:
            raise ValueError(
                f"scope host carries userinfo — parse the remote with "
                f"from_repo_url instead of keying on it: {value!r}"
            )
        return self

    # ── behaviour (the rules live ON the model) ─────────────────────────────

    @property
    def kind(self) -> Literal["host", "repo"]:
        """``"host"`` for a bare host (shared), ``"repo"`` for a slug (pinned)."""
        return "repo" if "/" in self.value else "host"

    @property
    def host(self) -> str:
        """The (lowercased) host this scope belongs to — always the first segment."""
        return self.value.split("/", 1)[0]

    @property
    def owner(self) -> str | None:
        """The owner/org segment, or ``None`` for a host scope."""
        segments = self.value.split("/")
        return segments[-2] if len(segments) >= 2 else None

    @property
    def repo(self) -> str | None:
        """The repository segment, or ``None`` for a host scope."""
        segments = self.value.split("/")
        return segments[-1] if len(segments) >= 2 else None

    def host_scope(self) -> CredentialScope:
        """Return the HOST scope this scope lives under (a host scope returns itself).

        This is the parent the chain falls back to: a repo with no repo-scoped
        credential inherits the shared host one.
        """
        return self if self.kind == "host" else CredentialScope(value=self.host)

    def covers(self, other: CredentialScope) -> bool:
        """True when a credential stored at THIS scope may authenticate *other*.

        THE SHARING RULE, stated once: a HOST scope covers every repo scope on
        that host (that is what "shared across every repo on this host" means);
        a REPO scope covers only itself. A different host is never covered — so
        a credential can never leak across hosts.
        """
        if self.host != other.host:
            return False
        return self.kind == "host" or self == other

    # ── parsing (the ONE place a remote/slug becomes a scope) ───────────────

    @classmethod
    def from_slug(cls, slug: str) -> CredentialScope:
        """Parse a bare host or ``host/owner/repo`` slug. Raises on a malformed scope."""
        return cls(value=slug)

    @classmethod
    def from_repo_url(cls, url: str) -> CredentialScope:
        """Parse a git remote — URL, scp-style, or bare slug — into a repo scope.

        The remote GRAMMAR is not re-implemented here: it delegates to
        ``RepositoryRef.split_remote`` (core), the ONE place the three shapes
        are recognised, shared with the api's ``RepoIdentity`` so the two cannot
        disagree about what a remote means. The scp branch it covers is
        reachable, not defensive: the wizard accepts a ``git@host:owner/repo``
        remote and, with no slug chosen yet, that raw string arrives here as the
        resolution scope, where naive string-splitting would yield a garbled
        ``git@host:owner`` host.

        What stays HERE is the PROJECTION, and it is deliberately this store's
        own: a reference with no structural host keeps every token as a path
        segment, so a lone ``git.example.com`` reads as a bare HOST scope. A
        repository reference reads that same lone token as a repo NAME — the two
        conventions are genuinely different and merging them would break one.
        Raises on anything that cannot be read as a remote.
        """
        host, segments = RepositoryRef.split_remote(url)
        parts = (host, *segments) if host else segments
        return cls(value="/".join(parts))

    @classmethod
    def coerce(cls, value: str | CredentialScope | None) -> CredentialScope | None:
        """Tolerant parse for READ paths that must degrade, never raise.

        The chain and the "is a credential on file?" probes run on whatever slug
        a job/route carries; a malformed one must not blow up a clone that would
        otherwise succeed anonymously. Returns ``None`` (⇒ "no scope, no stored
        credential") instead — the fail-fast lives at the WRITE boundaries
        (:meth:`from_slug` / the API route), where a bad scope is user input.
        """
        if isinstance(value, CredentialScope):
            return value
        if not value:
            return None
        try:
            return cls.from_repo_url(value)
        except ValueError:
            logging.warning("ignoring malformed credential scope")
            return None

    def __str__(self) -> str:
        """The store key — a scope IS its string in every wire/log context."""
        return self.value


# ---------------------------------------------------------------------------
# CredentialSource / CredentialCandidate — one entry of the resolution chain
# ---------------------------------------------------------------------------


class CredentialSource(str, Enum):
    """Which tier of :func:`resolve_chain` produced a candidate.

    A ``str`` enum so it keeps comparing/serialising like the bare source string
    this replaced (log keys, event payloads) while gaining an exhaustive,
    typo-proof domain.
    """

    ARG = "arg"
    STORE_REPO = "store:repo"
    STORE_HOST = "store:host"
    AMBIENT = "ambient"
    ANONYMOUS = "anonymous"

    @property
    def is_stored(self) -> bool:
        """True for the durable-store tiers — the only ones with a scope to blame."""
        return self in (CredentialSource.STORE_REPO, CredentialSource.STORE_HOST)

    def scope_for(self, scope: CredentialScope) -> CredentialScope | None:
        """The scope a STORED candidate came from, else ``None``.

        Behaviour on the model so a caller that must name the rejected scope (the
        clone's "update it in Settings" warning) reads it off the source instead
        of re-deriving repo-vs-host from a string. arg/ambient/anonymous have no
        stored scope — there is nothing for a user to fix.
        """
        if self is CredentialSource.STORE_REPO:
            return scope
        if self is CredentialSource.STORE_HOST:
            return scope.host_scope()
        return None


class CredentialCandidate(BaseModel):
    """One entry of the resolution chain: a source tier + the credential it found.

    Frozen. The invariant enforced below — ``credential is None`` IFF the source
    is ``anonymous`` — is what lets every consumer treat "no credential" and
    "anonymous tier" as the same thing without re-checking both.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: CredentialSource
    credential: RepoCredential | None = None

    @model_validator(mode="after")
    def _anonymous_iff_no_credential(self) -> CredentialCandidate:
        """Anonymous is the ONLY credential-less tier, and it is never credential-ed."""
        anonymous = self.source is CredentialSource.ANONYMOUS
        if anonymous and self.credential is not None:
            raise ValueError("the anonymous candidate must not carry a credential")
        if not anonymous and self.credential is None:
            raise ValueError(f"a {self.source.value} candidate requires a credential")
        return self

    @property
    def _token_credential(self) -> RepoCredential | None:
        """The credential IFF it is a token — the ONE narrowing the accessors share."""
        cred = self.credential
        return cred if cred is not None and cred.kind == "token" else None

    @property
    def is_token(self) -> bool:
        """True when this candidate authenticates with a git token."""
        return self._token_credential is not None

    @property
    def token(self) -> str | None:
        """The token value, or ``None`` for an ssh-key/anonymous candidate."""
        cred = self._token_credential
        return cred.value if cred is not None else None

    @property
    def ssh_key(self) -> str | None:
        """The private-key material, or ``None`` for a token/anonymous candidate."""
        cred = self.credential
        return cred.value if cred is not None and cred.kind == "ssh_key" else None

    @property
    def username(self) -> str | None:
        """The token's own username (GitLab ``oauth2``/deploy tokens), if any.

        Only meaningful for a token: it is injected into the URL beside the
        token, so an ssh-key credential's username must NOT leak into that path.
        """
        cred = self._token_credential
        return cred.username if cred is not None else None


class CredentialStore:
    """Static façade over the store's credential primitives, keyed by scope."""

    @staticmethod
    def _encode(cred: RepoCredential) -> dict[str, Any]:
        """Serialise a credential for at-rest storage. Identity today.

        The ONE place a future cipher lands: encrypt the returned blob here and
        decrypt in :meth:`_decode`; nothing else in the codebase changes.
        """
        return cred.model_dump(mode="json")

    @staticmethod
    def _decode(blob: dict[str, Any]) -> RepoCredential | None:
        """Deserialise an at-rest blob back into a credential (None if malformed).

        Drops the ``scope`` sidecar key first (:meth:`save` persists it INSIDE
        the blob so ``list_credentials`` recovers the exact scope without
        reversing a lossy filename — see the store's ``list_credentials``);
        it is store metadata, not a ``RepoCredential`` field (which is
        ``extra="forbid"``).
        """
        payload = {k: v for k, v in blob.items() if k != "scope"}
        try:
            return RepoCredential.model_validate(payload)
        except Exception:
            logging.warning("skipping malformed credential blob")
            return None

    @classmethod
    def save(cls, store: WikiStoreBase, scope: CredentialScope, cred: RepoCredential) -> None:
        """Persist *cred* for *scope* (overwrites any prior credential).

        The *scope* is stamped INTO the encoded blob (``blob["scope"]``) — the
        one place scope is bound to the credential at rest — so the JSON driver's
        ``list_credentials`` reads it back verbatim instead of reverse-engineering
        it from a filename (``__``→``/``, which corrupts a scope that itself
        contains a literal ``__``). :meth:`_decode` strips it back off on read.
        """
        stamped = cred.model_copy(
            update={"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        )
        blob = cls._encode(stamped)
        blob["scope"] = scope.value
        store.save_credentials(scope.value, blob)

    @classmethod
    def load(cls, store: WikiStoreBase, scope: CredentialScope) -> RepoCredential | None:
        """Return the durable credential for *scope*, or None if absent/malformed."""
        blob = store.get_credentials(scope.value)
        if blob is None:
            return None
        return cls._decode(blob)

    @staticmethod
    def delete(store: WikiStoreBase, scope: CredentialScope) -> bool:
        """Delete *scope*'s credential; return True if one was removed."""
        return store.delete_credentials(scope.value)

    @classmethod
    def list(cls, store: WikiStoreBase) -> dict[CredentialScope, RepoCredential]:
        """Return every stored credential keyed by scope (malformed entries skipped).

        A key that no longer parses as a scope is skipped with a warning, exactly
        as a malformed blob is: it could never have been resolved by the chain
        (which parses the same way), so surfacing it would advertise a credential
        that cannot authenticate anything.
        """
        out: dict[CredentialScope, RepoCredential] = {}
        for raw_scope, blob in store.list_credentials().items():
            scope = CredentialScope.coerce(raw_scope)
            cred = cls._decode(blob)
            if scope is not None and cred is not None:
                out[scope] = cred
        return out


def ambient_credential(host: str) -> RepoCredential | None:
    """Read the built-in git credential for *host* via ``git credential fill``.

    Strictly READ-ONLY: we never call ``credential approve``/``reject``, so
    git's own credential store is never written (a read-only mounted
    ``~/.git-credentials`` made that write fail with EBUSY and mask real auth
    errors). Prompting is disabled, so an absent credential fails fast.
    """
    if not host:
        return None
    import os  # noqa: PLC0415

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost={host}\n\n".encode(),
            capture_output=True,
            timeout=10,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    fields = dict(
        line.split("=", 1)
        for line in (proc.stdout or b"").decode(errors="ignore").splitlines()
        if "=" in line
    )
    password = fields.get("password", "").strip()
    if not password:
        return None
    return RepoCredential(
        kind="token", value=password, username=fields.get("username") or None
    )


def resolve_chain(
    store: WikiStoreBase | None,
    slug: str | CredentialScope | None,
    *,
    arg_token: str | None = None,
) -> Iterator[CredentialCandidate]:
    """Yield the ordered, deduped credential candidates for *slug* — LAZILY.

    Order: explicit arg → repo-scoped store → host-scoped store → ambient git
    credential → anonymous. Consumers iterate, advancing on auth-class failures
    only (see :func:`is_auth_failure`).

    A GENERATOR by design: each store read and — critically — the ambient
    ``git credential fill`` subprocess (a 10s-capped fork) run ONLY when
    iteration actually advances to that tier. A consumer whose arg/stored
    candidate authenticates on the first attempt never pays for the ambient
    probe — an eager form would do two store loads plus the ambient fork on
    every call, even a freshness check a repo-scoped token satisfies at once.

    Dedup key is ``RepoCredential.dedup_key`` (kind, value, username) so two
    credentials that share a value but differ in username (e.g. a GitLab
    ``oauth2`` deploy token vs a PAT) are BOTH tried rather than the second being
    silently dropped. Anonymous is ALWAYS the terminal candidate. ``store`` may
    be ``None`` (or *slug* empty/malformed) — then only the anonymous candidate
    is yielded (a store-less freshness probe).
    """
    seen: set[tuple[str, str, str | None]] = set()

    def _fresh(cred: RepoCredential) -> bool:
        if cred.dedup_key in seen:
            return False
        seen.add(cred.dedup_key)
        return True

    if arg_token:
        arg_cred = RepoCredential(kind="token", value=arg_token)
        if _fresh(arg_cred):
            yield CredentialCandidate(source=CredentialSource.ARG, credential=arg_cred)

    scope = CredentialScope.coerce(slug)
    # Both durable tiers AND the ambient probe are gated on having a store+scope:
    # a store-less caller (a freshness probe with no wiki store) resolves to
    # anonymous alone and must not pay for the ``git credential fill`` fork.
    if store is not None and scope is not None:
        repo_cred = CredentialStore.load(store, scope)
        if repo_cred is not None and _fresh(repo_cred):
            yield CredentialCandidate(
                source=CredentialSource.STORE_REPO, credential=repo_cred
            )
        host_scope = scope.host_scope()
        if host_scope != scope:
            host_cred = CredentialStore.load(store, host_scope)
            if host_cred is not None and _fresh(host_cred):
                yield CredentialCandidate(
                    source=CredentialSource.STORE_HOST, credential=host_cred
                )

        ambient = ambient_credential(scope.host)
        if ambient is not None and _fresh(ambient):
            yield CredentialCandidate(
                source=CredentialSource.AMBIENT, credential=ambient
            )

    yield CredentialCandidate(source=CredentialSource.ANONYMOUS)


def is_auth_failure(stderr: str) -> bool:
    """True when a git failure reads as an auth rejection (retry next credential)."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


__all__ = [
    "CredentialCandidate",
    "CredentialScope",
    "CredentialSource",
    "CredentialStore",
    "ambient_credential",
    "is_auth_failure",
    "resolve_chain",
]
