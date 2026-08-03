"""Session provenance — who/what spawned a session.

A session record carries no origin field: every session is created the same
way regardless of caller (see ``session_store``). Provenance is therefore
reconstructed from two durable signals written at creation time:

* the session's **tags** (e.g. ``wiki:job:<id>``, ``agentic_search:scg:<id>``,
  ``nextcloud-talk:room:<token>``, ``app:<app_id>``) — the robust signal,
  present for every internally-spawned session and surviving even when the
  context event is empty (older wiki jobs stored no capabilities);
* the first ``context`` event's ``client_capabilities`` / ``source_platform``
  — the fallback when a tag is absent.

``SessionOrigin`` is the single place that maps those signals to a coarse
origin. The console badges and filters the landing page on this value, so the
enum members are stable wire strings.

An advertisement is a claim, not a fact — see :class:`CapabilityEvidence` for
the sibling that decides which gated capabilities a session actually EXERCISED,
and why the origin classifier reads only the two capabilities no client sends.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

# Shared "what counts as a mobile surface" contract — used by both the
# classifier below (context fallback) and the api tagging seam that stamps
# ``mobile:<platform>`` at session creation, so the two never drift.
MOBILE_TAG_PREFIX = "mobile:"
MOBILE_SURFACES: frozenset[str] = frozenset({"android", "ios", "aura-android"})

# Owned here, not api-side, so the app-tagging seam
# (``apps/mewbo_api/src/mewbo_api/apps/lifecycle.py:MAINTAINER_TAG_PREFIX``) and this
# classifier read the SAME literal and can never drift — mirroring exactly why
# ``MOBILE_TAG_PREFIX`` exists above.
APPS_TAG_PREFIX = "app:"

# The vocabulary of every surface string ACTUALLY stamped today — the one home
# for "what can ``surface`` be", read by the operator-facing variable reference
# (``system_instructions.values.InstructionValueCatalog``). Every entry traces to
# a real stamp site, so this list can be trusted as a branch target:
#
# * ``api`` — the ``X-Mewbo-Surface`` reader's own default when a caller sends
#   no header (``mewbo_api.request_context.request_surface``);
# * ``console`` / ``mcp`` / ``android`` / ``home-assistant`` — clients that DO
#   send that header (the console's api client, the MCP facade's REST client,
#   Aura's OkHttp interceptor, the Home Assistant conversation agent);
# * ``cli`` / ``trigger`` / ``agent`` — in-process callers passing
#   ``source_platform=`` (the CLI, a trigger-driven wake, the search launcher);
# * ``email`` / ``nextcloud-talk`` — the channel adapters, which stamp their own
#   platform;
# * ``github`` / ``gitea`` — derived from the forge host by vcs-pickup;
# * ``vcs`` / ``unknown`` — the fallbacks ``TraceProvenance._resolve_surface``
#   derives below when nothing else stamped one.
#
# DELIBERATELY NOT a closed set and enforced nowhere: ``surface`` stays a plain
# ``str`` so a new client that stamps its own string keeps working. This is the
# current vocabulary, not a validator — treat it as such.
KNOWN_SURFACES: tuple[str, ...] = (
    "agent",
    "android",
    "api",
    "cli",
    "console",
    "email",
    "gitea",
    "github",
    "home-assistant",
    "mcp",
    "nextcloud-talk",
    "trigger",
    "unknown",
    "vcs",
)


def is_mobile_surface(surface: object) -> bool:
    """True when ``surface`` names a mobile client (a bare platform or Aura)."""
    if not isinstance(surface, str):
        return False
    value = surface.lower()
    return value in MOBILE_SURFACES or value.startswith("aura")


class SessionOrigin(str, Enum):
    """Coarse provenance of a session, derived from its tags + context."""

    USER = "user"
    WIKI = "wiki"
    SEARCH = "search"
    CHANNEL = "channel"
    MOBILE = "mobile"
    STRUCTURED = "structured"
    DRAFT = "draft"
    APPS = "apps"

    @classmethod
    def classify(cls, tags: list[str], context: dict[str, object]) -> SessionOrigin:
        """Map a session's tags + merged context to an origin.

        Tags win over context because they are the more reliable signal. Which
        origin a tag implies is declared once, on :class:`SessionTag` — the same
        table that BUILDS the tag — so a stamped prefix can never lack a
        classifier arm. A tag naming no origin (``scg:``, which reaches SEARCH
        through its capability context instead) and an unrecognised operator
        label alike are skipped, never masking a later tag.
        """
        for tag in tags:
            origin = SessionTag.origin_of(tag)
            if origin is not None:
                return origin
        # Mobile fallback runs BEFORE the generic source_platform->CHANNEL
        # branch below: Aura already persists context["client"] ==
        # "aura-android" (no ``mobile:`` tag), so this must be checked first
        # or a mobile source_platform would misclassify as CHANNEL.
        surface = context.get("source_platform") or context.get("client")
        if is_mobile_surface(surface):
            return cls.MOBILE
        if context.get("source_platform"):
            return cls.CHANNEL
        # Only capabilities NO client advertises about itself may imply an
        # origin. ``wiki`` and ``scg`` are written server-side by a headless job
        # that genuinely scoped the session that way; the console sends a FIXED
        # header naming every capability it can RENDER, which says nothing about
        # what the session is for. There is deliberately NO ``apps`` arm: it
        # would file every ordinary console chat under APPS, and it would be
        # redundant besides, since a real Apps session is also tagged
        # ``app:<id>`` at creation (``mewbo_api.apps.lifecycle``) and the tag
        # arm above already wins.
        capabilities = context.get("client_capabilities")
        if isinstance(capabilities, list):
            if "wiki" in capabilities:
                return cls.WIKI
            # The ONE capability arm no tag can replace: the SCG map job's
            # ``scg:map:`` row below declares ``origin=None`` and reaches SEARCH
            # through exactly this branch.
            if "scg" in capabilities:
                return cls.SEARCH
        return cls.USER


@dataclass(frozen=True)
class _CapabilityProof:
    """One row of the earned-capability table — pure data.

    *events* are transcript event kinds a capability's tool emits only AFTER its
    artifact durably landed, so they are the strongest evidence available.
    *tool_ids* are the tools the capability gates; a successful ``tool_result``
    for one is evidence too, and it is the only evidence some paths leave (an
    Apps *maintainer* session exercises the capability without ever emitting
    ``app_ready``, which fires on the builder session alone).
    """

    capability: str
    events: frozenset[str]
    tool_ids: frozenset[str]


class CapabilityEvidence:
    """Which gated capabilities a session actually EXERCISED.

    **A class label must be earned by an INVOCATION, never by an advertisement.**
    ``context.client_capabilities`` is what a CLIENT said it can render, and the
    console sends a fixed header on literally every request — so copying that set
    onto a session summary chipped every ordinary chat with capabilities it never
    used. This accumulates the opposite: what the transcript proves.

    **The rule that keeps ``wiki``/``scg`` honest AND working.** A capability is
    held to evidence exactly when this table can speak to it — i.e. when there is
    a durable artifact event or a gated tool id that PROVES use. Those are
    precisely the capabilities a client asserts about ITSELF (the four render
    surfaces below). Everything else — ``wiki`` and ``scg`` today — is written
    server-side by a headless job that really did scope the session that way, no
    client ever advertises it, and this class has nothing better to go on, so it
    passes through untouched. The table is therefore also the list of what must
    be earned: adding a row is what makes a capability answerable.

    **Why a table of literals rather than the tool registry.**
    ``SessionToolRegistry.capabilities_for`` is the live capability↔tool map, but
    it is built per-``Orchestrator`` from plugin discovery and is EMPTY in a
    process that only lists sessions — a registry lookup would blank a capability
    that really was used. Half of these ids also live one layer up
    (``mewbo_api.apps``), where core cannot import from. So each row names its
    producer instead:

    * ``stlite`` — ``widget_ready`` (``builtin_plugins.widget_builder.submit_widget``),
      tool ``submit_widget``;
    * ``ask_user`` — ``user_question`` / ``user_question_answered``
      (``mewbo_api.ask_user``), tool ``ask_user_question``;
    * ``generative_ui`` — ``generative_ui``
      (``builtin_plugins.generative_ui.present_ui``), tool ``present_ui``;
    * ``apps`` — ``app_ready`` / ``app_updated`` / ``app_issue``
      (``mewbo_api.apps.lifecycle``), tools ``submit_app`` / ``app_data`` /
      ``run_pipeline`` / ``get_app`` (``mewbo_api.apps.plugin.*``).

    Atomic and I/O-free like its :class:`SessionOrigin` sibling: events arrive as
    method arguments, so it folds into whatever pass a caller already makes over
    a transcript and needs no store, clock or session of its own.
    """

    _PROOFS: ClassVar[tuple[_CapabilityProof, ...]] = (
        _CapabilityProof(
            capability="stlite",
            events=frozenset({"widget_ready"}),
            tool_ids=frozenset({"submit_widget"}),
        ),
        _CapabilityProof(
            capability="ask_user",
            events=frozenset({"user_question", "user_question_answered"}),
            tool_ids=frozenset({"ask_user_question"}),
        ),
        _CapabilityProof(
            capability="generative_ui",
            events=frozenset({"generative_ui"}),
            tool_ids=frozenset({"present_ui"}),
        ),
        _CapabilityProof(
            capability="apps",
            events=frozenset({"app_ready", "app_updated", "app_issue"}),
            tool_ids=frozenset({"submit_app", "app_data", "run_pipeline", "get_app"}),
        ),
    )

    # Flattened once at class definition so ingest is a dict hit per event
    # rather than a scan of the table.
    _BY_EVENT: ClassVar[Mapping[str, str]] = {
        event: proof.capability for proof in _PROOFS for event in proof.events
    }
    _BY_TOOL_ID: ClassVar[Mapping[str, str]] = {
        tool_id: proof.capability for proof in _PROOFS for tool_id in proof.tool_ids
    }
    _EARNABLE: ClassVar[frozenset[str]] = frozenset(proof.capability for proof in _PROOFS)

    def __init__(self) -> None:
        """Start with nothing proven."""
        self._earned: set[str] = set()

    @classmethod
    def evidence_event_types(cls) -> frozenset[str]:
        """Event types whose mere presence proves a capability. ``O(1)``.

        Projected straight off :data:`_PROOFS` so a store that has to SELECT the
        evidence out of a transcript — rather than fold a whole one — asks this
        table instead of restating it. A hand-copied list is how a new row here
        becomes a capability that a listing silently stops reporting.
        """
        return frozenset(cls._BY_EVENT)

    @classmethod
    def evidence_tool_ids(cls) -> frozenset[str]:
        """Tool ids whose SUCCESSFUL ``tool_result`` proves a capability. ``O(1)``.

        The ``tool_result`` half of :meth:`evidence_event_types`, and the reason
        a listing needs to read only a handful of the collection's fattest event
        type rather than all of it.
        """
        return frozenset(cls._BY_TOOL_ID)

    def observe(self, event: Mapping[str, object]) -> None:
        """Fold one transcript event into the evidence set.

        A dedicated artifact event counts outright. A ``tool_result`` counts only
        when it SUCCEEDED — several of these tools are terminal-free, so a
        rejected or invalid call still leaves a result behind and would otherwise
        read as proof of an artifact that was never produced.
        """
        kind = event.get("type")
        if not isinstance(kind, str):
            return
        if kind != "tool_result":
            capability = self._BY_EVENT.get(kind)
            if capability is not None:
                self._earned.add(capability)
            return
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("success") is False:
            return
        tool_id = payload.get("tool_id")
        if isinstance(tool_id, str):
            capability = self._BY_TOOL_ID.get(tool_id)
            if capability is not None:
                self._earned.add(capability)

    def resolve(self, advertised: object) -> list[str]:
        """Project the advertised set through the evidence gathered.

        An earnable capability survives only if it was earned; anything else the
        session advertised passes through in the order the client sent it. A
        capability earned but never advertised is appended (a runtime grant is
        still a real invocation), so the field reads as "what this session did"
        end to end rather than "what it claimed plus a filter".
        """
        out: list[str] = []
        seen: set[str] = set()
        values = advertised if isinstance(advertised, list) else []
        for raw in values:
            capability = str(raw).strip()
            if not capability or capability in seen:
                continue
            if capability in self._EARNABLE and capability not in self._earned:
                continue
            seen.add(capability)
            out.append(capability)
        out.extend(sorted(self._earned - seen))
        return out


@dataclass(frozen=True)
class _TagKind:
    """One row of the session-tag grammar — pure data, read by :class:`SessionTag`.

    A row says how a tag of this kind is RECOGNISED (``head``/``subs``/
    ``min_parts`` for the strict parse, ``origin``/``origin_infix`` for the
    lenient origin read) and what it MEANS (``product``, a session type, the
    id segments it carries). Adding a session kind is one row plus one
    constructor, rather than a three-site lockstep.
    """

    product: str
    # First segment; ``None`` matches ANY head (the channel rows, whose head is
    # the platform name and therefore open-ended).
    head: str | None = None
    # Accepted second segments; ``None`` accepts any.
    subs: tuple[str, ...] | None = None
    # Segment count the FACET read requires. The origin read has no arity rule
    # beyond "has a colon" — see ``SessionTag.origin_of``.
    min_parts: int = 2
    # Origin this kind implies, or ``None`` for a kind that genuinely reaches its
    # origin some other way. Today only ``scg:`` does: its map job writes
    # ``client_capabilities: ["scg"]``, which the capability branch already reads
    # as SEARCH, so declaring one here would be redundant. Leaving a kind at
    # ``None`` is a claim that some OTHER signal covers it — verify that claim
    # against what the producer actually writes into context, not against an
    # empty dict, or the kind falls through to ``user`` unnoticed.
    origin: SessionOrigin | None = None
    # Set only when the origin read matches on an infix rather than the head
    # prefix. The channel adapters' platform segment is open-ended, so a channel
    # session has always been recognised by its ``:room:``/``:thread:`` infix.
    origin_infix: str | None = None
    # Session type when no ``type_by_sub`` entry applies; ``None`` derives it as
    # ``<product>_<sub>`` (the structured/draft/mobile families, whose second
    # segment IS the variant).
    default_type: str | None = None
    type_by_sub: Mapping[str, str] = field(default_factory=dict)
    # Facet name -> segment index. An index past the end is simply absent, which
    # is how an id-less channel room tag stays valid.
    ids: tuple[tuple[str, int], ...] = ()

    def accepts(self, parts: list[str]) -> bool:
        """True when *parts* satisfies this row's head, sub and arity rules."""
        if len(parts) < self.min_parts:
            return False
        if self.head is not None and parts[0] != self.head:
            return False
        return self.subs is None or parts[1] in self.subs

    def implies_origin(self, tag: str) -> bool:
        """True when *tag* is recognised by this row's (lenient) origin rule."""
        if self.origin is None:
            return False
        if self.origin_infix is not None:
            return self.origin_infix in tag
        return tag.startswith(f"{self.head}:")

    def facets(self, parts: list[str]) -> dict[str, str]:
        """Project accepted *parts* into product / session_type / id facets."""
        sub = parts[1]
        out = {
            "product": self.product,
            "session_type": self.type_by_sub.get(
                sub, self.default_type or f"{self.product}_{sub}"
            ),
        }
        out.update({name: parts[i] for name, i in self.ids if i < len(parts)})
        return out


