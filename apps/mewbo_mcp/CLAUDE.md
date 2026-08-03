> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo MCP — project guidance

Scope: `apps/mewbo_mcp/`. The Streamable-HTTP facade that exposes a Mewbo
deployment to external agents.

## Why a separate process

FastMCP is ASGI; `mewbo_api` is WSGI (Flask-RESTX / Gunicorn). They cannot share a
process. The MCP server runs standalone on `/mcp`, calls the REST API over HTTP,
and uses the official `mcp` SDK (`FastMCP`). **It never imports `mewbo_api`** —
every fact it needs crosses the HTTP boundary.

## Auth: token pass-through

The facade is *curation*, not a security boundary. Issued keys are full-power and
go only to trusted agents. Per tool call:

1. `auth.extract_bearer_token(ctx)` pulls `Authorization: Bearer <token>`.
2. `auth.validate_token(token)` validates locally via the shared `mewbo_core`
   `KeyStore`'s **expiry-aware `resolve_key`** (or master-token equality), so a
   credential the API would reject on expiry is never forwarded. `verify_key`
   (expiry-blind) is deliberately NOT used.
3. The validated token is forwarded verbatim to REST as `X-API-Key`. There is no
   privileged service identity; the master token never goes on the wire.

**Scopes are deliberately NOT checked here.** Scope enforcement belongs next to
the routes it protects, where `X-API-Key` is re-validated. Gating tool exposure by
scope here would need a second tool→scope mapping that must stay in sync with
whatever the API enforces — the drift-prone parallel decomposition the exposure
gate below exists to avoid.

⚠️ **Shared KeyStore is a deployment requirement.** `auth.validate_token` calls
`mewbo_core.key_store.create_key_store()`, which picks the file driver
(`$MEWBO_HOME/api_keys.json`) or the Mongo driver (`api_keys` collection) from
config. The MCP service **must** share storage with the API — same `MEWBO_HOME`
volume or same `MEWBO_MONGODB_URI`. Without it, only the master token validates
and every issued key is rejected.

## Code shape — every tool group is an atomic class

`tools.py` models each group as one frozen dataclass (`SessionTools`, `WikiTools`,
`IntegrationTools`, `SearchTools`, `StructuredQueryTools`, `TriggerTools`). The
class holds the injected `RestClient` plus per-feature config (`timeout_s` /
`poll_interval_s` for the polling groups, `ClassVar`s like `TURN_TEXT_TRUNC` /
`TERMINAL_STATUSES` for the rest) **as state**, and exposes behaviour as methods
over it; pure helpers are `@staticmethod` / `@classmethod`. `server.py` is the only
FastMCP touch point and constructs one per call —
`await SessionTools(client).history(…)` — so tests stub only the HTTP boundary.

The module-level `_as_dict` / `_dict_list` / `_as_list` are cross-cutting coercion
primitives; everything feature-specific lives on its class. **Adding a group means
adding a class, not a pile of module functions.**

## The five invariants — one shared seam each

Every tool converges on the shape of `search`/`get_search_run`.

1. **Structured error envelope.** Every `@tool(...)` is wrapped by `_enveloped`
   (`server.py`): a `RestError`/`ValueError` becomes
   `{"error": {code, reason, retryable}}` (retryable = transport/5xx), never a
   raised exception or leaked transport text. The partner seam is
   `rest._error_detail`, which reads the API's `{"error": {"reason"}}` envelope and
   **drops a raw HTML body** (a Werkzeug error page) rather than dumping it into a
   tool result. Do not add per-tool try/except.
2. **Async run-handle for every long await.** The bounded await is ONE helper,
   `tools.bounded_poll(fetch, is_terminal, *, timeout_s, interval_s)`. Every long
   tool returns a handle and has a companion: `search`→`get_search_run`,
   `ask_wiki`→`get_wiki_answer`, `structured_query`→`get_structured_run`. A timeout
   returns `status:"running"` + the handle, never a terminal failure. **`run_id` is
   per-run, not the session id** — the API mints `"<session_id>:r<seq>"`; MCP only
   passes it through.
3. **Smallest-useful-payload default.** Projection lives MCP-side: `list_sessions`
   caps to 20 newest compact rows; `read_wiki_structure` defaults to
   `detail="stats"`; `list_integrations` drops boilerplate descriptions;
   `get_session_history full` references the agent tree instead of inlining it.
