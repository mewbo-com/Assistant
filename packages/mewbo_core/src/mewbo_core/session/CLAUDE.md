> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `session/` — the durable record, and every projection over it

Scope: `session_store.py` · `session_store_mongo.py` · `session_query.py` ·
`session_digest.py` · `session_event_bus.py` · `session_provenance.py` ·
`transcript_timeline.py` · `share_store.py` · `notifications.py` ·
`draft_stream.py` · `attachments.py` · `context.py` · `token_budget.py` ·
`compact.py` · `compaction.py` · `title_generator.py` · `commands.py`.

`session_runtime.py` is NOT here — it lives in `loop/`, because it is a DRIVER at
the top of the dependency graph reaching the store through `task_master →
orchestrator`; filing the two "session" modules together by name re-introduces an
import cycle. Its doctrine, including `SessionStatus`, is in `loop/CLAUDE.md`.

## Vocabulary

- **Event** — one append to the transcript. `append_event` on either backend is
  the universal choke-point; hooks, the event bus and the termination gate all
  hang off it.
- **Context event** — an event carrying session-scoped facts
  (`client_capabilities`, `source_platform`, `structured_workspace`).
  `SessionStoreBase.merge_context_events` is the ONE reducer (most-recent payload
  wins), shared by `latest_context` (trace path) and `summarize_session`
  (origin/recovery path).
- **Snapshot** — a derived read: `summarize_session`, a timeline, an origin. None
  of it is stored, so teaching a classifier a new arm reclassifies every existing
  session with no migration.
- **Compaction boundary** — the position in the message list a rewrite collapsed.
  A POSITION, never a timestamp.
- **Turn** — a user message and everything the engine appended before the next
  one. An INTERRUPTED turn has no completion event, which makes it the dangerous
  case for any anchor.

## Cost classes

