> ↑ [packages/mewbo_graph/CLAUDE.md](../../../../CLAUDE.md) · [root](../../../../../../CLAUDE.md)

# scg built-in plugin — SCG map + search tools

`packages/mewbo_graph/src/mewbo_graph/plugins/scg/`. The deterministic SCG logic does **not**
live here — these SessionTools (`scg_introspect_source`, `scg_build_structure`,
`scg_link_entities`, `scg_finalize_map`, `scg_route`, `scg_observe`, `scg_memory`,
`scg_results`, `agentic_search`) are thin wrappers over `mewbo_graph.scg`, imported DOWN. The
substrate rules are `packages/mewbo_graph/CLAUDE.md` → "SCG substrate"; the durable
architecture is `apps/mewbo_api/src/mewbo_api/agentic_search/scg/CLAUDE.md`.

## `agentic_search` — the high-level self-facing search verb

Where `scg_route`/`scg_observe` let a task agent inspect reachability directly,
`agentic_search` runs a whole `scg-search` session and hands back a cited answer. It is
**async-by-handle**: `query` starts a run and returns a `run_id` + `status:"processing"`
IMMEDIATELY, because a search runs for minutes and must never block the caller's loop; re-call
with `run_id` to fetch the cited answer + `computed_at`, and an identical recent query is
idempotently reused. The tool owns NO orchestration — it drives through the down-only
`SearchLauncher` seam and degrades to a structured "unavailable" error when none is wired. Its
id is `search`-classified in core `_infer_operation`, so it is default-allowed in any
`scg`-advertised session.

`scg_results` is **transcript-as-transport**: it VALIDATES the entries (≤50, `extra="forbid"`,
relevance/confidence 0..1) and returns `{ok, count}`, writing nothing. The api projects the
validated `tool_input` from the session transcript onto the run event log, and a child loop
inherits the parent's event_logger, so a probe's emit rides the same transcript stamped with its
`agent_id`. Every search agent emits once: each probe right before its evidence block, the ROOT
before the synthesis for inline-grounded hits only. **Emitting is never terminal** — the probe's
terminal stays the stop-summary evidence block, and the root must NOT re-emit a hit a probe
already emitted, since probe cards are already projected and a root duplicate is degraded.

## The card wire contract — `meta` + `related_questions`

`ScgResultEntry.meta: dict[str, str|int|float|bool]` (≤12 keys, key ≤40 chars, str value ≤200
chars, scalar values only) carries every QUANTITATIVE or ENUMERABLE fact — repo
stars/forks/language, package version/downloads, paper year/citations, issue state/comments —
as data instead of prose flattened into `snippet`, which stays purely descriptive.

**`meta` IS the card's footer and is OPEN-vocab**: the agent proposes whatever facts make a hit
read richer, naming the connector's own fields, and the console's `resultMeta` classifier
renders any key (counts→`46.2k`, byte `size`→`24 KB`, dates→relative, `state`/`status`→a
colour-coded badge). **Do NOT add a second `card_meta` field or per-kind tool args** — the open
dict plus a generic renderer is the whole mechanism.

`related_questions: list[str]` (≤5, each ≤140 chars) is RUN-LEVEL, alongside `results`: the
structured home for follow-ups, so the synthesis answer drops conversational "If you want, I
can…" offers. Both validate-and-REJECT rather than truncating, since the tool is validate-only
and the model retries on a `ValidationError`. `extra="forbid"` means an undeclared field carries
nothing — declare it on the schema or it does not exist.

## `err_result` records a FAILED step

An scg tool that catches an internal exception and RETURNS `err_result(code, message)` — the
`str({"error": {"code", "message"}})` envelope shared with the wiki `_err_result` — must reach
the loop's failure machinery. Core's `tool_use_loop` detects that envelope shape on any
session-tool return (`_session_tool_error_envelope`, a pure structural check — core never
imports the graph layer) and emits `tool_result` with `success: False`, while the model still
receives the envelope JSON as the tool output. Without it an embedding-429 renders as "✓ ok" and
the per-step failure nudge never fires. Keep returning `err_result` for handled failures.

## The tool ↔ substrate import boundary (`_core.py`)

`_core.ScgCore` is the one atomic resolver every tool crosses to reach `mewbo_graph.scg` and
`mewbo_graph.wiki`. Each accessor **late-imports inside the call**, so a core-only install — the
optional `mewbo-graph` library present but its `treesitter`/`retrieval` extras (or the api run
store) absent — never fails at plugin load: the tool catches `ImportError` and degrades to a
structured error. Tests monkeypatch `ScgCore` classmethods to inject fakes. Don't re-spread late
imports across the tools; add the seam to `ScgCore`.

