> ↑ [agentic_search/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_api/CLAUDE.md](../../../../CLAUDE.md) · [root](../../../../../../CLAUDE.md)

# Source Capability Graph (SCG) — API Subsystem Guidance

Scope: `apps/mewbo_api/src/mewbo_api/agentic_search/scg/` — the run/map-job **lifecycle glue**.
The deterministic engine (`types`, `store`, `providers`, `parser`, `router`, `entity_resolution`,
`memory_bridge`) lives down in `mewbo_graph.scg`, the SessionTools in `mewbo_graph.plugins.scg`.

## Architecture

The SCG is a **cheap routing controller, not a proof-search engine.** It indexes *reachability*
(schemas + qualified pathways), **never the data behind them.**

**No parallel control loop.** The one engine is the `ToolUseLoop` the `scg-search` AgentDef runs;
the deterministic graph ops (router, parser, entity resolver) are **tools** that agent drives.
`OrchestratedSearchRunner` adds no loop — it projects a finished session transcript onto the run
event log. There are no verification rounds and none are wanted: **the connector's real return is
the only verifier.** Do not add an A\* `f=g+h` frontier, majority-vote self-consistency, a
process-reward heuristic, MCTS, a trained PRM, or RL search.

**Tiers (fast / auto / deep) are ONE budget knob** — decomposition depth, probe fan-out, **and the
model**: `ScgConfig.model_for_tier(run.tier)` reads `scg.traversal.tier_models` (all three tiers
default to `openai/gpt-oss-120b`) into the drive's `model_name`, and probes inherit the session
model, so the one knob moves the whole run. Blank/unknown → `llm.default_model`; an explicit
request `model` wins over the tier map at the drive.

**The search-run terminal is NL** (`AnswerSynthesis`). The structured graph-first terminal reuses
`EmitStructuredResponseTool` semantics including `should_terminate_run()` — a terminal emit tool
ends the loop itself.

## Search-graph seams

- **`WorkspaceGraphBinding`** (`workspace_binding.py`) is THE one seam any workspace-bound run
  crosses → {capability + quarantined-instruction context events, connector grant ∪
  `TRAVERSAL_TOOLS` (incl. `scg_observe`), an `ScgScope.use(sources, workspace=id)` context
  manager}. Both the search runner and the structured graph-first path consume it; never
  re-assemble inline.
- **Live streaming** (`run_streamer.py:RunEventStreamer`): subscribe the backing session's core
  `SessionEventBus` BEFORE the drive; a daemon consumer projects `sub_agent`→`agent_*` as
  published. `_settle` is RECONCILE-only (`reconcile_missing`, no double-emit). `ProbeTrace` and
  `CoordinatorTrace` are each ONE projection shared by the live and settle paths, so the two
  render byte-identically.
- **Graph-first structured** (`graph_structured_runner.py`): `/v1/structured` with a mapped search
  `workspace` drives `StructuredResponder` (graph-free core plus injected
  `capabilities`/`context_events`/`extra_instructions`/`scope_factory`) and the
  `scg-search-structured` playbook → a schema-validated `emit_result`, streaming via the bus
  natively. GET carries additive `RunProvenance` (recipes/probes).
- **Provenance facets**: search RUN `agentic_search:run:` → `search_run`; MAP job `scg:map:` →
  `scg_map`; structured `structured:run` → `structured_run`.

## Trace projection — probes, the coordinator lane, honest settle

**A fast-tier run can spawn ZERO probes** — the root inlines every tool call. A projection keyed
only off `sub_agent` events therefore renders a blank trace, `results=[]` and `total_ms=0` beside
a correct synthesis. The coordinator lane exists for exactly that case.

- **Coordinator lane** — root `tool_result` events project into ONE synthetic lane (id
  `coordinator`, name `scg-search`, `source_id ""`). SECURITY: a line is a digest only — tool_id
  plus a scalar-input hint capped at 120 chars. **The tool's RESULT payload is never read**, since
  connector returns can carry secrets/PII; `scg_results` renders as `emitted N results`, never its
  entries. The lane has no `stop` lifecycle — its `agent_done` fires at settle with
  `empty = no data-bearing probe AND no results` — and it is appended to `payload.trace` at settle
  (kind `coordinator`, blank `result`) so a zero-probe run never snapshots `trace:[]`. Slots come
  from merged first-seen order (`_assign_lane_slots`) so settle reproduces the live interleaving.