@dataclass(frozen=True)
class SessionTag:
    """A session routing key — the ONE home of the tag grammar, both ways.

    A tag is a ``:``-joined key a surface stamps at session creation so a later
    message resolves back to the same conversation, and so provenance can say
    WHAT a session is without a stored origin field. Every kind in use is one
    :data:`_KINDS` row, and the per-kind constructors below build from that same
    table — so a tag cannot be stamped without a classifier arm, which is how
    ``app:<app_id>`` came to be written for the whole life of the Apps
    sub-product while the classifier filed every one of those sessions under
    ``user``.

    **Two reads, deliberately, because the live consumers accept different
    shapes.** Origin is DERIVED on every session read and never stored, so any
    change here retroactively reclassifies every existing session; collapsing
    the two into one acceptance rule would silently move sessions between
    origins with no migration to point at.

    * :meth:`origin_of` is LENIENT — head prefix, or the channel infix anywhere
      in the tag, with no arity rule. ``wiki:`` alone still reads as the wiki
      origin.
    * :meth:`parse` is STRICT — head, sub and segment count must all hold, since
      a facet read indexes segments that have to exist. A tag it rejects yields
      no facets, and the caller moves on to the next tag.

    Pure data with no I/O, sibling to :class:`SessionOrigin`: a tag arrives as a
    string argument and the parsed value carries no store, clock or session.
    """

    value: str
    product: str
    session_type: str
    origin: SessionOrigin | None
    ids: Mapping[str, str]

    # Ordered: the channel rows are LAST because their head is a wildcard, so a
    # tag whose own head owns a row (``vcs:``, ``wiki:``) must reach that row
    # first. Among the head rows order is irrelevant — heads are distinct.
    # ``ClassVar`` so the table stays a constant rather than becoming a field on
    # every parsed tag (a bare annotation inside a dataclass IS a field).
    _KINDS: ClassVar[tuple[_TagKind, ...]] = (
        _TagKind(
            product="wiki",
            head="wiki",
            min_parts=3,
            origin=SessionOrigin.WIKI,
            default_type="wiki_qa",
            # ``act`` is the scoped-refresh session stage 2 (jobs.py's
            # ``_start_refresh_act_session``) rewrites the pages a sessionless
            # delta pass flagged stale; ``maintain`` is an on-demand WIKI
            # maintainer session, opened against an already-indexed project so
            # a page can be edited without scheduling a job (a Mewbo App's
            # maintainer session is the separate ``app:`` row below). Neither
            # of these is a QUESTION, so without
            # a row here both fell through to ``wiki_qa`` — a scoped rewrite
            # and a Q&A turn are different workloads with different retry
            # policies (see ``GoalRetryGate.EXCLUDED_SESSION_TYPES`` below).
            type_by_sub={"job": "wiki_index", "act": "wiki_act", "maintain": "wiki_maintain"},
            ids=(("wiki_id", 2),),
        ),
        _TagKind(
            product="search",
            head="agentic_search",
            min_parts=3,
            origin=SessionOrigin.SEARCH,
            # A search RUN must never read as a map. An ``agentic_search:scg:``
            # tag names no sub-kind, so it falls to the map default.
            default_type="scg_map",
            type_by_sub={"run": "search_run"},
            ids=(("search_id", 2),),
        ),
        _TagKind(
            product="structured",
            head="structured",
            origin=SessionOrigin.STRUCTURED,
        ),
        _TagKind(product="draft", head="draft", origin=SessionOrigin.DRAFT),
        _TagKind(product="mobile", head="mobile", origin=SessionOrigin.MOBILE),
        _TagKind(
            product="apps",
            head="app",
            origin=SessionOrigin.APPS,
            # One type for both roles: a builder session is retired once submit
            # mints or reuses the maintainer, so splitting them would add a
            # filter dimension with no query behind it.
            default_type="app_agent",
            ids=(("app_id", 1),),
        ),
        # The SCG map-source (indexing) job — the ``search`` product, but an
        # auditor must tell a map apart from a run.
        _TagKind(
            product="search",
            head="scg",
            subs=("map",),
            min_parts=3,
            default_type="scg_map",
            ids=(("search_id", 2),),
        ),
        # A forge pickup is a CHANNEL session: something outside the console
        # opened a conversation and expects the reply to go back there. It reads
        # `channel` rather than a product of its own because that is what the
        # pickup route itself already records — it writes `origin: "channel"`
        # into its context payload, a key `classify` has never read, so the
        # intent was declared and then silently lost. Without an origin here
        # NOTHING supplies one: the payload carries no `source_platform`, so the
        # context fallback misses too and every pickup badges as a hand-typed
        # `user` session on the landing page.
        _TagKind(
            product="vcs",
            head="vcs",
            origin=SessionOrigin.CHANNEL,
            min_parts=4,
            default_type="vcs_pickup",
            ids=(("repo", 1), ("vcs_kind", 2), ("vcs_number", 3)),
        ),
        _TagKind(
            product="channel",
            subs=("room",),
            origin=SessionOrigin.CHANNEL,
            origin_infix=":room:",
            default_type="channel_msg",
            ids=(("platform", 0), ("channel_id", 2)),
        ),
        _TagKind(
            product="channel",
            subs=("thread",),
            origin=SessionOrigin.CHANNEL,
            origin_infix=":thread:",
            default_type="channel_msg",
            ids=(("platform", 0), ("channel_id", 2), ("thread_id", 3)),
        ),
    )

    # -- reading -----------------------------------------------------------

    @classmethod
    def parse(cls, tag: str) -> SessionTag | None:
        """Parse *tag*, or ``None`` when no kind accepts it.

        ``None`` is the ordinary answer for an operator's manual label, so a
        caller skips it and keeps looking rather than letting a custom label
        mask the real product. Never raises: this runs over every stored tag on
        every session read, and a strict parser would take session listing down.
        """
        parts = tag.split(":")
        for kind in cls._KINDS:
            if kind.accepts(parts):
                facets = kind.facets(parts)
                return cls(
                    value=tag,
                    product=facets.pop("product"),
                    session_type=facets.pop("session_type"),
                    origin=kind.origin,
                    ids=facets,
                )
        return None

    @classmethod
    def origin_of(cls, tag: str) -> SessionOrigin | None:
        """The origin *tag* implies, or ``None`` when it implies none.

        The lenient read (see the class docstring): it needs only the head
        prefix or the channel infix, so a tag too short to yield facets still
        classifies. ``None`` covers both an unrecognised label and a kind that
        deliberately declares no origin.
        """
        for kind in cls._KINDS:
            if kind.implies_origin(tag):
                return kind.origin
        return None

    def facets(self) -> dict[str, str]:
        """The trace facets this tag carries — product, session type, ids."""
        return {"product": self.product, "session_type": self.session_type, **self.ids}

    # -- writing (one constructor per kind) --------------------------------

    @staticmethod
    def wiki_index(job_id: str) -> str:
        """Tag for a wiki INDEXING job's session."""
        return f"wiki:job:{job_id}"

    @staticmethod
    def wiki_qa(answer_id: str) -> str:
        """Tag for a wiki QUESTION's session."""
        return f"wiki:qa:{answer_id}"

    @staticmethod
    def wiki_act(job_id: str) -> str:
        """Tag for the ACT stage of a scoped refresh — stage 2's session."""
        return f"wiki:act:{job_id}"

    @staticmethod
    def wiki_maintain(slug: str) -> str:
        """Tag for a wiki project's on-demand MAINTAINER session.

        Keyed by the project SLUG rather than a job or answer id, and that
        carries two properties the callers depend on.

        It is the IDEMPOTENCY: the tag collection maps one tag to one session,
        so re-opening a maintainer for the same project resolves the session
        that already exists instead of accumulating one per request.

        It is also the AUTHORIZATION, and the address inside it is the point.
        Only the api's maintainer route stamps this, and only after validating
        the slug against an indexed project, so a reader that requires the tag
        can trust the slug it carries — where a slug read from session context
        proves nothing (context is writable by whoever drives the session, and
        re-writable on any later turn). ``mewbo_graph``'s wiki ctx resolver
        reads it back through :meth:`parse` for exactly that reason.

        A slug is ``host[/namespace…]/owner/repo`` and carries no ``:`` —
        ``RepositoryRef`` strips a port and normalises an scp-form remote's
        colon to ``/`` — so it stays the single trailing segment ``parse``
        hands back as ``wiki_id``. Were one ever to contain a colon, ``parse``
        would truncate at it and the shortened slug would match no project, so
        the failure is closed (nothing resolves) rather than open.
        """
        return f"wiki:maintain:{slug}"

    @staticmethod
    def wiki_maintain_fresh(slug: str, session_id: str) -> str:
        """Tag for an ADDITIONAL, non-canonical maintainer session on *slug*.

        Same authorization, different identity. :meth:`wiki_maintain` is keyed by
        the slug ALONE, which is what makes it idempotent — and therefore what
        makes it un-shareable: the tag collection maps one tag to one session, so
        a second session taking it STEALS it, dropping it from the first
        session's ``tags_for_session`` and revoking that session's page-write
        ctx. A caller that wants a genuinely separate conversation against the
        same project must therefore take a tag nobody else can hold, and the
        session's own id is the one value guaranteed unique (the
        :meth:`structured_run` trick, for the same reason).

        The fourth segment is transparent to both reads. ``parse`` indexes
        segments and enforces only a MINIMUM count, so this yields the identical
        ``session_type`` (``wiki_maintain``) and the identical ``wiki_id`` (the
        slug, which carries no ``:``) as the three-segment form — which is what
        lets ``mewbo_graph``'s ctx resolver authorize it with no change. Callers
        that need the canonical, resolvable-by-slug session keep using
        :meth:`wiki_maintain`.
        """
        return f"{SessionTag.wiki_maintain(slug)}:{session_id}"

    @staticmethod
    def search_run(run_id: str) -> str:
        """Tag for an agentic-search RUN's session."""
        return f"agentic_search:run:{run_id}"

    @staticmethod
    def scg_map(job_id: str) -> str:
        """Tag for an SCG map-source (indexing) job's session."""
        return f"scg:map:{job_id}"

    @staticmethod
    def structured_run(session_id: str) -> str:
        """Tag for an agentic structured run.

        The id segment is the session's own id, and it is load-bearing: the tag
        collection is keyed BY TAG, so a constant tag would let each run steal
        every earlier run's tag and reclassify those sessions to the ``user``
        fallback. The parsers read only the second segment, so the id is
        transparent to them.
        """
        return f"structured:run:{session_id}"

    @staticmethod
    def structured_fast(session_id: str) -> str:
        """Tag for the no-loop synthesis lane of a structured request."""
        return f"structured:fast:{session_id}"

    @staticmethod
    def draft_stream(session_id: str) -> str:
        """Tag for a token-streaming draft request."""
        return f"draft:stream:{session_id}"

    @staticmethod
    def mobile(surface: str) -> str:
        """Tag for a session created by a mobile client, keyed by its surface."""
        return f"{MOBILE_TAG_PREFIX}{surface.lower()}"

    @staticmethod
    def app(app_id: str) -> str:
        """Tag for a Mewbo App's builder or maintainer session."""
        return f"{APPS_TAG_PREFIX}{app_id}"

    @staticmethod
    def app_fresh(app_id: str, session_id: str) -> str:
        """Tag for an ADDITIONAL session opened against an app, keyed uniquely.

        The :meth:`app` tag is held by the app's builder-or-maintainer session
        and one tag resolves to one session, so a second session taking it would
        steal it. The session's own id makes this one un-stealable; the parsers
        read only segment 1, so it yields the same ``app_agent`` session type and
        the same ``app_id`` facet as the two-segment form.

        It is also the AUTHORIZATION a fresh session carries: nothing else
        stamps it, and unlike the ``app_id`` CONTEXT key — merged verbatim from a
        request and re-writable on any later turn — a tag cannot be re-pointed,
        so a session opened against one app can never be re-addressed at another.
        """
        return f"{SessionTag.app(app_id)}:{session_id}"

    @staticmethod
    def vcs_pickup(repository: str, kind: str, number: int | str) -> str:
        """Tag for a forge issue/PR pickup, so one item maps to one session."""
        return f"vcs:{repository}:{kind}:{number}"

    @staticmethod
    def channel_room(platform: str, channel_id: str) -> str:
        """Tag for a room-scoped channel conversation."""
        return f"{platform}:room:{channel_id}"

    @staticmethod
    def channel_thread(platform: str, channel_id: str, thread_id: str) -> str:
        """Tag for a thread-scoped channel conversation (beats the room tag)."""
        return f"{platform}:thread:{channel_id}:{thread_id}"