4. **Schema matches behaviour.** `create_session` takes a single `tag` (the API
   stores one `session_tag`); `ask_wiki`'s `model` is genuinely optional;
   `list_sessions` returns the `project` it filtered on; `overview` reads the API's
   authoritative `status`/`title` off `/events` rather than reconstructing a
   timeline.
5. **Discovery + actionable ids.** `list_projects` wraps `GET /api/projects`
   (registered name + git `repo`/`aliases` identity), which is how a caller learns
   what to pass to `create_session`'s `project`.

Three traps:

- **The QA terminal signal is the snapshot's `status`**, authoritative when present:
  a `running` snapshot is never terminal even when it carries a `sources` block.
  The sources-block check is only the fallback for status-less snapshots. Treating
  the block as terminal yields complete-looking but truncated answers.
- **A tool-call-only turn's `(no content)…` summary** is rendered from the turn's
  steps (`→ called <tool>`), keyed on our own `NO_CONTENT_SENTINEL`. A trailing
  `call:default_api:<tool>{}` in model text is a response-normalization concern
  belonging at the LiteLLM/adapter seam — never string-matched here.
- **`ask_wiki` must serialize ALL block kinds.** `_block_text` enumerates every
  `WikiEmitBlock` variant — `table`→markdown table, `diagram`→mermaid placeholder,
  `hr`→`---`. An allowlist silently drops the rest and the answer returns
  `complete` while being non-responsive.

**Worktree lifecycle is system-owned.** `create_session` returns
`{session_id, status}` only — no worktree handle, and there is no
`cleanup_worktree` tool or `SessionTools` method (`tests/test_tools.py` asserts its
absence). The API's `on_session_end` hook provisions and reaps it. Minimize the
session knobs exposed to callers: fewer knobs means better fast-model agentic
accessibility.

## Tool exposure — gate AT REGISTRATION, on TWO orthogonal axes

`config.py`'s `ToolGroup` + `EffectTier` + `McpToolPolicy` are the gate, and
`build_server`'s local `tool(group, tier)` registrar is the ONE seam applying it.
Every registration reads `@tool(ToolGroup.X, EffectTier.Y)` — there is **no bare
`@mcp.tool()`** in this app, deliberately.

**Why two axes.** The subsystem cut alone makes "expose wiki READS but not wiki
ASKS" inexpressible: reading a generated page and asking the wiki a question are
the same PRODUCT but not the same COST — one returns stored bytes, the other starts
a model run.

| Tier | Means | Examples |
|---|---|---|
| `read` | returns stored data; no model call, no run | `list_wiki_pages`, `read_wiki_page`, `get_wiki_answer`, `list_sessions` |
| `navigate` | graph traversal over stored edges; no model call | `graph_neighbors` |
| `ask` | starts a model run | `ask_wiki`, `search`, `structured_query`, `submit_insight` |
| `drive` | creates, steers, or kills a session | `create_session`, `terminate_session`, `cancel_trigger` |

**Exposure is the CONJUNCTION.** `policy.allows(group, tier)` is two set
memberships and nothing else — no dispatch, no `if kind ==`. Allowing the `ask`
tier does not expose a withheld group's ask-tier tools, and allowing a group does
not expose tiers the operator turned off. Note `get_wiki_answer` is `read`, not
`ask` (it re-fetches a stored snapshot), and `submit_insight` IS `ask` (the server
condenses the raw text with a model call before storing).

**Filter at construction; never register-then-`remove_tool`.** A withheld tool's
function is returned undecorated, so FastMCP never sees it: `Tool.from_function`
never runs, no JSON schema is built, and the tool exists in neither `list_tools` nor
dispatch. A default `build_server()` constructs 15 schemas out of 24. Pruning after
registration produces the same observable surface with absence as a side effect
rather than a decision, and moves the gate away from the line its author edits.

**The group IS the subsystem taxonomy — don't invent a third one.** `ToolGroup`
mirrors the atomic tool-group classes in `tools.py`. A flat list of bare tool NAMES
would be a parallel, drift-prone copy of a decomposition that already exists. The
effect axis is not a second taxonomy of the same thing; it asks a different
question about the same tool.

**Fail-closed by construction** — four load-bearing properties:

- Every registration must name a group AND a tier, so a new tool cannot be added
  without both decisions being made on the line its author is editing.
- `_DEFAULT_EXPOSURE` must decide every `ToolGroup` and `_DEFAULT_TIER_EXPOSURE`
  every `EffectTier` — one import-time `RuntimeError` loop covers both axes, so a
  new member can never silently auto-expose.