## Routing invariants (each shipped a live no-answer bug)

- **`ScgRouter.route` ranks ONLY persisted recipes.** Neither default provider emits them, so
  `ScgParser.parse_source` backfills a single-step recipe per recipe-less capability — zero
  recipes means a mapped source that routes nothing.
- **The probe's tool scope is copied, never inferred.** `scg_route` enriches each recipe with
  `source_ids` + `source_capabilities` (every capability of the pathway's sources) and the
  scg-search playbook copies that into the spawn's `allowed_tools`. Left to inference, the parent
  scopes probes to the step tools only, and a one-tool probe cannot chain a follow-up lookup.
- **A pathway is the probe's ENTRY, not its ceiling** (scg-path-probe.md): the probe chases the
  sub-query to ground with any granted tool of the same source and declares NO DATA only when the
  SOURCE cannot supply it.
- **`scg_route` projects the memory bias, it does not compute it.** It calls `route_with_memory`
  and projects capped `memory_hints` per recipe — compact, so the probe needs no second
  `scg_memory` read. A failed bridge yields an empty bias, never a route failure. `scg_memory`
  write takes a `polarity` (positive/dead_end) arg.
- **`scg_observe` is the Search-on-Graph read (arXiv 2510.08825).** The ENGINE ranks entries
  (`scg_route`), the AGENT reads the hops and navigates — the typed edges
  (SUPPORTS_QUERY/PRODUCES/CONSUMES/RESOLVES_TO) carry the routing meaning, not a second engine
  score. Given node refs it projects each node's directed, typed neighborhood (edges + 1-hop
  neighbor cards + recipes + anchored memory notes) — a thin read over the store, NO new
  traversal engine. Two-stage: a node over `_SURVEY_THRESHOLD` in-scope edges with no filter
  returns a `kinds_only` rollup, then the agent re-calls with `edge_kinds`/`direction` for the
  instances. Read-only, `ScgScope`-filtered, `auth_scope` redacted.

## Capability gating (data-driven, advertisement-only)

The plugin manifest declares `requires-capabilities: ["scg"]` and the AgentDefs repeat it in
frontmatter. A session reaches these tools when it advertises `client_capabilities: ["scg"]` —
the generalized form of the wiki gate, with no `scg` string hardcoded in `agent_registry.py` or
`capabilities.py`. The deterministic core is also opt-in behind `scg.enabled`. Every genuine
surface advertises `scg` explicitly: the search/structured run, the map job, and — via the
request-scoped derivation — any session naming an `scg_*`/`agentic_search` id in `allowed_tools`.

**`scg` is advertisement-only. This suite registers NOTHING into core's
`register_session_capability_provider` seam, and must not.** A runtime provider sees only the
ADVERTISED cap tuple — no context, no tags — so it cannot tell a search surface from a bare
`POST /api/sessions` coding session that requested no integrations. Granting `scg` from
`scg.enabled` + "≥1 mapped source" therefore fires for ordinary CLI/console/channel sessions,
binding all 12 tool schemas on EVERY LLM call (~half the catalog, pure token waste under zero
prompt caching) and leaking the augmented cap set into the trace context so the session
mis-tags `origin:search`/`product:search`. To get `scg` on an ordinary session, advertise it
(`X-Mewbo-Capabilities: scg`) or name the tool in the request `allowed_tools`.

**The two-surface gate is orthogonal to WHERE `scg` comes from — keep those tests green.**
`SessionToolRegistry.build_for` selects session tools by `allowed_tools` AND by manifest
capability, building any factory whose `requires-capabilities` ⊆ `session_caps`. By
`allowed_tools` alone, these tools surface to a ROOT agent only when its AgentDef names them, so
a re-engaged search session where the root deposits directly sees no `scg_*` and answers
`TOOLS-MISSING`.

An unscoped scg session binds no `ScgScope`, so `scg_observe`/`scg_route` read the WHOLE graph
and `scg_memory write` attributes to `session:<id>` (a `labels` fallback for `ws:<id>`, no new
field). The three reasoning tools are default-allowed: their ids are `get`-classified in core
`_infer_operation`, so the default permission policy allows the reads plus the additive deposit
with no new config knob.