@dataclass(frozen=True)
class TraceProvenance:
    """Filterable Langfuse trace identity derived from a session's durable signals.

    Pure classifier (no I/O), sibling to :class:`SessionOrigin`. A session is
    untraceable to filter today because the observability seam only ever sees
    ``session_id`` + ``source_platform``. Yet a session already *carries* its
    identity in three durable signals the orchestrator can read at run start:

    * its **tags** — product / workspace identity, e.g. ``wiki:job:<id>``,
      ``wiki:qa:<id>``, ``agentic_search:run:<id>``, ``agentic_search:scg:<id>``,
      ``<platform>:room:<chan>`` / ``<platform>:thread:<chan>:<thread>``,
      ``vcs:<owner/repo>:<kind>:<n>``;
    * its merged **context** — ``project`` (``managed:<uuid>`` ⇒ a worktree),
      ``repo``, ``branch``, ``model``, ``structured_workspace``,
      ``client_capabilities``, ``source_platform``;
    * the originating client **surface** — stamped by the entry point (CLI,
      console, api, mcp, channel, github/gitea, home-assistant).

    ``derive`` folds those into the ``tags`` + ``metadata`` that make traces
    filterable by product, workspace, project, repo, branch, worktree, origin,
    and surface. Keeping it a pure transform lets the seam
    (``components.langfuse_session_context``) stay taxonomy-free — it propagates
    whatever it is handed and never learns these prefixes.

    ``tags`` are the low-cardinality ``key:value`` filter chips (the existing tag
    convention used across the codebase); ``metadata`` is the superset, adding
    the high-cardinality fields (ids, worktree, capabilities) for structured
    filtering without exploding the tag list.
    """

    origin: SessionOrigin
    product: str
    session_type: str
    surface: str
    tags: tuple[str, ...]
    metadata: dict[str, str]

    # Facets promoted to filter chips, in a stable order. Everything lands in
    # ``metadata``; only these low-cardinality dimensions also become tags.
    _TAG_FACETS = (
        "origin",
        "product",
        "session_type",
        "surface",
        "transcript_sink",
        "project",
        "repo",
        "branch",
        "workspace",
        "model",
    )

    # Coarse product when no concrete tag refines it. ``vcs`` has no coarse
    # origin (it isn't one of the console's four), so it only ever arrives via a
    # ``vcs:`` tag override below.
    _ORIGIN_PRODUCT = {
        SessionOrigin.USER: "agent",
        SessionOrigin.WIKI: "wiki",
        SessionOrigin.SEARCH: "search",
        SessionOrigin.CHANNEL: "channel",
        SessionOrigin.MOBILE: "mobile",
        SessionOrigin.STRUCTURED: "structured",
        SessionOrigin.DRAFT: "draft",
        SessionOrigin.APPS: "apps",
    }

    @classmethod
    def derive(
        cls,
        *,
        tags: list[str],
        context: dict[str, object],
        surface: str | None = None,
    ) -> TraceProvenance:
        """Fold a session's durable signals into trace tags + metadata."""
        origin = SessionOrigin.classify(tags, context)
        facets: dict[str, str] = {"origin": origin.value}

        # Context first, then tags: the tag-derived product / workspace / ids are
        # the more reliable signal, so they win on any overlapping key.
        facets.update(cls._facets_from_context(context))
        facets.update(cls._facets_from_tags(tags))

        facets.setdefault("product", cls._ORIGIN_PRODUCT.get(origin, "agent"))
        facets.setdefault(
            "session_type",
            "structured" if context.get("structured_workspace") else "chat",
        )
        facets["surface"] = cls._resolve_surface(surface, context, tags)

        tag_list = [f"{key}:{facets[key]}" for key in cls._TAG_FACETS if facets.get(key)]
        return cls(
            origin=origin,
            product=facets["product"],
            session_type=facets["session_type"],
            surface=facets["surface"],
            tags=tuple(tag_list),
            metadata=facets,
        )

    # -- signal extractors (pure, atomic) ----------------------------------

    @staticmethod
    def _facets_from_tags(tags: list[str]) -> dict[str, str]:
        """Map the most-specific session tag to product / type / workspace / ids.

        Returns on the first tag :meth:`SessionTag.parse` recognises; an
        unrecognised label (e.g. a manual ``/tag``) is skipped, so a custom
        label never masks the real product.
        """
        for tag in tags:
            parsed = SessionTag.parse(tag)
            if parsed is not None:
                return parsed.facets()
        return {}

    @classmethod
    def _facets_from_context(cls, context: dict[str, object]) -> dict[str, str]:
        """Pull project / repo / branch / worktree / workspace / model / caps.

        A ``project`` of the form ``managed:<uuid>`` is an ephemeral worktree, not
        a named project — surface it as ``worktree`` (its ``repo`` / ``branch``
        arrive via sibling context) so the high-cardinality uuid never becomes a
        ``project`` filter chip.
        """
        out: dict[str, str] = {}
        project = cls._as_str(context.get("project"))
        if project.startswith("managed:"):
            out["worktree"] = project.split(":", 1)[1]
        elif project:
            out["project"] = project
        for key in ("repo", "branch", "model"):
            value = cls._as_str(context.get(key))
            if value:
                out[key] = value
        # ``transcript_sink`` is the CLI's local-vs-synced facet:
        # ``local-only`` vs ``synced`` — a low-cardinality chip that distinguishes
        # a purely-local CLI session from one mirrored to a remote API, so console/
        # wiki/search never cross-operate on a synced CLI transcript.
        sink = cls._as_str(context.get("transcript_sink"))
        if sink:
            out["transcript_sink"] = sink
        workspace = cls._as_str(context.get("structured_workspace")) or cls._as_str(
            context.get("workspace")
        )
        if workspace:
            out["workspace"] = workspace
        capabilities = context.get("client_capabilities")
        if isinstance(capabilities, list) and capabilities:
            out["capabilities"] = ",".join(str(cap) for cap in capabilities)
        return out

    @classmethod
    def _resolve_surface(
        cls, surface: str | None, context: dict[str, object], tags: list[str]
    ) -> str:
        """Pick the client surface, most-reliable signal first.

        Explicit param (stamped by the entry point) > context ``source_platform``
        (channels) > forge inferred from a ``vcs:`` tag > ``unknown`` — the latter
        keeps an un-stamped path *visible* as a filter rather than silently
        untagged.
        """
        explicit = (surface or "").strip()
        if explicit:
            return explicit
        platform = cls._as_str(context.get("source_platform")).strip()
        if platform:
            return platform
        if any(tag.startswith("vcs:") for tag in tags):
            return "vcs"
        return "unknown"

    @staticmethod
    def _as_str(value: object) -> str:
        """Coerce a context value to a non-empty string, or ``""``."""
        return value if isinstance(value, str) else ""