- `MEWBO_MCP_EXPOSED_GROUPS` / `MEWBO_MCP_EXPOSED_TIERS` are ALLOWLISTS (naming a
  member is the only way to expose it), resolved independently, and an unknown name
  is a hard startup `ValueError` — never a silent no-op. Same rule as
  `extra="forbid"` on a Pydantic wire model.
- The instructions blurb is DERIVED from the policy (`_instructions`), so the
  server can never advertise something it withholds — invariant 4 applied to the
  server's own description. It splits on the effect axis too: withhold `ask` and
  the blurb stops promising answers and stops listing long-running tools.

**Default GROUP exposure: `sessions` + `wiki` only.** Agentic Search is a *product*
surface owned by the console and the REST API, and `structured_query` routes
GRAPH-FIRST through a mapped Search workspace, so exposing it re-opens that surface
by another door. `integrations` (capability discovery) and `triggers` (an operator
surface) are likewise outside the minimum agent-facing set. Nothing here touches the
engine: `scg.enabled` stays on and `/api/agentic_search/*` keeps serving the
console, because MCP reaches search only over REST. The tools are **gated, not
deleted** — `MEWBO_MCP_EXPOSED_GROUPS=sessions,wiki,integrations,triggers,search,structured`
restores the full surface.

**Default TIER exposure: all four open.** The effect axis ships open on purpose. It
exists so an operator CAN narrow the facade — `MEWBO_MCP_EXPOSED_TIERS=read,navigate`
yields a wiki that lists pages and walks the graph but answers no questions and
starts no runs — not to narrow it by default. Widening a *group* stays the explicit
act.

**This is curation, not access control.** Issued keys are full-power: a
key-holding caller can still hit `/api/agentic_search/*` on the REST API directly.
Withholding a group or tier removes it from *this facade's* surface only.

## Response bounds — bound the QUERY, don't truncate the reply

Mewbo's internal `ToolSpec.max_result_chars` cap cannot reach an external MCP
client (different process), so each tool carries its own ceiling. The rule:
**truncating a large answer to N characters is context-safe but useless** — the
caller gets a mangled prefix of a JSON blob. Bounding or paginating at the QUERY
returns a COMPLETE, SMALL, CORRECT answer.

- `list_wiki_pages` → `limit`/`offset`; the reply carries `truncated` +
  `nextOffset` so a caller pages forward.
