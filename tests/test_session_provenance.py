"""Tests for session provenance classification."""

import pytest
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.session.session_provenance import (
    CapabilityEvidence,
    SessionOrigin,
    SessionTag,
    TraceProvenance,
    is_mobile_surface,
)
from mewbo_core.session.session_store import SessionStore


@pytest.mark.parametrize(
    ("tags", "context", "expected"),
    [
        # Tag prefix is the primary signal (covers old wiki jobs w/ empty context).
        (["wiki:job:abc"], {}, SessionOrigin.WIKI),
        (["wiki:qa:abc"], {}, SessionOrigin.WIKI),
        (["agentic_search:scg:abc"], {}, SessionOrigin.SEARCH),
        (["agentic_search:run:abc"], {}, SessionOrigin.SEARCH),
        (["nextcloud-talk:room:tok"], {}, SessionOrigin.CHANNEL),
        (["email:thread:chan:root"], {}, SessionOrigin.CHANNEL),
        # Realtime structured/draft surfaces.
        (["structured:run"], {}, SessionOrigin.STRUCTURED),
        (["structured:fast"], {}, SessionOrigin.STRUCTURED),
        (["draft:stream"], {}, SessionOrigin.DRAFT),
        # Mobile-created sessions (Aura et al.) — tag wins, context is fallback.
        (["mobile:android"], {}, SessionOrigin.MOBILE),
        ([], {"client": "aura-android"}, SessionOrigin.MOBILE),
        ([], {"source_platform": "android"}, SessionOrigin.MOBILE),
        # Mewbo Apps builder/maintainer sessions (tag wins, context is fallback).
        (["app:abc123"], {}, SessionOrigin.APPS),
        # Context fallback when no tag is present — only for the two
        # capabilities NO client advertises about itself.
        ([], {"client_capabilities": ["wiki"]}, SessionOrigin.WIKI),
        ([], {"client_capabilities": ["scg"]}, SessionOrigin.SEARCH),
        # An `apps` ADVERTISEMENT does not imply the APPS origin: the console
        # sends it on every request, so treating it as evidence would file
        # every ordinary chat under `apps` and hide those rows behind the
        # landing page's origin filter. A real Apps session is tagged
        # `app:<id>` (asserted above), which wins earlier anyway.
        ([], {"client_capabilities": ["apps"]}, SessionOrigin.USER),
        ([], {"source_platform": "nextcloud-talk"}, SessionOrigin.CHANNEL),
        # The console's exact fixed header, which must classify as a plain user
        # session.
        (
            [],
            {"client_capabilities": ["stlite", "apps", "ask_user", "generative_ui"]},
            SessionOrigin.USER,
        ),
        # Manual console sessions and the empty default.
        ([], {"client_capabilities": ["stlite"]}, SessionOrigin.USER),
        ([], {}, SessionOrigin.USER),
        # Tag wins over a conflicting context capability.
        (["wiki:job:abc"], {"client_capabilities": ["stlite"]}, SessionOrigin.WIKI),
    ],
)
def test_classify(tags, context, expected):
    """classify maps tags + context to the right coarse origin."""
    assert SessionOrigin.classify(tags, context) == expected


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        (None, False),
        ("android", True),
        ("ios", True),
        ("aura-android", True),
        ("AURA-Foo", True),
        ("slack", False),
        (123, False),
    ],
)
def test_is_mobile_surface(surface, expected):
    """is_mobile_surface matches known platforms + any Aura-prefixed surface."""
    assert is_mobile_surface(surface) is expected


def test_tags_for_session_round_trip(tmp_path):
    """tags_for_session is the reverse of resolve_tag and returns every match."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.tag_session(session_id, "wiki:job:1")
    store.tag_session(session_id, "extra-label")
    store.tag_session(store.create_session(), "other:room:x")
    assert sorted(store.tags_for_session(session_id)) == ["extra-label", "wiki:job:1"]


def test_summarize_session_sets_origin(tmp_path):
    """summarize_session classifies a tagged wiki session and a plain one."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    wiki_id = runtime.resolve_session(session_tag="wiki:job:42")
    store.append_event(wiki_id, {"type": "user", "payload": {"text": "index"}})
    assert runtime.summarize_session(wiki_id)["origin"] == "wiki"

    user_id = runtime.resolve_session()
    store.append_event(user_id, {"type": "user", "payload": {"text": "hi"}})
    assert runtime.summarize_session(user_id)["origin"] == "user"