- **Probe `tool_result`s ARE on this transcript/bus**, because a child loop inherits the parent's
  `event_logger` (core `AgentContext.child`). Every `tool_result` payload carries the emitting
  `agent_id`, so **every one must be classified `agent_id ∈ probe lanes` before projection** (live
  `_probe_emitter` / settle `probe_ids`); unclassified, probe tool calls mislabel into the
  coordinator lane. A spawn's `sub_agent` `start` always precedes the child's first tool call, so
  the lane is known in time. A probe's `scg_results` projects as ITS result cards; its other tool
  calls project ONE secret-free digest line onto its lane — the probe lane is otherwise
  lifecycle-only, and the evidence rides the stop summary.
- **The `sub_agent` STOP event's `detail` is only the `done_reason`.** The probe's real
  `EVIDENCE (pathway: …)` / `NO DATA …` block is `tq.task_result`, echoed as an additive `summary`
  on that event. `ProbeTrace.result_text` / `is_dead_end` are the ONE reader and the ONE
  classifier: the `NO DATA` prefix is the scg-path-probe return contract and the deterministic
  dead-end marker, feeding `agent_done.result` (the console's per-lane response panel) and `empty`.
- **`scg_results` = transcript-as-transport, and every search agent emits it.** The tool
  (`mewbo_graph.plugins.scg.results`, granted via `TRAVERSAL_TOOLS`, bound to probes by the
  capability gate) validates (≤50 entries, `extra="forbid"`, relevance/confidence 0..1) and
  returns `{ok, count}`. **It writes NOTHING** — the library never touches the api run store, and
  no `MapPhaseSink`-style DI is needed because the transcript already reaches the api.
  `ResultsProjection` maps entries → `SearchResult` with stable ids `r-<run_id8>-<n>` (root) /
  `r-<run_id8>-<agent8>-<n>` (probe emit, salted so concurrent emitters never collide); that id is
  the live↔settle dedup key. Entry `confidence` rides the wire verbatim AND folds into `relevance`
  only when `relevance` is absent. Playbook discipline: each probe emits ONCE before its evidence
  block; the ROOT emits once, before synthesis, ONLY for hits it grounded inline. Emitting is not
  terminal for anyone.
- **Cross-emitter SEMANTIC dedup** (`ResultsProjection.dedup_keys`): a card registers BOTH its
  normalized-url key AND its `(title, source)` key, so the root's url-less prose re-emit of a
  probe's hit collapses into the probe's card — FIRST emission wins. Held live and at settle.
- **Per-lane counts.** `_build_results` returns `emitter→count`, crediting
  `TraceAgent.results_count` and `agent_done`; the coordinator is credited only its OWN inline
  emits. `results_count` is KEPT (post dedup), `returned_count` the RAW emit (credited BEFORE the
  dedup skip in `_emit_results`); the delta is the lane's "N filtered", which the console surfaces.
- **Lane label = agent KIND, never the model.** `ProbeTrace.lane_name` reads
  `sub_agent.agent_type`, falling back to the literal `scg-path-probe`; the model is
  `TraceAgent.model` separately. The `start`-brief opener prefers a `SUB-QUERY:`/`PATHWAY:` line —
  the real task sits ~7 KB past the leaf-executor system-prompt header.
- **Per-lane telemetry** (`_lane_stats`) derives `steps`/`duration_ms`/tokens from the lane's
  `llm_call_start`/`llm_call_end` events (matched on `agent_id`; duration = last-end − first-start;
  tokens = cumulative on the last end, else per-call sums) plus the `sub_agent` stop aggregates,
  which win for steps/tokens.

### Metrics must be derived, never defaulted

- `_synthesis_metrics` derives `AnswerSynthesis.confidence` and `sources_count` from the trace,
  never from the echo fixture. `sources_count` = data-bearing probe lanes **keyed by `agent_id`** —
  the wire `source_id` is the shared parent grouping key and is useless for distinctness — ∪
  emitted results' `source` fields. `confidence` = data-bearing/probes when probes ran, else the
  mean folded entry score, else `(0.0, 0)`, at which the console suppresses the chip rather than
  render an unearned `0%`. The coordinator lane is NEVER a probe.
- `total_ms` = `started_at`→settle wall clock, stamped on `run_done`, `RunPayload` and the `_fail`
  payload, **and persisted on the record via `update_run(total_ms=…)` on BOTH the settle and
  `_fail` paths** — a settle path that omits it leaves `RunRecord.total_ms` at its default 0 while
  `run_done` carries the real elapsed.
- `RunStatsWire` (`_build_stats`): `probes` = lanes, `tool_calls` = tool_result count, tokens =
  cross-lane totals, `setup_ms` = `created_at` → first user/llm event (the pre-turn MCP-handshake
  gap a bare total hides), `search_ms` = `total − setup`. **Both `_ms` fields are None when the
  bracketing event is absent — never a fabricated 0.**
- **`probes` counts STARTED lanes, so a PERMANENT spawn refusal is invisible to it.** Core
  `spawn_agent.py` emits `sub_agent` only after hypervisor registration, i.e. once a spawn is
  accepted. Capacity refuses nothing under the `AgentQueue` scheduler — an over-subscribed spawn is
  accepted immediately (a real `agent_id`, `status: "submitted"`) and waits for a slot, earning a
  lifecycle. The remaining refusals are PERMANENT resolution failures (unresolvable project,
  unknown `agent_type`, unavailable model — `SpawnRefusalCode` in core `hypervisor.py`) and leave
  no trace-agent at all. `SpawnAttempts` reads the one place they DO leave a mark — the
  coordinator's own `spawn_agent`/`spawn_agents` `tool_result` — into
  `RunStatsWire.probes_rejected`. **Classify STRUCTURALLY: only a parsed `error.code` out of the
  `_SpawnOutcome.report()` envelope `{"error": {"code", "message", "permanence"}}` counts as a
  refusal.** Treating "does not parse as an accepted `{agent_id, status}`" as refused silently
  counts the loop's own per-tool-call timeout — a probe that WAS admitted and merely ran long —
  making the field a mislabelled timeout counter instead of one trending to zero. `spawn_agents`
  needs no such check: its envelope's `rejected` count is already computed the same way.
  `probes_rejected` stays out of `confidence`, which asks "of the evidence gathered, how much was
  substantive" and has no refused probe to average in.
- **`related_questions` is a PARALLEL structured call, not the agent's emit.**
  `RelatedQuestionsRunner` (`related_questions.py`) reuses the core no-loop `StructuredSynthesizer`
  (one emit + reask) over the query plus the synthesized answer. The settle worker starts it on a
  daemon thread BEFORE the `answer_delta*`/`answer_ready` events stream, so the answer reveals
  without waiting, then joins it (bounded) and appends a `related_questions` event **between
  `answer_ready` and the terminal `run_done`** — the stream closes on `run_done`, so it must land
  before. It is PRIMARY; `_build_related_questions` (the transcript read) is the fallback. The
  runner is INJECTED and only `get_search_runner()` arms it, so fake-runtime tests stay LLM-free.
- `run_started.session_id` carries the real session id, resolved before the emit on the happy path;
  fail-fast paths keep the tag, since there is no session yet.
- **Skills opt-out on BOTH drives.** Search and map pass `enable_skills=False` via
  `_skills_opt_out(runtime)` — a signature introspection, so a `run_sync` without the kwarg never
  raises — because the scg-* playbook is the ONLY trusted system-prompt extension.
- **`resolve_entity` errors on every search run (open).** It reaches search-run sessions via the
  scg plugin manifest and its capability-driven `build_for`, but a search RUN never satisfies
  `resolve_qa_ctx` (no QA answer, no `structured_workspace` event), so it always returns
  `wiki ctx not found`. Exclusion is not one-seam: the same registration serves
  `scg-search-structured`, where the ctx DOES resolve. The fix gates entity tools on wiki-ctx
  presence rather than the bare `scg` capability — don't hack the manifest.

## Two silent correctness traps (no exception either way)

1. **Two `StructureProvider` protocols share a name root and nothing else.** The code-graph's
   `StructureProvider` (`resolve` / `resolve_many` / `entity_key_of` / `exists`) resolves an
   `entity_key`↔code-node; SCG's `SourceStructureProvider` (`build_structure`, `providers/base.py`)
   *builds* a connector subgraph from a raw descriptor. The memory bridge MUST hand
   `InsightIngestor` a `ScgAnchorResolver`, or the default `CodeStructureProvider` cannot resolve a
   connector `source_key`, the `ANCHORS` edge is never created, and the insight is **written but
   silently dropped on read** (`memory_vector_search` defaults to `exclude_invalidated=True`).
   The resolver must be **KIND-AGNOSTIC**: `node_id = sha1(source_key|kind)`, so
   `ScgAnchorResolver.resolve` probes `_ANCHORABLE_KINDS = ("capability", "entity_type")`
   (`mewbo_graph/scg/memory_bridge.py:91`). An MCP-tool-list source mints `capability` nodes and no
   entity layer, so a hard-coded `make_id(source_key, "entity_type")` resolves none of them and
   drops every anchor. **A seam test seeding only `entity_type` nodes cannot catch this** — seed
   `capability` too. One fix point; `ScgGraphView` reuses the resolver.
2. **Read the flywheel via the store, not the expander.** Retrieve connector insights with
   `store.memory_vector_search(slug, qvec, k, filt=MemoryFilter(corpus="connector"))`, **not**
   `MultiplexExpander.expand` — its code-graph neighbour expansion no-ops for connectors, which
   have no tree-sitter CALLS/IMPORTS edges to walk.

## Session-drive invariants

Every LLM session is driven through the RunRegistry seam (`runtime.start_command` / `start_async`)
— the mapper (`map_job.py`) and the search drive (`orchestrated_runner.py`). **Bare `run_sync`
never registers a `RunHandle`**, so `runtime.cancel` becomes a no-op by construction and a dead
worker strands a `running` record with no terminal event.

**Terminal status comes from `runtime.summarize_session`** — the engine's single status chokepoint
— never re-derived from the raw completion payload, which coerces non-success `done_reason`s
(`awaiting_approval`, `max_iterations_reached`, …) to "completed" and invites guarding a
`"canceled"` spelling the engine never emits. Wrinkle: settling from inside the worker sees
summarize's `is_running` override (`status="running"`), so `_run_status` falls back to the
summary's verbatim `done_reason`; the raw payload is read only for `task_result`, which the summary
does not carry.

`source_type "text"` maps 422 at the route: the schemaless `LlmStructureProvider` is not
registered, as it needs an injected LLM.

## Substrate split — two stores by design

- **SCG *structure*** (schemas + pathways) is search-owned: its own `ScgStore` (dual JSON/Mongo,
  `agentic_search_scg_*` collections), separate from the run store, so a re-map rewrites graph
  nodes without touching in-flight runs. `node_id = sha1(source_key|kind)[:16]`, overwritten on
  validate (content-addressed, stable across re-maps); `parse_source` does a clean `delete_source`
  first, so re-indexing replaces rather than accumulates.
- **The learned layer** reuses the code-graph's `InsightIngestor` with `corpus="connector"`,
  anchored by `source_key` on the shared memory substrate (`runtime.wiki_store`). Zero
  re-implementation of atomic-note / anchor / dedup machinery.
- **Manifest hash + drift re-map.** `parse_source` stamps
  `mewbo_graph.scg.manifest.ManifestHash.of_descriptor_raw` (order-independent, schema-aware sha
  over sorted tool names + props + required) onto `SourceDescriptor.schema_version`.
  `WorkspaceSourceSync._drifted` recomputes it from the LIVE tool list on workspace save and
  re-maps drifted sources (idempotent, in-flight-guarded).
- **Map-time enrich.** The mapper playbook mints initial memory notes from the connector's own tool
  descriptions plus the workspace `instructions`/`desc`, which ride `SourceMapInput.nl_context` →
  `_render_user_query` as an **untrusted user-turn block, never `skill_instructions`**; anchored to
  capability `source_key`s.

## Workspace scope is a VIEW, never a store partition

A workspace does not get its own copy of the SCG. `docs/features-search.md` is binding: the SCG is
one tenant of the shared multiplex graph, and the wiki/search memory layers cross-pollinate without
explicit wiring — a per-workspace partition would sever that.

Per-source mappings are therefore GLOBAL and content-addressed (a re-map is a cheap idempotent
upsert every workspace mapping that source benefits from), and a workspace is a **scope filter**:
the source-id allowlist its enabled sources resolve to. `ScgScope` (`mewbo_graph.scg.scope`) holds
that allowlist on a `ContextVar`, and `ScgRouter.route` drops any candidate recipe whose steps
reach an out-of-scope source. **It rides an ambient ContextVar specifically because the `scg`
plugin tools call `ScgCore.store()` / `ScgCore.router()` with no scope argument** — the search
drive (`orchestrated_runner._scoped_to_workspace`) binds it for the worker thread, leaving the
un-owned plugin tools untouched. Cross-workspace insight attribution, if ever needed, is a TAG on
the note, not a partition.

## source_key scheme + security

`source_key = "<source_id>#<Qualified.Name>"`; a flat MCP tool list → `"<source_id>#<tool_name>"`.
This is the stable anchor the learned layer hangs off. **No secrets in nodes** — only a redacted
`auth_scope` descriptor string (e.g. `"oauth:repo"`); tokens stay in the connector config, never
copied into a node, transcript, or event log.

## Entity resolution — type-level offline, instance-level online

`TypeAligner` (`entity_resolution.py`) runs **at map time**: it compares `entity_type` nodes across
sources and deposits durable `RESOLVES_TO` edges (`method="type_align"`) — a weighted, provenanced
*hypothesis*, never an asserted join. **Abstain by default**: emit on the field-overlap heuristic
above `confident_threshold`; a band below it emits only if an *injected* LLM affirms it (one call
per band pair); no LLM ⇒ the band abstains. Instance-level matching ("is Jira #42 the same record
as Linear ENG-7?") is native to the probe agent over live data — there is no instance-ER module.

## Map-job progress — a DI sink, not a direct write

`MapSourceJob` (`map_job.py`) is the wiki-`WikiIndexingJob` analogue. All durable state lives in the
**agentic_search store** (`MapJobRecord` + its event log), not the SCG structure store, so it rides
the run-event-log + `RunSseGenerator` plumbing verbatim. `MapJobProgress.emit_phase`
(`map_progress.py`) dual-writes the `phase` event plus a snapshot patch — the wiki `emit_phase`
invariant (progress page and landing card never drift) without importing wiki `_ctx`.

**Lifecycle is settled by the drive, not the agent.** `start` drives the mapper session via
`runtime.start_command` (the same `RunRegistry` seam — serialized per session, cancellable via
`should_cancel`), so the worker that ran `run_sync` settles the job when the session ends:
`queued → running → completed|failed` plus a terminal event (`run_done` / `error` ∈
`TERMINAL_EVENT_TYPES`) appended to the map-job event log. The SSE stream closes on that event
instead of dying by idle timeout, and a crashed mapper can never stay `queued` forever. Those four
states are all of `MapJobStatus`; fine-grained progress is `MapJobPhase`
(`connect|introspect|parse|link|finalize`). `_settle` is the ONE terminal path — **event first,
snapshot second, so a snapshot failure never loses the terminal event.** Failure detection reads
`TaskQueue.last_error`; a raising `run_sync` is the secondary net.

The asymmetry vs the wiki: the mapper SessionTool lives down in `mewbo_graph.plugins.scg` and
cannot write this api run store. The API registers a writer at startup (`_register_map_phase_sink`
in `routes.py`) into `mewbo_graph.scg.map_phase.MapPhaseSink`, and the plugin emits cosmetic phases
through that DI seam. No writer (a graph-only install, or the API never initialised) → the phase is
skipped; the SCG structure write already happened and the phase is purely cosmetic. The wiki
`emit_phase` needs no sink — it writes its own store, already down in `mewbo_graph`.

## Playbooks + descriptors

- **Playbook delivery = `skill_instructions`, both sessions.** `playbooks.py:load_playbook` reads
  the bundled AgentDef body from `mewbo_graph.plugins.scg` (via `plugins_root()`), and both drives
  pass it as `skill_instructions`. That is the ONLY trusted system-prompt extension; untrusted
  input (source descriptors, workspace instructions) rides the user turn or context events.
- **`descriptors.py:SourceDescriptorBuilder` lives HERE by layering necessity:** it composes
  `mewbo_tools` (the PUBLIC `mewbo_tools.integration.mcp.list_server_tool_schemas(server, cwd=…)`
  seam — never import that module's underscore privates from an app) with
  `mewbo_graph.scg.types.SourceDescriptor`, and `mewbo_graph` may never import `mewbo_tools` (DAG).
  Both imports are guarded: `LookupError` = no configured connector (route 422), `RuntimeError` =
  deps absent / introspect failed (503). Auto-build is gated on `source_type == "mcp_tool_list"`; a
  descriptor-less openapi map keeps the mapper's fetch-natively contract. The built raw shape
  `{"tools": [{name, description?, inputSchema?}]}` is exactly what `McpToolListStructureProvider`
  parses.

## Node-query cache + landing summary

`/sources` and `/workspaces/<id>/graph` re-scan the node collection once per source per request —
on JSON that reloads and re-validates the whole `nodes.json` each time. `ScgStore` memoizes
`query_nodes` by the `(source_id, kind, name_contains)` triple (`_NodeQueryCache`,
`mewbo_graph/scg/store.py:95`): the base `query_nodes` is concrete (cache → delegate to the
driver's `_query_nodes_uncached`), and **every node write (`upsert_nodes` / `delete_source`) calls
`_invalidate_nodes()`**, so same-process reads — including an in-process map job re-driving the
parser — never see a stale graph. A 30 s TTL (`_TTL_S`) bounds cross-worker Mongo staleness, since
another worker's map job cannot reach our `clear`. **A new node-mutating write path must invalidate
too.**

`GET /workspaces/<id>/graph/summary` (`graph_routes.py`) is the same `_build_graph_payload`
assembly projected to `{scope, stats}` only, sharing `_resolve_scope` and `_safe_graph_payload`
with the full graph route (identical auth/404/degradation) and riding the warm cache — so the
landing health band never ships the full graph to render a four-number stat strip.

## Router — brute-force now, PPR is the documented scale seam

`ScgRouter.route` is the cheap, zero-LLM query-time job: embed → `vector_search` (brute-force
cosine) → expand one hop along capability/route edges → rank by `cosine + edge weight`. A
query-seeded Personalized PageRank with hub damping is the upgrade for catalog scale; it lands
**behind the same `route()` signature**, and is not built.

## Plugin boundary

The SessionTool wrappers in `mewbo_graph.plugins.scg` are **thin** — the deterministic logic lives
in `mewbo_graph.scg`. They reach it through the one `_core.ScgCore` resolver, which late-imports
the engine from `mewbo_graph.scg` and the shared wiki memory substrate from `mewbo_graph.wiki` —
both DOWN within the library, never up into an app. The late imports keep a core-only install
(optional extras absent) degrading to a structured error instead of crashing at plugin load.
AgentDefs (`scg-mapper`, `scg-search`, `scg-path-probe`) gate on the `scg` capability advertised at
session start; see `packages/mewbo_graph/src/mewbo_graph/plugins/scg/CLAUDE.md`.

## Testing notes

- `tests/agentic_search/scg/`. Use `scg.store.reset_for_tests()` plus the run store's
  `reset_for_tests()`; inject a fake embedder / LLM / runtime at the seams. Embedding is
  best-effort — a missing backend leaves a structure-only SCG, never a hard failure.
- **Fake-runtime transcripts MUST mirror the real engine event shapes.** The completion payload is
  `{done, done_reason, task_result, error?, last_error?}` (`orchestrator.py`) — **there is no
  `text` key**, and a fixture inventing one masks an always-empty-answer bug in `_terminal`. The
  `stop` event needs a realistic `summary` (an EVIDENCE or NO-DATA block) or the metrics read 0 and
  the evidence panel is empty. Copy shapes from the orchestrator's `append_event` call sites.
- SCG tests must NEVER spawn a real LLM or hit a real proxy.