The cross-cutting law is in the [root](../../../../../CLAUDE.md) ("Performance is
a contract at every boundary"). This is its session-store instance.

| Primitive | Cost | Note |
|---|---|---|
| `append_event` · `append_user_turn` · `append_terminal_event` | `O(1)` | one insert / one appended line; the bus publish is off the hot path |
| `resolve_tag` · `load_summary` · `load_title` · `is_archived` · `get_terminated_at` | `O(1)` | single-document reads on both backends |
| `load_recent_events(limit=)` | `O(limit)` Mongo, `O(one session)` base | Mongo overrides with sort-DESC + `limit`; the base materialises then slices |
| `tags_for_session` · `last_attestation_hash` | `O(1)` Mongo, `O(tags)` / `O(one session)` base | same override split |
| `load_transcript` | `O(one session's events)` | the largest live session is 10,266 events |
| `load_events_after(cursor)` | `O(matched events)` Mongo, `O(one session)` base | indexed `(session_id, ts)` range vs. stream-and-test |
| `latest_event_of_type(type, payload_key=)` | `O(1)` Mongo, `O(one session)` base | `ix_events_type_session_ts` walked BACKWARDS under `limit(1)`; bounded by the TYPE, never by a window |
| `session_digest` | `O(one session's summary-relevant events)` Mongo | the single-session shape of `list_session_digests`; what a poll's status read costs |
| `list_sessions` (store) | `O(sessions)`, ids only | cheap; what a caller does per id is not |
| `load_session_records` | `O(sessions)` | 2 queries on Mongo whatever the page size |
| `list_session_digests` | `O(sessions)` + `O(summary-relevant events)` Mongo, `O(all history)` base | the listing seam; the base has no index to project with and says so |
| `summarize_session` | `O(one session's events)` | folds whatever event list it is handed |

The JSON backend (`SessionStore`, the default driver) has no index at all —
`load_transcript` reads and parses `transcript.jsonl` line by line. Anything merely
slow against Mongo is worse there.

**`load_transcript` is a PER-SESSION read and must NEVER be called in a loop over
sessions.** Measured: 904 sessions / 136,733 events, a listing that summarised each
session's full transcript read all 136,733 to produce 904 rows —
`GET /api/sessions` at 4.15 s / 2.0 MB against 0.007 s for a `POST` doing two store
writes. That is `O(all events ever recorded)` wearing a listing's clothes: fine on
a fresh install, monotonically worse forever. **The loop's own docstring said it
loaded every transcript and it shipped anyway — a docstring that states a cost is
documentation, not a gate.**

**A derived field needed for MANY records comes from the store, never from the
children.** Two legal sources: a store-side projection/aggregation returning the
field, or a value maintained on the parent `sessions` document at append time.
Deriving fresh from the transcript is what buys reclassification with no migration
and is worth `O(one record)` on a DETAIL read; multiplied by the collection it is a
listing that reads all history.

### Shrink the INPUT, never fork the fold

`SessionDigest` (`session_digest.py`) is how that was closed, and the shape
generalises. The obvious repair — re-derive each row's fields with an aggregation —
is wrong: it makes a SECOND implementation of a derivation, free to drift until a
listed row disagrees with the session it names, and
`status`/`origin`/`capabilities`/`diff_stat` are not expressible in query language
anyway. What a summary reads is a SPARSE slice of a transcript, so the store
projects that slice and `summarize_session` folds it unchanged. Measured over the
live store: 138,471 events folded → 6,895 (20.1x), 5.03 s → 1.14 s, **all 908 rows
byte-identical** between the two paths.

Two invariants a driver must keep, both silent when broken:

- **A driver's selection is a SUPERSET of `SessionDigest.is_relevant`.** Surplus
  costs bytes; a shortfall drops a field off a row that still renders. The Mongo
  pushdown is built out of the predicates their owning classes publish
  (`SessionDigest.SIGNAL_TYPES`, `CapabilityEvidence.evidence_tool_ids`,
  `DiffStat.may_describe_edit`'s two gates) rather than restating them, and
  `test_session_listing_cost.py` drives one corpus through BOTH drivers.
- **The first event is projected whatever its type**, because `created_at` is
  `events[0]["ts"]`. On the live store 374 sessions open on `context` and 293 on
  `session` — types no listing would otherwise fetch.

A cheap gate a store pushes down belongs ON the class that owns the expensive rule
(`DiffStat.may_describe_edit`), stated as a one-way implication: *rejected ⇒ the
fold would find nothing*. That is what a test can pin. "The filter matches the
fold" is not.

### Indexing is necessary and not sufficient

`ix_events_session_ts` on `{session_id: 1, ts: 1}` is compound, so a transcript
read AND its `ts` sort are served by one index scan with no in-memory SORT. It was
already in place, and correct, while the listing took 4.15 s. **No index helps a
caller that asks for everything.** State the query SHAPE first — which fields, how
many documents, sorted how — then confirm an index serves it. Reach for the index
first and you make a full read fast, which is the same defect with a better
`explain`.

### A narrowing argument must narrow the WORK

`SessionRuntime.load_events(session_id, after)` once narrowed only what was
RETURNED, filtering in Python over an already-materialised transcript. A poll whose
`after` matched ZERO events cost 0.259 s on the largest session, plus 0.211 s in
`summarize_session` over the full log — 0.470 s to answer "nothing new", against
0.010 s on a 15-event session. **The response was empty and the cost tracked the
session: measure the TIME, never the payload.** Both halves are now store
primitives (`load_events_after`, `session_digest`); the poll floor is 0.093 s.
**The caller hands the store the NARROWING, not a filter to apply afterwards.**

**A pushed-down bound compares differently from the value it came from, and must
be the COARSER of the two.** Mongo orders these timestamps as TEXT while the cursor
orders instants, so `EventCursor.store_floor` is truncated to the second and
`matches` still decides. The text ordering is sound only because
`SessionStoreBase.stamp` canonicalises a caller-supplied `ts` on the way in — the
mirror-ingest endpoint accepts client timestamps, and one carrying a different UTC
offset would sort one way as text and another as an instant.

**Whole-session status must not be recomputed from the full log on a per-poll
path**, and must never be narrowed to the window to make the number look good — it
answers from the session's DIGEST, the same projection a listing row gets,
byte-identical in result.

### Fail closed on a filter

**A filter argument that cannot be applied must refuse, never silently widen to
"everything".** An unparseable `after` cursor falling through to the unfiltered
transcript made `GET /events?after=not-a-timestamp` a `200` carrying 14.8 MB.
Fail-open converts a caller's bug into a full-transcript read that reports success
— invisible on both ends. The live trigger was never a hypothetical malformed
value: this API's own timestamps contain `+`, so a client echoing one back without
percent-encoding sends a space.

The refusal belongs to the surface that ACCEPTED the value — the route 400s.
`SessionRuntime.load_events` keeps widening on an unparseable cursor as a defensive
default, because an in-process caller has no channel to refuse on; nothing may rely
on that arm. **Parse once at the edge and pass the parsed value inward**
(`EventCursor`), so the store, the runtime and the route cannot drift into three
spellings — and so "absent" and "unparseable" stay distinguishable.

### Profiling this layer

Credentials come from the container's own environment, so they are never typed,
never in shell history, and never in this file:

```bash
docker exec assistant-mongo-1 sh -c 'mongosh --quiet \
  -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin mewbo --eval "<JS>"'
```

```js
db.sessions.countDocuments({}); db.events.countDocuments({});  // shape
db.events.getIndexes();                                        // what exists
// The fattest records — a per-record read's cost at the tail, not the mean.
db.events.aggregate([{$group: {_id: "$session_id", n: {$sum: 1}}},
                     {$sort: {n: -1}}, {$limit: 5}]);
// What a hot query really does.
const s = db.events.find({session_id: "<id>"}).sort({ts: 1})
            .explain("executionStats").executionStats;
print(s.executionStages.stage, s.nReturned,
      s.totalDocsExamined, s.executionTimeMillis);
```

| `explain` signal | Reading |
|---|---|
| `stage: COLLSCAN` | no index serves this query |
| `totalDocsExamined` ≫ `nReturned` | an index is used and is not selective |
| `totalDocsExamined == nReturned`, both large | the index is fine and the QUERY is the defect |
| a `SORT` stage appears | the sort ran in memory; its key is missing from the index |

To time an internal phase, difference two stored event timestamps — the technique
and its three traps are in `../../../CLAUDE.md` → "Profiling the engine".

**Asserting cost in a test: count store reads, never wall-clock.** Subclass the
store, count `load_transcript` calls, assert the bound — one per listed session is
the defect; a fixed small number is the contract. A read count is exact and names
the defect in its own assertion; a timing assertion is flaky under a parallel
suite, and the first spurious red gets it deleted rather than fixed.

### Checklist — adding a store method, or a field to a listing

- **Declare the cost class in the docstring.** An undeclared primitive is one every
  caller will assume is `O(1)`.
- **Grep the call sites for the loop shape.** A per-record cost inside a listing
  reads as innocuous at every individual call site.
- **If a listing needs the field:** can the store project it in ONE query, or can
  the parent `sessions` document maintain it at append time? If neither, the field
  does not belong on the listing.
- **Check whether Mongo overrides it.** Adding a targeted read without a base-side
  answer leaves the default JSON driver silently paying `O(one session)`.
- **Confirm every narrowing argument narrows the STORE query.** If it filters in
  Python after a full read, say so in the docstring.
- **An unparseable filter refuses.** No silent widening.
- **Measure on the deployed artifact and write the number down.**

## Compaction

**Sliced by POSITION, never by ISO timestamp.** `Z` sorts above `+00:00` for the
same instant, so one same-second mix silently drops a post-boundary event.

**A state transition on one path and a side effect on the other.** `/compact` sets
`done_reason="compacted"` — a genuine terminal. Automatic mid-loop compaction
mutates the message list in place, emits a `context_compacted` event, records it on
the hypervisor handle, and leaves `OrchestrationState` untouched. When you write
"after compaction", say which one you mean.

**TWO implementations, over different data structures, sharing only a prompt.**
`compact.py:compact_conversation(events, mode)` rewrites the EVENT transcript —
auto-compact (`orchestrator._maybe_auto_compact_async`) passes PARTIAL, manual
`/compact` passes FULL, and those two MODES share one prompt template.
`loop/tool_use_loop.py:_compact_messages(messages)` rewrites the in-flight MESSAGE
list mid-run, keeping `messages[0]` and the recent tail. Both produce the
structured `<analysis>` + `<summary>` block via `get_compact_prompt()`, and that
prompt is the ONLY thing reaching both — a change to either one's slicing or
rebuild reaches just the implementation you edited.

**A rewrite must leave the tool chain PAIRED, or the next call is a 400.** The
provider rejects a dangling `tool_use` and an orphan `tool_result` alike, and the
recent-tail slice strands both. `llm/llm_resilience.py:repair_tool_pairing` is the
ONE repair (drop a result whose call is gone, synthesize an interrupted result for
a call whose result is gone); it runs after the slice AND after a doom-loop halt,
which breaks out with the `AIMessage` already appended. Same invariant by another
route: `_has_dangling_tool_use` blocks a `model_control` switch while an EARLIER
batch sits unpaired — the in-flight batch is excluded, or every switch looks unsafe.

Compaction-resilient state lives where the LLM rebuilds it each step or where a
message-list rewrite cannot reach: the agent tree (`render_agent_tree()`, whose
root copy sits in `messages[0]`, which compaction never slices, and which
`check_agents` re-reads live), child results (`AgentHandle.result`), and
`AgentHandle.progress_note`. **If you add orchestration state, ask whether it
survives a compaction.** If not, store it on the hypervisor or the session runtime,
not in messages.

**The transcript-timeline parity fixture is a binding contract.**
`tests/fixtures/transcript_timeline_corpus.json` is replayed by
`tests/test_transcript_timeline_parity.py` AND by the console's
`timelineParity.test.ts`. The Python assembler and the TypeScript one must agree
over it — reading one side and inferring the other is how they diverge.

## Filterable facets — FACT vs CLASSIFICATION

A listing builds each row by projecting that session's transcript, so the only
narrowing that costs nothing is one decided BEFORE that read — and once a listing
can be PAGED, a narrowing that cannot be decided there gets the page cut from the
wrong set (`loop/CLAUDE.md` → "A page is cut from what the query ADMITTED"). That
collides with the rule that origin is derived and never stored. Both are right:

- **A CLASSIFICATION stays derived.** `origin` and `SessionStatus` are readings of
  a session; never storing them buys reclassification with no migration.
  Materialising either to make it filterable trades that away permanently and buys
  nothing a consumer cannot do for free — the summary row carries both, so
  filtering post-summarisation costs one comparison.
- **A FACT and an INTENT are stored.** `pinned_at` is an assertion only a user can
  make; `projects` records what actually happened. Neither is a reading a better
  classifier could improve. `SessionQuery` (`session_query.py`) owns the boundary:
  every field on it is answerable from the record alone, which is why it can be
  pushed into the driver.

**`pinned_at` is a reversible stamp, not a set-once one** — same shape as
`archived_at`/`terminated_at`, not the same law, and it doubles as the ordering key
so a surface needs no second sort field. **Unpinning `$unset`s the field rather
than writing null**: a sparse index omits documents MISSING a field, not documents
holding null, so an explicit null for every unpinned session would put the whole
collection back into the index that was made sparse to stay out of. That is also
why `ensure_session` has no `pinned_at` default.

**`projects` is a SET, and that is the whole point.** An auto-select session starts
bound to nothing and the agent may switch projects several times mid-task, so "the
project of a session" is not one value and a single-field filter would miss every
project a task moved into after the first. ONE hook on the `append_event`
choke-point maintains it, which works because both binding writes already funnel
through it: session creation writes a `context` event carrying `project`, and
`rebind_workspace` writes a mid-run switch as a MERGED context event. The hook is
**best-effort** — the cost of losing one is a session missing from a filter, not a
lost transcript event.

**⚠️ Tags cannot carry a label like this.** The `tags` collection is keyed BY THE
TAG (`_id: tag` on Mongo, `index["tags"][tag]` on the json driver), so a tag
resolves to exactly ONE session. A `project:<name>` tag would let the second
session using that project STEAL it from the first, which would then also lose it
from `tags_for_session` and could change its derived origin. `SessionTag` bakes the
session id into tag values to dodge precisely this, and vcs-pickup relies on
re-pointing being silent. **Tags are identifiers here, not labels** — reach for a
record field when many sessions must share something.

## Session origin and capability evidence

`session_provenance.py:SessionOrigin` is the single classifier for *who spawned a
session*. The record stores **no** origin field — every session is created
identically — so origin is reconstructed from two durable signals:

1. **Tags.** Which KIND implies which origin is declared ONCE, as the `origin=`
   field of a `SessionTag._KINDS` row — never re-enumerated in prose. Not every row
   declares one, and the channel rows match on an INFIX, not a head prefix.
2. **Fallback: the first context event's**
   `client_capabilities`/`source_platform`/`client`. A mobile surface
   (`android`/`ios`/`aura-*`, via `is_mobile_surface`) → `MOBILE`, checked BEFORE
   the generic `source_platform` → CHANNEL fallback so a mobile client never reads
   as a channel.

Tags win (they survive even when a job stored empty context). `summarize_session`
attaches it as `origin`; the console badges and filters the landing page on it.
`SessionStore.tags_for_session` is the concrete reverse of `resolve_tag`.

**⚠️ ONLY a capability NO CLIENT ADVERTISES ABOUT ITSELF may imply an origin.** The
capability fallback is a list a CLIENT sends, and the console sends a FIXED header
naming every capability it can RENDER — so an `apps` arm filed every ordinary
console chat under `APPS`, and because the landing page hides non-`user`/`channel`
origins by default it did not merely mislabel those sessions, it HID them (measured
live: 1 of the 100 most recent sessions read as `user`). What survives is `wiki` and
`scg`, written server-side by headless jobs that genuinely scoped the session;
`scg` is load-bearing because the `scg:map:` tag row declares `origin=None`
DELIBERATELY and reaches SEARCH through exactly that branch. **Before adding a
capability arm, ask whether any client could assert it about itself. If yes, it is
an advertisement and must not classify.**

**`CapabilityEvidence` (same module) answers "what did this session actually DO".**
It holds each *earnable* capability (`stlite`/`ask_user`/`generative_ui`/`apps`) to
a durable artifact event (`widget_ready`, `user_question`, `generative_ui`,
`app_ready`…) or a SUCCESSFUL `tool_result` for a tool that capability gates —
several of these tools are terminal-free, so a rejected call still leaves a result
behind and would otherwise read as proof. Server-written scopes pass through
untouched: the table IS the list of what must be earned, so adding a row is what
makes a capability answerable. **It deliberately does NOT use
`SessionToolRegistry.capabilities_for`** even though that is the live
capability↔tool map: the registry is built per-`Orchestrator` from plugin discovery
and is EMPTY in a process that only lists sessions, so a lookup there would blank a
capability that really was used — and half the ids live in `mewbo_api`, which core
cannot import. Each row names its producer instead. Atomic and I/O-free like
`SessionOrigin`: events arrive as method args, so it folds into a pass a caller
already makes.

## Recovery must not delete an interrupted turn

**`continue` truncation anchors on the last `recovery` marker, NOT the last
`completion`.** Its only job is to drop a STALE PRIOR continue attempt (the
`recovery` audit event + the synthetic turn it drove) so the transcript does not
accrete duplicate recovery prompts. Anchoring on the last *completion* is a
data-loss bug: a run killed mid-flight emits no completion for its turn, so the
anchor falls back to the PREVIOUS completed turn and `truncate_after` physically
deletes the entire interrupted turn — user message, tool calls and results. That
presents as a console "prior traces hidden" bug, the FE faithfully rendering a
transcript the backend erased. No prior `recovery` marker ⇒ nothing stale ⇒
preserve everything. **A recovery/stitch that DELETES transcript must key off a
marker present ONLY when there is genuinely something stale to delete, never off a
terminal event a crash can skip.**

**The `recovery` marker is also a LEDGER, so it may not be deleted or rewritten.**
Its payload is `{"action": …}` plus an OPTIONAL `trigger` (`RecoveryTrigger`,
`loop/session_runtime.py`) naming a non-human originator. Absent means a human
asked — every console/CLI recovery today — so those events stay byte-identical. A
present trigger is what makes an AUTOMATIC re-drive idempotent: "has this trigger
already fired on this session?" is answerable from the transcript alone, with no
store field (`loop/CLAUDE.md` → "Automatic one-shot re-drive"). Two consequences: a
reader must treat the key as optional, and the `retry` action — which deletes the
failed turn outright — can never carry a trigger, because the marker it would need
to read back is exactly what it erases.

## Hard termination

**A KILL SWITCH, not another recoverable state.** `terminate_session` stamps
`terminated_at` set-once — the `terminated_at: None` filter clause on Mongo,
`setdefault` on the json index — mirroring `archived_at`, and there is deliberately
**NO un-terminate primitive**. `summarize_session` derives `status="terminated"`,
which beats EVERYTHING including a still-unwinding live run (checked AFTER the
`running` override, since cancellation is cooperative and `is_running` briefly lags
a terminate), and forces `recoverable=False`.

`SessionTerminatedError` (HTTP surfaces map it to 410 Gone) is raised at the
`resolve_session` core seam so a terminated transcript can't be FORKED — copying it
into a fresh session would let an agent launder its way around the kill. **That
guard is on the FORK leg ONLY: the `session_tag=` leg of the same call resolves a
terminated session and hands it straight back**, so a tag-path caller owns its own
`is_terminated` gate AFTER the call — and the two live ones differ deliberately:
channels replies once and drops the message, while vcs-pickup mints a fresh session
and RE-POINTS the tag so a CI/issue event never dead-ends. Adopting
`resolve_session` buys the resolve-or-create seam, not the kill switch.

The `_on_terminate` callback list is the cascade seam (best-effort, summed int
returns): the API registers the trigger store's `cancel_for_session` so a dead
session's armed triggers can't fire into it. **Terminated ≠ deleted:** reads stay
open; only run/steer/recover/fork are blocked.

## Trace provenance — one seam, one classifier

Every execution path funnels through ONE observability entry point:
`components.py:langfuse_session_context`, opened once in `Orchestrator.run`. Its
`langfuse_propagate` (a thin wrap of Langfuse `propagate_attributes`) attaches
tags+metadata to **every** child observation — nested LangChain CallbackHandler
generations AND `langfuse_trace_span` spans — because propagation is
contextvar-based. **So enrich filterability HERE, never at per-call-site
handlers.** The seam stays taxonomy-free: it propagates whatever `tags`/`metadata`
it is handed and never learns the prefixes.

`session_provenance.py:TraceProvenance` is the single classifier (pure, no I/O,
sibling to `SessionOrigin`). `derive(tags, context, surface)` folds a session's
durable signals into `tags` (low-cardinality `key:value` chips — `origin`/`product`/
`session_type`/`surface`/`project`/`repo`/`branch`/`workspace`/`model`) and
`metadata` (the superset incl. high-card ids `wiki_id`/`search_id`/`vcs_*`/
`channel_id`/`thread_id`, plus `worktree` and `capabilities`). Tags win over
context on overlap; an unrecognised manual `/tag` label is skipped so it never
masks the real product. `origin` is the console enum
(`user`/`wiki`/`search`/`channel`/`apps`/`structured`/`draft`/`mobile`); `product`
refines it (adds `vcs`, whose sessions badge under the `channel` origin).

**`SessionTag._KINDS` is the ONE tag grammar:** the rows that BUILD a tag are the
same rows `classify` reads (via `origin_of`) and `_facets_from_tags` reads (via
`parse`), so adding a session KIND is one row plus one constructor and a stamped
prefix can no longer lack a classifier arm. **Adding an ORIGIN** is the
`SessionOrigin` member, the `_ORIGIN_PRODUCT` map, and the console mirror
(`types.ts` union + `utils/sessionOrigins.ts` `ORIGIN_META`, exhaustive, and the
source of both the filter list and the badge).

**A tag PREFIX an app-side seam also stamps belongs to core, not the app** —
`MOBILE_TAG_PREFIX` and `APPS_TAG_PREFIX` are declared here and imported by their
stamp sites so the classifier and the writer cannot drift. A duplicated literal
fails silently in one direction only: a prefix stamped by the app with no matching
`classify` arm files every one of those sessions under `user` and raises nothing.
Tag a session at creation — context fallback only catches un-tagged paths.

Session types worth knowing: the structured family uses `structured:run` /
`structured:fast` (its `mode:"synthesis"` no-loop lane) / `draft:stream` — the tag's
2nd segment becomes `session_type` so the three stay individually filterable under
one product. Search has three `session_type`s under product `search`: a RUN
`agentic_search:run:` → `search_run`, and a MAP-source job under either
`scg:map:` or `agentic_search:scg:` → `scg_map`. **A search RUN must never read as
a map.**

**`source_platform` IS the client-surface channel** — the existing param on
`start_async`/`run_sync` → `Orchestrator.run`. In-process callers (CLI) pass the
kwarg; HTTP clients (console/mcp/HA) send an `X-Mewbo-Surface` header the API reads
(default `api`) — mirror `X-Mewbo-Capabilities`, and keep it in the CORS
allow-headers or it is dropped cross-origin. Channels pass their platform;
vcs-pickup derives `github`/`gitea` from the forge host. Precedence in `derive`:
explicit param > context `source_platform` > `vcs:`-tag forge > `unknown` (kept
visible, not dropped, so un-stamped paths are findable).

- `project = managed:<uuid>` is an ephemeral worktree → the `worktree` metadata
  facet, never a high-card `project` chip.
- **`transcript_sink` is the CLI local-vs-synced facet.** A `transcript_sink`
  context key (`synced` when the local-first CLI mirrors to a remote API, else
  absent ⇒ local) is promoted by `_facets_from_context` to a low-card chip —
  SEPARATE from `surface` (which stays `cli`). Do NOT confuse it with a
  `source_platform` event (`SessionOrigin.classify` reads that as CHANNEL).
- **Runtime-granted capabilities must be OVERLAID into `derive`'s context.**
  `derive` reads `client_capabilities` from the merged context — the
  CLIENT-ADVERTISED set only. A runtime grant is live in the run yet absent from
  context, so it would vanish from the trace's `capabilities` facet.
  `Orchestrator.run` overlays the AUGMENTED set (`_session_capabilities`) into the
  context dict it hands `derive`, keeping `derive` a pure
  `(tags, context, surface)` transform. A no-op passthrough today: no capability is
  runtime-granted (parent file → "Capability gating" for why the last provider was
  removed).
- **The two capability surfaces mean DIFFERENT things, deliberately:** the trace
  facet is the ADVERTISED ∪ granted set (what the run could reach — what you filter
  on), while the session row reports only what the transcript PROVES was exercised
  (`CapabilityEvidence`). A row chip is a claim about the session and must be
  earned; a trace facet describes availability, so the superset is right.
  `summarize_session` deliberately does NOT probe the runtime predicate per session
  — a live store read in a session LIST is the wrong tradeoff.
- Derivation runs at run start; apps write context/tags BEFORE invoking the
  runtime. A brand-new session minted inside `run()` (CLI first turn) has only
  surface+origin until its first context event exists — by design.

## Session event bus (push SSE)

`session_event_bus.py:SessionEventBus` — per-session in-process pub/sub, fired from
`append_event`. Publish is **non-blocking** off the hot path (bounded drop-oldest
queue). Single-process is correct (gunicorn `--workers 1`); a
`RedisSessionEventBus` subclass is a **documented seam, deliberately NOT built
(YAGNI)**. The API SSE generator subscribes → emits backlog once → drains the
queue, applying content-key dedup against the subscribe↔backlog race and
**draining completely before `stream_end`** so the terminal `completion` event
(published during the close-race window) is never dropped.
