> ↑ [apps/mewbo_api/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md) · children: [scg/](scg/CLAUDE.md)

# Agentic Search ("Mewbo Search") — API Subsystem Guidance

Scope: `apps/mewbo_api/src/mewbo_api/agentic_search/`. A *workspace* selects connectors
("sources"), a *run* fans an agent out across them, and the console renders streamed results
plus a cited synthesis. This package owns the wire **contracts** and the **persistence**;
orchestration sits behind one seam (`runner.py`). Only the non-obvious decisions are here.

## Run lifecycle — the event log IS the search-event stream

`POST /runs` returns `{run: RunPayload}` plus top-level `run_id` / `session_id` / `status`.
Don't "fix" the duplicated fields — the console reads either shape. The echo runner completes
inline (`status="completed"`); the orchestrated runner launches the session on the runtime's
managed worker (`runtime.start_command`) and returns `status="running"` promptly.

The run's append-only, idx-keyed event log IS the normalized search-event stream. Three
transports project the same write:

| Surface | Endpoint | Role |
|---|---|---|
| Durable snapshot | `GET /runs/{id}` | reload / share / deep-link; `RunRecord` + accumulated `payload` |
| Live projection / replay | `GET /runs/{id}/events` (SSE) | replays from idx 0, then tails until a terminal event |
| History | `GET /workspaces/{id}/runs` | newest-first run records for a workspace |

A synchronous runner appends its terminal event (`run_done` / `error`) before returning; the
async one returns a `running` snapshot and its worker appends until the session ends. The SSE
generator tails either case identically. **Do not add a second status channel.**

## The shareable deep-link contract — `GET /runs/{id}` is self-sufficient

`/search?ws=<workspace_id>&run=<run_id>` is a deterministic multi-user URL: a cold browser opens
it with **one `GET /runs/{id}` plus an SSE attach — never a POST**. Three guarantees, locked by
`test_agentic_search_runs_routes.py`, extendable only **additively**:

1. **Snapshot self-sufficiency.** `GET /runs/{id}` returns everything needed to render with no
   other context: top-level `run_id`, `session_id`, `workspace_id`, `query`, `tier`, `status`,
   `created_at`, plus `payload`. The console reads these **top-level** — never move them under
   `payload`. `session_id` links the URL-addressed run to its auditable session.
2. **Cold-store durability.** Persisted through the run store (`create_run` plus the terminal
   `update_run(..., payload=…)` BOTH runners write), so a shared URL never 404s after a deploy.
   The read has **no per-session/per-user scoping**: any valid API-key holder resolves it.
3. **Clean 404 envelope.** An unknown run id is `{"message": "run not found"}, 404` — never a
   raw 500 or Werkzeug HTML. The SSE and cancel routes 404 the same way before opening a stream.

**`apps/mewbo_mcp` is a second consumer.** Its `search` / `get_search_run` /
`list_search_workspaces` tools wrap `POST /runs` plus the snapshot **poll** path, not SSE — the
bounded poll is the canonical await for a non-streaming caller. So the `POST /runs` envelope and
`RunRecord.status` are load-bearing for that await loop.

## Storage is SEPARATE from session transcripts (hard requirement)

The run store (`agentic_search_runs` + `_run_events` + `_workspaces`) is its own namespace. A run
is *backed by* a session, but its normalized projection lives here so snapshot reads stay fast
and survive session GC. JSON under `<cache_dir>/agentic_search/` (`workspaces/<id>.json`,
`runs/<id>/run.json`, `runs/<id>/events.jsonl`); Mongo mirrors it in `agentic_search_*`.
**Never route run state through the transcript event log.**

## The seam — `SearchRunner` (runner.py)

`SearchRunner` (a `Protocol`) is the swap point:

- **`EchoSearchRunner`** (fallback) replays the prototype fixtures over the REAL event log and
  store with NO LLM — filtered to the workspace's enabled sources, emitting the full normalized
  sequence (including the `answer_delta*` typewriter) and persisting the terminal snapshot. It is
  what makes console↔API integration testable without a model.
- **`OrchestratedSearchRunner`** (`scg/orchestrated_runner.py`) starts a tool-scoped
  `SessionRuntime` session and translates transcript events via the `events.py` builders.