- `graph_neighbors` → the shared `WikiGraphNeighborsArgs` bounds (`hops<=3`,
  `limit<=500`) plus a facade default of **25** nodes rather than the model's
  internal 50 (an external caller's context is not ours to spend). Its edge list is
  the one place a response-side cap survives, because a node limit does not bound a
  hub's incident edges at all: `WikiTools.NEIGHBOR_EDGE_LIMIT` caps them and reports
  `edgesTruncated` + the true `edgeCount`. Announced, never silent; the node list
  stays complete.
- `read_wiki_structure` is bounded by its `detail` tier (default `stats`) + `limit`.
- **Still unbounded:** `list_wiki_projects` (whole-project list, no cap) and
  `read_wiki_page` (a full markdown body). Neither has a natural pagination key
  today; both are worth a bound the next time this surface is touched.

## Tool groups (24 tools; 15 exposed by default)

Signatures and route mappings live in the `server.py` docstrings; this lists only
what the code does not say for itself.

**A. Sessions — create & control** *(`drive`)* — `create_session`,
`send_followup`, `interrupt_session`, `terminate_session`.

- `create_session` resolves `project`/`repo` by registered NAME or git identity
  `host/owner/repo`, and auto-provisions a fresh worktree+branch unless
  `branch`/`worktree` targets an existing one.
- `send_followup` re-engages an idle or finished session, not just a running one;
  only a terminated session rejects.
- `interrupt_session` pauses the current run and leaves the session steerable (idle
  is a no-op, `status:"no_active_run"`). `terminate_session` is the permanent,
  irreversible, idempotent kill: it cascade-cancels every armed trigger
  (`cancelled_triggers` in the result), reads stay open, there is no un-terminate.

**B. Sessions — discover & read** *(`read`)* — `list_sessions`,
`get_session_history` (tiers `overview` < `turns` < `steps` < `full`; the last two
need `turn=N`, 1-based), `get_agent_tree`.

- **`list_sessions`' `project` filter is a SERVER-side query param** (pushed into
  core's `SessionQuery`, decided at the store before any transcript is read). A
  client-side rule would only ever see a session's CURRENT `context.project` and
  miss every project an auto-select session already switched out of — the
  accumulated set lives only in the store. `status`/`since` stay client-side;
  neither is a document-level fact the store can decide before reading a transcript.
- It re-sorts pinned-first-then-newest locally even though the server returns that
  order, because `limit` truncates AFTER sorting — trusting the wire order silently
  drops a pinned session that sorts past the cutoff. The honest-outcome facets
  (`recoverable`, `blocked_code`, `failure_reason`, `models_tried`, `pinned`,
  `pinned_at`) are pure projection of `summarize_session` output.
- A turn's `done_reason` reads `"blocked"` or `"unmet_goal"` when the derivations
  below fire; `error` is omitted from a `turns` row unless the run failed.

**C. Wiki — query & ask** — `list_wiki_projects`, `read_wiki_structure`,
`read_wiki_page`, `list_wiki_pages`, `graph_neighbors`, `submit_insight`,
`ask_wiki`, `get_wiki_answer`.

- ⚠️ **`read_wiki_structure` is MISNAMED for what it returns**: it maps to
  `GET .../graph`, the multiplex CODE GRAPH, not a page index. The page index is
  `list_wiki_pages`, which is also the only way to discover the page id
  `read_wiki_page` consumes.
- `graph_neighbors` answers "what calls X" / "what does X contain" in ONE directed,
  kind-filtered, multi-hop BFS, with no model call. The route reuses the down-layer
  `WikiGraphNeighbors` engine and its `WikiGraphNeighborsArgs` AS the query
  contract, so bounds and vocabularies have exactly one definition and
  `extra="forbid"` reaches the HTTP boundary for free.
- `submit_insight` is the human/external-agent write surface for memory notes
  (in-session agents use `wiki_submit_insight`): the server condenses raw text into
  atomic claims, auto-anchors each to the tree-sitter graph, dedups and merges.
  `condense=True` (default) sends `raw`; `condense=False` sends `content` (verbatim
  ≤200-char claim). The endpoint returns 201 on store and **200 with `ok:false`**
  when every claim is rejected — a normal advisory outcome, so it does not raise
  `RestError`.
- `ask_wiki` is SSE-only: stream the start frames to read the `meta` `answerId`,
  then `bounded_poll` the snapshot until `status` is terminal. `get_wiki_answer` is
  the `read`-tier companion that re-fetches it.

**D. Integrations / capability discovery** *(withheld by default)* —
`list_integrations` (projected to `{tool_id, name, kind?, enabled?}`, so a caller
knows what ids to pass to `create_session`'s `integrations`) and `list_projects`
(the discovery surface for `project`).

**E. Mewbo Search** *(withheld by default)* — `list_search_workspaces`, `search`,
`get_search_run`.

- `list_search_workspaces` **drops `instructions` and the full `past_queries`** —
  `instructions` is untrusted prompt input and must not leak to a consuming agent;
  the history is console state.
- **The await mirrors `WikiTools.ask` and is forward-compatible with the async
  runner.** `EchoSearchRunner` finishes synchronously, so `POST /runs` is already
  terminal and `search` returns with zero polling; `OrchestratedSearchRunner`
  returns `running` immediately and `search` polls until terminal or `timeout_s`,
  returning the partial rather than hanging. **Don't add an SSE consumer here** —
  the snapshot poll is the canonical bounded await; SSE is the console's live-reveal
  transport.
- **Two detail tiers, not four.** `answer` (default) returns the cited synthesis + a
  compact result index (`id/source/kind/title/url/relevance`) so citations resolve;
  `full` adds `snippet/insight/refs`. Runs are shallow, unlike sessions.
- The projection always drops the per-source trace and decorative fields
  (`related_people`, `image`, `embed`) — console-render signal, not search signal.
- **`SearchTools.TERMINAL_STATUSES` is duplicated, not imported** — this process
  talks to the API over REST only. The `ClassVar` mirrors
  `agentic_search.schemas.TERMINAL_RUN_STATUSES`; update the mirror if that set
  changes.
- **Workspace resolution is by id-or-name** (agents think in names). An ambiguous
  name or no match raises `ValueError` naming the candidates rather than silently
  searching the wrong workspace. Workspace authoring is NOT exposed — read+search
  only.

**F. Structured query** *(withheld by default — it routes graph-first through a
mapped Search workspace)* — `structured_query`, `get_structured_run`. The server
runs an agentic session and emits a JSON-Schema-validated object; a mapped Search
workspace goes GRAPH-FIRST (route→probe→aggregate→emit) and the result carries
additive `provenance`. The MCP param is `tool_ids`, not `tools`, to avoid shadowing
the `tools` module; it is forwarded as the body's `tools`.

**G. Triggers** *(withheld by default)* — `list_triggers` (server-side filters
forwarded verbatim, compact rows; `args`/`provenance`/`created_by` dropped, since a
caller managing triggers already knows the kind) and `cancel_trigger` (idempotent,
same contract as `terminate_session`).

- **Deliberately NO create/arm tool.** Arming is an agent-facing, IN-SESSION
  capability — the `schedule_trigger` SessionTool an agent calls on *itself* to end
  its turn and be woken later — never a general external mutation surface.
  `TriggerTools` is the read/manage counterpart for triggers armed elsewhere.

## `timeline.py` — DRY pairing with the console

A Python port of `buildTimeline` / `computeTurnTokenUsage` from
`apps/mewbo_console/src/utils/timeline.ts`, kept in sync so the console renders the
same turns MCP exposes. Parity is enforced by `tests/test_timeline.py` against
shared fixtures. **Change turn-boundary or token-usage logic in either file and you
must update both plus the parity test.**

Turn boundary rule (matches TS): a `user` event opens a turn, the next `assistant`
event closes it, and a `completion` defensively closes an open turn when no
`assistant` precedes it. `tool_result` events inside a turn are steps. Token totals:
PEAK root input (context pressure), SUM output, SUM sub-agent peaks.

MCP is RICHER than the console in outcome ATTRIBUTION — a projection concern.
Boundaries and token math stay in lockstep; these do not:

- **`done_reason` resolves from THREE sources and needs all three.** `TurnMeta`
  carries a reason only when a `completion` closed the turn ITSELF — the rare shape,
  since an ordinary run writes its final `assistant` event first and the completion
  lands on an already-closed turn, where the assembler keeps the reason only if it
  classifies as a failure. Reading that field alone yields `null` for every turn of
  sessions `overview` correctly calls failed. So: turn metadata, then the failure
  record (`run_failure.reason`, also the row's `error`, read BEFORE the
  turn-metadata gate because a failure beside real prose carries no turn metadata),
  then `_completion_outcomes` — a pass pairing each `completion` with the most
  recently opened turn, the only source covering successful turns and the
  halted/verification/budget outcomes the assembler does not model as failures.
- **Token totals are TWO quantities, both reported.** `total_input_tokens` sums
  per-turn PEAKS and measures context pressure; `total_billed_input_tokens` sums
  EVERY call's input and is what the per-call event log adds up to. On a tool-heavy
  session the second is several times the first — the whole prompt is resent each
  step — so reporting only the peak under a "total" name reads as a metering bug.
- **A BLOCKED run must not read as `done_reason: "completed"`.** The loop leaves
  `done_reason` at `"completed"`/`"error"` for a run that hit a user-actionable wall
  and carries the real fact in the completion payload's `blocked_code`
  (`repo_access`/`network`/`forbidden`/`quota_exceeded`); an external agent trusting
  `done_reason` alone acts on a failure as if it succeeded. `_completion_outcomes`
  reads `blocked_code` off each completion, validated against core's `_BLOCKED_CODES`
  (**imported, never re-listed**), and `build_timeline` OVERRIDES `done_reason` to
  `"blocked"` and stamps `Turn.blocked_code`. This is the ONE derivation MCP does;
  the four honest-outcome facets are derived once in core's `summarize_session`.
- **UNMET_GOAL rides a SEPARATE `outcome_assertion` event, not the completion.** A
  run can end CLEAN — no halt, no blocked envelope — and still not have done its job;
  every signal the loop owns says success. A session-end hook appends
  `outcome_assertion` (`{reason, detail, source}`) AFTER the completion to contradict
  it, and core's `summarize_session` promotes `"completed"` → `"unmet_goal"`.
  `_completion_outcomes` folds the assertion onto the turn whose completion it
  follows (a stray one before the terminal is ignored, mirroring core resetting its
  pending assertion on each completion; validated through core's `OutcomeAssertion`
  model, so a malformed record is dropped). `build_timeline` promotes ONLY a
  `"completed"` turn, and blocked — checked first — OUTRANKS it, as in core. The
  `overview` tier needs nothing here: `status` off the `/events` meta is already
  authoritative.

**`extract_trigger_events` is a SEPARATE pass, not folded into `build_timeline`.**
It recognizes `trigger_armed`/`trigger_fired`/`session_terminated` REGARDLESS of
turn-open state — the console parses them BEFORE its open-turn gate, so a marker
arriving between turns (the common case for `trigger_fired`, which wakes a session
before the next `user` event) is never dropped. Keeping it out of `build_timeline`'s
loop leaves turn reconstruction untouched. `SessionTools.history()` wires it twice:
`full` surfaces a turn's markers (`out["triggers"]`, omitted when empty), and
`overview`'s `terminated`/`terminated_at` prefers the API's authoritative meta but
FALLS BACK to the transcript's `session_terminated` marker — the same
authoritative-or-reconstructed idiom `_overview` uses for `status`/`title`.

**`generative_ui` — the degradation contract, and why MCP is the surface that needs
it.** A `present_ui` event's `spec` is a component tree only a React surface can
draw; its `alt_text` is that tree rendered as prose, carried precisely so a surface
without the vocabulary has something to show. An MCP caller has no renderer at all,
and the `present_ui` step it already sees is a bare receipt naming a `ui_id` and a
node count. So `full` surfaces `Turn.generative_ui` as
`{ui_id, summary, alt_text, ts}`, capped by `_cap_field` like any inlined field.
**`spec` is deliberately dropped** — inlining markup a caller cannot use is exactly
the smallest-useful-payload violation invariant 3 exists to prevent.

**It reads the assembler's `generative_ui` ROWS, never the raw events.** The rows
UPSERT on `ui_id` (the wire's declared replace key — `present_ui`'s result tells the
model to pass it back to replace a panel), and the console's `buildTimeline` upserts
identically, with the core corpus fixture binding both. Reading `turn.events`
directly stacks near-copies: a run that refined a panel surfaces the stale version
beside the current one with nothing marking which is live. A payload with no `ui_id`
is dropped rather than given a synthetic one, again matching both, and `ts` is the
panel's FIRST appearance, not the refinement's. **`Turn.steps` reading raw events is
fine and this is not** — a step is one row per event, a panel is one row per
`ui_id`, so only the second has a rule that can drift.

**`extract_question_events` — another SEPARATE pass.** Ports the console's
`question` role: `user_question` opens a pending `QuestionMarker` keyed by `call_id`
and `user_question_answered` settles it in place (status + answers + answered_via).
Two deliberate departures from the trigger pass, both TS-faithful: (1) a
`user_question` with NO open turn is DROPPED — a question is only ever asked
mid-run, so `turn_index` is always a real int; (2) the marker OMITS the console's
`call_token`, the single-use bearer secret the answering surface POSTs — this
read-only projection must never hand an external agent the answer credential (the
same rule the search group applies to workspace `instructions`). Not wired into
`SessionTools.history()` today; it exists for TS parity and the shared fixture test.

## Env vars

| Variable | Default | Notes |
|---|---|---|
| `MEWBO_API_URL` | `http://localhost:5124` | REST API base URL; a trailing slash is stripped. Point it at the API's gunicorn port in a container deployment. |
| `MEWBO_MCP_HOST` | `127.0.0.1` | Bind host. Set to `0.0.0.0` in a container. |
| `MEWBO_MCP_PORT` | `5127` | Bind port. Deliberately not the API's port. |
| `MEWBO_MCP_EXPOSED_GROUPS` | *(unset ⇒ `sessions,wiki`)* | Comma-separated ALLOWLIST of `ToolGroup` names. Unknown name fails startup. |
| `MEWBO_MCP_EXPOSED_TIERS` | *(unset ⇒ `read,navigate,ask,drive`)* | Comma-separated ALLOWLIST of `EffectTier` names — `read,navigate` yields a zero-model-call facade. Unknown name fails startup. |
| `MEWBO_MASTER_API_TOKEN` | `msk-strong-password` | Break-glass token; must match the API's value. |
| `MEWBO_HOME` | `~/.mewbo` | Data dir for the file-driver KeyStore. Must match the API. |
| `MEWBO_MONGODB_URI` | *(unset)* | When set, selects the Mongo KeyStore driver — must be the API's DB. |

## Run

```bash
uv run mewbo-mcp
```

Entry point `src/mewbo_mcp/server.py:main` → `build_server()` →
`server.run(transport="streamable-http")`. MCP endpoint: `http://<host>:<port>/mcp`.