def test_summarize_session_surfaces_capabilities_and_workspace(tmp_path):
    """The summary lifts caps + workspace to top-level (transparency).

    The landing page shows WHAT a session was scoped to without parsing the
    context shape. `scg` is server-written and passes through; a plain session
    reports an empty list / no workspace.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    scoped = runtime.resolve_session()
    store.append_event(
        scoped,
        {
            "type": "context",
            "payload": {
                "client_capabilities": ["scg"],
                "structured_workspace": "ws-7870f5ab",
            },
        },
    )
    store.append_event(scoped, {"type": "user", "payload": {"text": "curate"}})
    summary = runtime.summarize_session(scoped)
    assert summary["capabilities"] == ["scg"]
    assert summary["workspace"] == "ws-7870f5ab"

    plain = runtime.resolve_session()
    store.append_event(plain, {"type": "user", "payload": {"text": "hi"}})
    plain_summary = runtime.summarize_session(plain)
    assert plain_summary["capabilities"] == []
    assert plain_summary["workspace"] is None


# -- CapabilityEvidence -----------------------------------------------------

# The exact header the console sends on EVERY request
# (`apps/mewbo_console/src/api/realClient.ts`). It is an advertisement of what
# the surface can render, not a statement about the session.
_CONSOLE_HEADER = ["stlite", "apps", "ask_user", "generative_ui"]


def _console_session(runtime, store, *, text="hello"):
    session_id = runtime.resolve_session()
    store.append_event(
        session_id,
        {"type": "context", "payload": {"client_capabilities": list(_CONSOLE_HEADER)}},
    )
    store.append_event(session_id, {"type": "user", "payload": {"text": text}})
    return session_id


def test_console_advertisement_alone_earns_nothing(tmp_path):
    """A fixed header is not evidence of anything.

    An ordinary console chat advertises all four render capabilities and uses
    none of them. It must classify as a plain `user` session (so the landing
    page's default origin filter still shows it) and report NO capabilities (so
    it is not chipped APPS / STLITE / ASK_USER / GENERATIVE_UI).
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = _console_session(runtime, store)

    summary = runtime.summarize_session(session_id)
    assert summary["origin"] == "user"
    assert summary["capabilities"] == []


def test_app_tag_still_classifies_as_apps(tmp_path):
    """A real Apps session is TAGGED, and the tag arm is untouched."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session(session_tag=SessionTag.app("app-abc123"))
    store.append_event(
        session_id,
        {"type": "context", "payload": {"client_capabilities": list(_CONSOLE_HEADER)}},
    )
    store.append_event(session_id, {"type": "user", "payload": {"text": "build"}})
    assert runtime.summarize_session(session_id)["origin"] == "apps"


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        # A dedicated artifact event — emitted only once the artifact landed.
        ({"type": "widget_ready", "payload": {"widget_id": "w1"}}, "stlite"),
        ({"type": "generative_ui", "payload": {"ui_id": "gui-0badc0de"}}, "generative_ui"),
        ({"type": "user_question", "payload": {"call_id": "c1"}}, "ask_user"),
        ({"type": "app_updated", "payload": {"app_id": "a1"}}, "apps"),
        # A successful invocation of a tool the capability gates.
        (
            {
                "type": "tool_result",
                "payload": {"tool_id": "present_ui", "success": True, "result": "ok"},
            },
            "generative_ui",
        ),
        (
            {
                "type": "tool_result",
                "payload": {"tool_id": "run_pipeline", "success": True, "result": "ok"},
            },
            "apps",
        ),
    ],
)
def test_invocation_earns_the_capability(tmp_path, event, expected):
    """One durable signal is enough, and it earns ONLY its own capability."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = _console_session(runtime, store)
    store.append_event(session_id, event)

    assert runtime.summarize_session(session_id)["capabilities"] == [expected]