`get_search_runner()` resolves **per run** (orchestrated iff `scg.enabled` AND ≥1 mapped source
in the SCG store, else echo) — never frozen at startup, so mapping the first source flips a live
process out of echo mode with no restart. Resolution is failure-soft: any import/store error
keeps echo. `set_search_runner()` is the explicit override (tests / manual swap; `None` restores
resolution) and always wins.

Transcript→event normalization is the runner's internal concern; the `events.py` builders are the
only shared surface.

## `schemas.py` is the single source of truth for the wire

It holds the entity/wire models, `RunRecord` (durable), and `SEARCH_EVENT_TYPES` (the vocabulary
the console's reducer switches on).

- `OUTPUT_CONTRACT_VERSION` is stamped on every `RunRecord` and emitted in `run_started`. Bump it
  only on an **incompatible** change, so the console can guard.
- Wire models are `extra="forbid"`; `clean_for_model()` whitelists bookkeeping keys (`_id`,
  `idx`, `event_count`) on load — lenient loads, strict models.
- Synthesis streams `answer_delta*` then a final `answer_ready`. `agent_*` drives the per-source
  trace; `result` drives arrival order. The prototype's `finish_delay_ms` / `t_ms` are deprecated
  decoration — **real ordering comes from event arrival, never a client timer.**
- **`SearchResult.meta: dict[str, scalar] | None`** carries agent-emitted structured facts
  (stars/language/version/state/size…) verbatim, **SCALARS only** — the projection silently drops
  a non-scalar so a connector blob can't leak. This is the SINGLE card-metadata channel: the
  console's open-vocab `resultMeta` classifier renders it as a footer, it is not a second wire
  field. The model proposes the keys; the console renders any of them.
- **`TraceAgent`** carries `kind`/`model`/`steps`/`duration_ms`/`input_tokens`/`output_tokens`/
  `results_count` (KEPT, post-dedup) and `returned_count` (RAW emit); `returned − results_count`
  is the lane's "N filtered".
- **`RunPayload.stats: RunStatsWire | None`** (probes · tool_calls · tokens · setup_ms ·
  search_ms) is populated at settle and is **None when underivable; NEVER a fabricated 0**.
- **`RunPayload.related_questions`** comes from a parallel structured call at settle plus a
  dedicated `related_questions` SSE event; the `scg_results` read is the fallback.
- Orchestrated runs populate ALL of it: `agent_*` includes a synthetic coordinator lane for
  root-inlined tool activity, `result` events come from `scg_results`, and
  `total_ms`/`confidence`/`sources_count` are computed, never defaulted. How each field is
  derived — dedup keys, lane labelling, per-lane counts, stats honesty — is owned by
  **`scg/CLAUDE.md`**; don't restate it here.

## `store.py` is the substitution boundary

Dual-backend JSON/Mongo, mirroring `project_store` / `session_store`.

- `save_workspace` is the verbatim write primitive both `create` and `seed` share — one persist
  path, so seed ids stay stable.
- Demo-workspace seeding is gated by `MEWBO_AGENTIC_SEARCH_SEED` (default on) and only fires on
  an empty store. Set `0` for a production start-empty.
- `reset_for_tests()` swaps a fresh seeded JSON store under a tempdir, still exercising the JSON
  backend through the routes.
- Mongo idx is atomic (`$inc` on `event_count` via `find_one_and_update`); JSON counts non-blank
  lines under a lock. **Keep idx monotonic per run** — the SSE `id:` line and replay-from-idx
  depend on it.
- `past_queries` is bounded at `PAST_QUERY_CAP`; a `running` entry is written up-front and
  patched in place on completion.
- `GET /workspaces?q=` is `search_workspaces` — ONE concrete method on the base class, inherited
  by both backends like `cancel_run`. Don't add per-backend overrides.

## `SourceCatalog` (catalog.py) — source→`allowed_tools` scoping

**`entries()` is live-first.** The catalog lists the **configured MCP servers** (id = server
name, `source_type="mcp_tool_list"`) from the merged `configs/mcp.json` chain plus the tool
registry; demo fixtures merge *after* them **only while demo seeding is on**
(`store.seeding_enabled()` — a live server id wins a fixture-id collision). A configured server
whose discovery failed stays listed `available=False` with the manifest's `disabled_reason` as
`unavailable_reason` — greyed out, never omitted.

`SourceCatalog.tools_for(source_ids, project)` scopes `allowed_tools`. Resolution order per
source: **live SCG capability nodes** (`kind == "capability"`) → the live server's registry
`mcp_<server>_*` ids → the illustrative `tools` declared beside the source in
`fixtures.SOURCE_CATALOG` (seeding on only — never a `TOOL_MAP` constant in the resolver). The
union is intersected with the live registry via `filter_specs()`. The wire shape
(`SourceCatalogEntry`) and the `tools_for` contract are fixed; only the resolution body changes.

⚠️ **GRANT INVERSION — capability nodes carry the RAW MCP tool name, not the registry id.**
`_scg_tool_ids` must mint `mcp_tool_id(source_id, node.name)` (the core convention, the same
translation `plugins/scg/route.py` ships for probe spawns), preferring the raw name only when IT
is the live id (a node already holding a built-in or full id — the fixture shape). Returning the
raw name inverts the grant: the `filter_specs` intersection DELETES every successfully-mapped
source's tools, while a failed-map source falls through to the live branch and binds every raw
registry tool, `create_repo` / `delete_branch` / `wiki_write` included.

**SEARCH grants are READ-ONLY:** `tools_for` filters the union through `_is_write_tool` (a pure
name-verb check — leading token vs the broad write set, trailing token vs a STRICT suffix set so
`get_latest_release` survives). The traversal verbs a graph drive also needs are unioned later by
`WorkspaceGraphBinding`, never here. **`RunRecord.allowed_tools` records the BINDING's actual
grant** (`orchestrated_runner` persists `binding.allowed_tools()` at seed), so the audit field
cannot diverge from what `run_sync` drove with.

**Map descriptors auto-build at the route.** `POST /sources/<id>/map` without a `descriptor` for
an `mcp_tool_list` source builds one via `scg/descriptors.py:SourceDescriptorBuilder` — the
connector's live MCP tool list through the `mewbo_tools` pool, composed **in the app** because
`mewbo_graph` may never import `mewbo_tools`. Schema only, never credentials. No configured
connector and no descriptor → 422; other source types keep the mapper's fetch-natively contract.

## Virtual MCP config + workspace scope

A workspace = name + instructions + a selection of MCP servers. That selection persists as a
DB-backed *virtual MCP config* — `WorkspaceMcpConfig` (`mcp_config.py`), an exact
`CredentialStore` sibling (one `_encode`/`_decode` seam; JSON mode-0600 file /
`agentic_search_workspace_mcp_configs` Mongo collection). It is **the source of truth for what a
run may reach**. `McpServerDef.headers`/`env` are the only secret-bearing fields and are ALWAYS
redacted outward (`redacted()` masks values, keeps key shape; `auth_scope()` names which auth a
server carries). `SearchRun.start` resolves the grant from
`WorkspaceMcpConfig.attached_server_names` first, falling back to the workspace's raw `sources`
when no config is persisted.

`WorkspaceSourceSync.on_workspace_saved` (`source_sync.py`) is the POST/PATCH hook: refresh the
virtual config, then auto-map newly-enabled **live** sources (idempotent — skips already-mapped
or in-flight; a terminal/failed job does NOT block a re-map, so a previously-unreachable source
re-maps once its URL is fixed).

- **The hook is register-and-return.** Only the config refresh plus NL fingerprint runs on the
  request thread; the whole auto-map fan-out (resolution, live descriptor builds,
  `MapSourceJob.start`) runs on one named daemon thread (`workspace-automap-<id>`), because a
  descriptor build is a live MCP handshake per source and in-request it blocks "Create Workspace"
  for N handshakes. The method returns the `Thread | None` so tests join deterministically; routes
  ignore it. **Never move the fan-out back in-request, and never let the thread body raise.**
- It also re-maps already-mapped enabled sources whose live tool list drifted from the stamped
  `ManifestHash`, and carries the workspace `instructions`/`desc` as untrusted `nl_context` to
  seed the map-time enrich step.
- **Workspace editing IS a graph-lifecycle event.** An instructions/desc edit moves no source and
  drifts no tool list, so the tool-list gates miss it. `NlContextFingerprint` (a `ManifestHash`
  sibling over the prose) stamped on `WorkspaceMcpConfigRecord.nl_fingerprint` (the honest
  internal home, NOT the wire `Workspace`) gates an idempotent re-enrich; the PATCH route fires
  the hook on a sources OR prose change (an instructions-only body has no `sources` key).
- **The per-workspace graph is a scoped VIEW** — removing a source narrows it without a delete.
  See `scg/CLAUDE.md` → "Workspace scope".

## Security invariants the real runner must uphold

- Workspace `instructions` are **UNTRUSTED prompt input** — never concatenate them into the
  system prompt.
- `allowed_tools` = selected sources ∩ project policy. The catalog union is the upper bound, not
  the final grant.
- Never persist secrets in workspace or run records; both are JSON/Mongo visible.

## SSE plumbing (events.py)

Inherited verbatim from `wiki/events.py`: a 2 KB primer frame at byte 0 plus padded heartbeats
defeat proxy buffering (OpenResty/NPM buffer ~4 KB). **Don't shrink them** — that reintroduces
the buffer bug. Frames are `id: <idx>\nevent: <type>\ndata: <json>\n\n`; the `id:` line carries
the idx so a flaky proxy resumes via `Last-Event-ID` / `?after_idx=`. The generator polls
`load_run_events(after_idx=...)` until a terminal event or the idle threshold (env-tunable:
`MEWBO_AGENTIC_SSE_MAX_IDLE`, `MEWBO_AGENTIC_SSE_SLEEP`). SSE auth uses `?api_key=` —
`EventSource`/`fetch` can't set headers everywhere, and `_require_api_key` honours the param.

## The real runner is the SCG — a cheap router

`OrchestratedSearchRunner` is **not** "spawn one agent per enabled source." It drives a
`scg-search` session over the **Source Capability Graph** — a routing controller indexing
*reachability* (schemas + pathways, **never data**), letting the agent fan probes out along
qualified pathways. The graph ops are tools the agent drives, never a parallel control loop;
tiers are one decomposition+probe budget knob over the single `ToolUseLoop`; **the connector's
real return is the only verifier.**

The tier rides `RunRecord.tier` (`POST /runs` body `tier`: `fast|auto|deep`, default from `scg`
config `default_tier`, echoed on `RunPayload`), never the runner instance. `RunRecord.model`
rides the same way: the optional body `model` (a LiteLLM name; non-string/blank → ignored, never
a 400 — the `/v1/structured` stance) wins over the tier's configured model at the drive
(`run.model or ScgConfig.model_for_tier(run.tier)`) and is echoed on every `RunPayload` so the
deep-link snapshot stays self-sufficient. Per-tier defaults are config
(`scg.traversal.tier_models`); the override is per-run only — no config write, no restart.
`GET /tiers` exposes the resolved per-tier preset (tier map → `llm.default_model`, exactly the
drive's fallback; a pure config read, NOT gated on `scg.enabled`) so the composer can show which
model a tier runs before submit.

The SCG *engine* lives **down** in the optional `mewbo_graph.scg` library; this app holds only
the runner seam and the map-job lifecycle glue, composing the engine via the `wiki` extra.

**Workspace binding ⇒ graph access.** `WorkspaceGraphBinding` (`scg/workspace_binding.py`) is the
ONE seam — any workspace-bound run gets the `scg` capability, the graph tools, and the `ScgScope`
source scope. A `/v1/structured` run on a mapped workspace goes graph-first
(`scg/graph_structured_runner.py`). Search runs stream live via `scg/run_streamer.py` (core
`SessionEventBus`). Durable decisions and the silent correctness traps: **`scg/CLAUDE.md`**.

## Testing notes

- Use `store.reset_for_tests()` for isolation; mock at the runner seam, not inside it.
- Tune the SSE idle/sleep env vars down so the generator closes promptly after the terminal event.
- Agentic-search tests must NEVER spawn a real LLM or hit a real proxy.