def test_a_failed_tool_call_is_not_evidence(tmp_path):
    """These tools are terminal-free, so a rejected call still leaves a result."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = _console_session(runtime, store)
    store.append_event(
        session_id,
        {
            "type": "tool_result",
            "payload": {
                "tool_id": "submit_widget",
                "success": False,
                "error": "lint failed",
            },
        },
    )
    assert runtime.summarize_session(session_id)["capabilities"] == []


def test_earned_capabilities_report_in_advertised_order(tmp_path):
    """Two proofs report as two chips, in the order the client sent them."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = _console_session(runtime, store)
    store.append_event(session_id, {"type": "generative_ui", "payload": {}})
    store.append_event(session_id, {"type": "widget_ready", "payload": {}})

    caps = runtime.summarize_session(session_id)["capabilities"]
    assert caps == ["stlite", "generative_ui"]


def test_server_written_scopes_pass_through_unearned(tmp_path):
    """`wiki`/`scg` are written by a headless job, never advertised by a client.

    Nothing in a transcript can prove them the way an artifact event proves a
    render capability, and blanking them would drop a chip the console (and its
    docs) rely on — so they are reported as written.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session(session_tag=SessionTag.wiki_index("job-1"))
    store.append_event(
        session_id,
        {"type": "context", "payload": {"client_capabilities": ["wiki", "apps"]}},
    )
    store.append_event(session_id, {"type": "user", "payload": {"text": "index"}})

    summary = runtime.summarize_session(session_id)
    assert summary["capabilities"] == ["wiki"]
    assert summary["origin"] == "wiki"


def test_capability_evidence_reports_an_unadvertised_grant():
    """An invocation counts even when the client never advertised it."""
    evidence = CapabilityEvidence()
    evidence.observe({"type": "widget_ready", "payload": {}})
    assert evidence.resolve(None) == ["stlite"]
    assert evidence.resolve(["wiki"]) == ["wiki", "stlite"]


def test_capability_evidence_dedupes_and_drops_blanks():
    """A repeated or blank advertised entry never doubles a chip."""
    evidence = CapabilityEvidence()
    assert evidence.resolve(["wiki", "wiki", "  ", "scg"]) == ["wiki", "scg"]


# -- TraceProvenance.derive -------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "context", "surface", "product", "session_type", "expect_meta"),
    [
        # Wiki indexing vs Q&A — product/type/id from the tag.
        (["wiki:job:abc"], {}, "api", "wiki", "wiki_index", {"wiki_id": "abc"}),
        (["wiki:qa:xyz"], {}, "console", "wiki", "wiki_qa", {"wiki_id": "xyz"}),
        # Agentic search run vs SCG map: the three search-product session
        # types stay distinct — a RUN (agentic_search:run), the scg-namespaced tag,
        # and the MAP-source job (scg:map). A run must NOT read as a map.
        (["agentic_search:run:r1"], {}, "api", "search", "search_run", {"search_id": "r1"}),
        (["agentic_search:scg:s1"], {}, "api", "search", "scg_map", {"search_id": "s1"}),
        (["scg:map:j1"], {"client_capabilities": ["scg"]}, "api", "search", "scg_map",
         {"search_id": "j1"}),
        # Structured query, distinguished from chat by ``structured_workspace``;
        # surface separates an MCP-invoked call from a console one.
        ([], {"structured_workspace": "ws"}, "mcp", "agent", "structured", {"workspace": "ws"}),
        ([], {}, "cli", "agent", "chat", {}),
        # Realtime structured/draft tags drive product + session_type ; the
        # tag's session_type wins over the context-derived "structured".
        (["structured:run"], {}, "api", "structured", "structured_run", {}),
        (["structured:fast"], {"structured_workspace": "ws"}, "api", "structured",
         "structured_fast", {"workspace": "ws"}),
        (["draft:stream"], {}, "console", "draft", "draft_stream", {}),
        # Mobile-tagged session (#, aura): tag drives product + sub-kind session_type.
        (["mobile:android"], {}, "android", "mobile", "mobile_android", {}),
        # App builder/maintainer session: tag drives product + type + app_id.
        (["app:abc123"], {}, "api", "apps", "app_agent", {"app_id": "abc123"}),
        # Untagged mobile context (existing Aura sessions) still resolves to the
        # mobile product via the ``_ORIGIN_PRODUCT`` fallback (no session-type refinement).
        ([], {"client": "aura-android"}, "aura-android", "mobile", "chat", {}),
    ],
)
def test_derive_product_and_type(tags, context, surface, product, session_type, expect_meta):
    """derive maps the most-specific tag/context signal to product + type + ids."""
    prov = TraceProvenance.derive(tags=tags, context=context, surface=surface)
    assert prov.product == product
    assert prov.session_type == session_type
    assert prov.surface == surface
    for key, value in expect_meta.items():
        assert prov.metadata[key] == value
    # The coarse filter chips are always present and mirror metadata.
    assert f"product:{product}" in prov.tags
    assert f"session_type:{session_type}" in prov.tags
    assert f"surface:{surface}" in prov.tags
    assert f"origin:{prov.origin.value}" in prov.tags


def test_derive_mobile_origin_and_product():
    """A mobile tag resolves MOBILE origin end-to-end (classify + product + facet)."""
    prov = TraceProvenance.derive(tags=["mobile:android"], context={}, surface="android")
    assert prov.origin == SessionOrigin.MOBILE
    assert prov.product == "mobile"
    assert "origin:mobile" in prov.tags


def test_derive_apps_origin_and_product():
    """An app: tag resolves APPS origin end-to-end (classify + product + facet)."""
    prov = TraceProvenance.derive(tags=["app:abc123"], context={}, surface="api")
    assert prov.origin == SessionOrigin.APPS
    assert prov.product == "apps"
    assert prov.metadata["app_id"] == "abc123"
    assert "origin:apps" in prov.tags


def test_derive_channel_unpacks_platform_and_ids():
    """A channel thread tag yields platform + channel + thread, surface = platform."""
    prov = TraceProvenance.derive(
        tags=["nextcloud-talk:thread:room123:thr456"],
        context={"source_platform": "nextcloud-talk"},
        surface="nextcloud-talk",
    )
    assert prov.product == "channel"
    assert prov.session_type == "channel_msg"
    assert prov.surface == "nextcloud-talk"
    assert prov.metadata["platform"] == "nextcloud-talk"
    assert prov.metadata["channel_id"] == "room123"
    assert prov.metadata["thread_id"] == "thr456"
    # High-cardinality ids stay out of the tag chips.
    assert not any(tag.startswith("channel_id:") for tag in prov.tags)


def test_derive_vcs_pickup_and_managed_worktree():
    """A vcs tag drives product; a managed project becomes a worktree, not a chip."""
    prov = TraceProvenance.derive(
        tags=["vcs:bearlike/Assistant:pull_request:72"],
        context={"project": "managed:deadbeef", "branch": "grove/x", "repo": "Assistant"},
        surface=None,
    )
    assert prov.product == "vcs"
    assert prov.session_type == "vcs_pickup"
    # Surface inferred from the vcs tag when nothing stamped it.
    assert prov.surface == "vcs"
    assert prov.metadata["vcs_kind"] == "pull_request"
    assert prov.metadata["vcs_number"] == "72"
    # The tag's owner/repo wins over the context's bare repo.
    assert prov.metadata["repo"] == "bearlike/Assistant"
    assert "repo:bearlike/Assistant" in prov.tags
    assert "branch:grove/x" in prov.tags
    # ``managed:<uuid>`` is a worktree, never a project chip.
    assert prov.metadata["worktree"] == "deadbeef"
    assert not any(tag.startswith("project:") for tag in prov.tags)


def test_derive_transcript_sink_facet():
    """A ``transcript_sink`` context key becomes a low-cardinality filter chip."""
    prov = TraceProvenance.derive(
        tags=[],
        context={"transcript_sink": "synced"},
        surface="cli",
    )
    # surface stays 'cli'; the sink is a SEPARATE local-vs-synced facet.
    assert prov.surface == "cli"
    assert prov.metadata["transcript_sink"] == "synced"
    assert "transcript_sink:synced" in prov.tags
    assert "surface:cli" in prov.tags


def test_derive_without_transcript_sink_omits_facet():
    """A purely-local CLI session carries no sink facet (absence == local-only)."""
    prov = TraceProvenance.derive(tags=[], context={}, surface="cli")
    assert "transcript_sink" not in prov.metadata
    assert not any(tag.startswith("transcript_sink:") for tag in prov.tags)


def test_derive_named_project_and_capabilities():
    """A named project is a chip; capabilities ride in metadata only."""
    prov = TraceProvenance.derive(
        tags=[],
        context={"project": "atlas", "client_capabilities": ["wiki", "scg"], "model": "m"},
        surface="api",
    )
    assert prov.metadata["project"] == "atlas"
    assert "project:atlas" in prov.tags
    assert "model:m" in prov.tags
    assert prov.metadata["capabilities"] == "wiki,scg"
    assert not any(tag.startswith("capabilities:") for tag in prov.tags)
    assert "worktree" not in prov.metadata


@pytest.mark.parametrize(
    ("surface", "context", "tags", "expected"),
    [
        # Explicit stamp wins over a context platform.
        ("cli", {"source_platform": "email"}, [], "cli"),
        # Context platform fills in when nothing was stamped.
        (None, {"source_platform": "email"}, [], "email"),
        # A vcs tag infers the forge when no other surface signal exists.
        (None, {}, ["vcs:o/r:issue:1"], "vcs"),
        # Truly unknown stays visible as a filter, not silently dropped.
        (None, {}, [], "unknown"),
    ],
)
def test_derive_surface_precedence(surface, context, tags, expected):
    """Surface resolves explicit > context platform > vcs-forge > unknown."""
    prov = TraceProvenance.derive(tags=tags, context=context, surface=surface)
    assert prov.surface == expected


def test_derive_custom_label_does_not_mask_product():
    """An unrecognised manual label is skipped; the real product tag still wins."""
    prov = TraceProvenance.derive(tags=["my-label", "wiki:qa:xyz"], context={}, surface="console")
    assert prov.product == "wiki"
    assert prov.session_type == "wiki_qa"


# -- SessionTag grammar -----------------------------------------------------
#
# Origin is derived on every session read and never stored, so a change to this
# grammar retroactively reclassifies every session already in the store. These
# tests freeze the CURRENT behaviour over the shapes real producers stamp, plus
# the degenerate ones a client-supplied ``session_tag`` can introduce.


@pytest.mark.parametrize(
    ("tag", "session_type", "ids", "origin"),
    [
        (SessionTag.wiki_index("j-ab"), "wiki_index", {"wiki_id": "j-ab"}, SessionOrigin.WIKI),
        (SessionTag.wiki_qa("ans-1"), "wiki_qa", {"wiki_id": "ans-1"}, SessionOrigin.WIKI),
        (
            SessionTag.search_run("run-7"),
            "search_run",
            {"search_id": "run-7"},
            SessionOrigin.SEARCH,
        ),
        # The map job alone implies NO origin, and that is load-bearing rather
        # than an omission: its job writes ``client_capabilities: ["scg"]``, which
        # the capability branch already reads as SEARCH. A tag origin here would
        # be a second source for a value one signal already supplies.
        (SessionTag.scg_map("job-9"), "scg_map", {"search_id": "job-9"}, None),
        (SessionTag.structured_run("sess-1"), "structured_run", {}, SessionOrigin.STRUCTURED),
        (SessionTag.structured_fast("sess-2"), "structured_fast", {}, SessionOrigin.STRUCTURED),
        (SessionTag.draft_stream("sess-3"), "draft_stream", {}, SessionOrigin.DRAFT),
        (SessionTag.mobile("android"), "mobile_android", {}, SessionOrigin.MOBILE),
        (SessionTag.app("abc123"), "app_agent", {"app_id": "abc123"}, SessionOrigin.APPS),
        # A forge pickup IS a channel session — something outside the console
        # opened the conversation. Nothing else supplies that: the pickup route's
        # context carries no ``source_platform``, and the ``origin: "channel"`` it
        # does write is a key ``classify`` never reads.
        (
            SessionTag.vcs_pickup("acme/widget", "issue", 7),
            "vcs_pickup",
            {"repo": "acme/widget", "vcs_kind": "issue", "vcs_number": "7"},
            SessionOrigin.CHANNEL,
        ),
        (
            SessionTag.channel_room("nextcloud-talk", "tok"),
            "channel_msg",
            {"platform": "nextcloud-talk", "channel_id": "tok"},
            SessionOrigin.CHANNEL,
        ),
        (
            SessionTag.channel_thread("email", "chan", "root"),
            "channel_msg",
            {"platform": "email", "channel_id": "chan", "thread_id": "root"},
            SessionOrigin.CHANNEL,
        ),
    ],
)
def test_session_tag_round_trip(tag, session_type, ids, origin):
    """Every kind a producer stamps parses back to its own product/type/ids."""
    parsed = SessionTag.parse(tag)
    assert parsed is not None, tag
    assert parsed.value == tag
    assert parsed.session_type == session_type
    assert dict(parsed.ids) == ids
    assert parsed.origin is origin
    # The parsed facets are exactly what the trace consumer reads back.
    assert parsed.facets() == {
        "product": parsed.product,
        "session_type": session_type,
        **ids,
    }


@pytest.mark.parametrize(
    ("built", "wire"),
    [
        (SessionTag.wiki_index("j1"), "wiki:job:j1"),
        (SessionTag.wiki_qa("a1"), "wiki:qa:a1"),
        (SessionTag.search_run("r1"), "agentic_search:run:r1"),
        (SessionTag.scg_map("j2"), "scg:map:j2"),
        (SessionTag.structured_run("s1"), "structured:run:s1"),
        (SessionTag.structured_fast("s2"), "structured:fast:s2"),
        (SessionTag.draft_stream("s3"), "draft:stream:s3"),
        (SessionTag.mobile("Android"), "mobile:android"),
        (SessionTag.app("app-1"), "app:app-1"),
        (SessionTag.vcs_pickup("acme/w", "pull_request", 12), "vcs:acme/w:pull_request:12"),
        (SessionTag.channel_room("email", "c1"), "email:room:c1"),
        (SessionTag.channel_thread("email", "c1", "t1"), "email:thread:c1:t1"),
    ],
)
def test_session_tag_wire_values_are_frozen(built, wire):
    """The stored wire value of every kind is unchanged.

    Tags are persisted and resolved by exact string, so a constructor that
    reshapes its key orphans every session already tagged with the old one.
    """
    assert built == wire


# (tag, origin classify reports for it alone, facets it yields)
_TAG_CORPUS = [
    # -- shapes real producers stamp ----------------------------------------
    ("wiki:job:job-abc", SessionOrigin.WIKI,
     {"product": "wiki", "session_type": "wiki_index", "wiki_id": "job-abc"}),
    ("wiki:qa:ans-123", SessionOrigin.WIKI,
     {"product": "wiki", "session_type": "wiki_qa", "wiki_id": "ans-123"}),
    ("agentic_search:run:run-7", SessionOrigin.SEARCH,
     {"product": "search", "session_type": "search_run", "search_id": "run-7"}),
    # A map's key written under the run namespace: stored sessions carry
    # this spelling, so it resolves to the map, not to a run.
    ("agentic_search:scg:s1", SessionOrigin.SEARCH,
     {"product": "search", "session_type": "scg_map", "search_id": "s1"}),
    ("scg:map:job-9", SessionOrigin.USER,
     {"product": "search", "session_type": "scg_map", "search_id": "job-9"}),
    ("structured:run:sess-1", SessionOrigin.STRUCTURED,
     {"product": "structured", "session_type": "structured_run"}),
    ("structured:fast:sess-2", SessionOrigin.STRUCTURED,
     {"product": "structured", "session_type": "structured_fast"}),
    ("draft:stream:sess-3", SessionOrigin.DRAFT,
     {"product": "draft", "session_type": "draft_stream"}),
    ("mobile:android", SessionOrigin.MOBILE,
     {"product": "mobile", "session_type": "mobile_android"}),
    ("mobile:aura-android", SessionOrigin.MOBILE,
     {"product": "mobile", "session_type": "mobile_aura-android"}),
    ("app:abc123", SessionOrigin.APPS,
     {"product": "apps", "session_type": "app_agent", "app_id": "abc123"}),
    ("vcs:acme/widget:issue:7", SessionOrigin.CHANNEL,
     {"product": "vcs", "session_type": "vcs_pickup", "repo": "acme/widget",
      "vcs_kind": "issue", "vcs_number": "7"}),
    ("nextcloud-talk:room:tok", SessionOrigin.CHANNEL,
     {"product": "channel", "session_type": "channel_msg", "platform": "nextcloud-talk",
      "channel_id": "tok"}),
    ("email:thread:chan:root", SessionOrigin.CHANNEL,
     {"product": "channel", "session_type": "channel_msg", "platform": "email",
      "channel_id": "chan", "thread_id": "root"}),
    # -- two-segment structured/draft forms, stamped before the id suffix ----
    ("structured:run", SessionOrigin.STRUCTURED,
     {"product": "structured", "session_type": "structured_run"}),
    ("draft:stream", SessionOrigin.DRAFT,
     {"product": "draft", "session_type": "draft_stream"}),
    # -- operator labels and empties: no origin, no facets, never a raise ----
    ("my-label", SessionOrigin.USER, {}),
    ("", SessionOrigin.USER, {}),
    ("wiki", SessionOrigin.USER, {}),
    # -- arity too short for facets, but the prefix still carries the origin -
    # The origin read is lenient and the facet read is strict; they are two
    # projections of one table precisely because they disagree here.
    ("wiki:", SessionOrigin.WIKI, {}),
    ("agentic_search:run", SessionOrigin.SEARCH, {}),
    # ``scg:`` stays USER here because it declares no tag origin at all — its
    # SEARCH reading comes from the capability context, which this corpus does
    # not supply. ``vcs:`` does declare one, so the lenient origin read finds it
    # even at an arity too short for facets.
    ("scg:map", SessionOrigin.USER, {}),
    ("vcs:o/r", SessionOrigin.CHANNEL, {}),
    ("app:", SessionOrigin.APPS,
     {"product": "apps", "session_type": "app_agent", "app_id": ""}),
    # -- the channel infix is matched anywhere in the tag for ORIGIN, but the
    # facet read requires it in the second segment.
    ("foo:bar:room:baz", SessionOrigin.CHANNEL, {}),
    ("wiki:room", SessionOrigin.WIKI,
     {"product": "channel", "session_type": "channel_msg", "platform": "wiki"}),
    # A room tag never grows a thread_id, however many segments follow.
    ("nextcloud-talk:room:a:b", SessionOrigin.CHANNEL,
     {"product": "channel", "session_type": "channel_msg", "platform": "nextcloud-talk",
      "channel_id": "a"}),
    ("p:thread:c", SessionOrigin.CHANNEL,
     {"product": "channel", "session_type": "channel_msg", "platform": "p",
      "channel_id": "c"}),
]


@pytest.mark.parametrize(("tag", "origin", "facets"), _TAG_CORPUS)
def test_tag_corpus_classification_is_frozen(tag, origin, facets):
    """Every stored tag shape keeps classifying exactly as it does today."""
    assert SessionOrigin.classify([tag], {}) is origin
    assert TraceProvenance._facets_from_tags([tag]) == facets


def test_unrecognised_tag_never_masks_a_later_one():
    """An unparsed label is skipped by BOTH reads, not treated as a product."""
    tags = ["my-label", "", "wiki:qa:xyz"]
    assert SessionOrigin.classify(tags, {}) is SessionOrigin.WIKI
    assert TraceProvenance._facets_from_tags(tags)["session_type"] == "wiki_qa"


def test_tag_beats_context_and_first_match_wins():
    """Tags win over context, and the FIRST recognised tag decides."""
    assert (
        SessionOrigin.classify(["app:a1", "wiki:job:j1"], {"client_capabilities": ["wiki"]})
        is SessionOrigin.APPS
    )
    assert TraceProvenance._facets_from_tags(["app:a1", "wiki:job:j1"])["product"] == "apps"
