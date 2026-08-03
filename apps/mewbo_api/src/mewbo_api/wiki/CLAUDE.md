> ↑ [apps/mewbo_api/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# MewboWiki — API Subsystem Guidance

Scope: `apps/mewbo_api/src/mewbo_api/wiki/` (thin HTTP/SSE + job-lifecycle glue) and the
wiki SessionTools under `packages/mewbo_graph/src/mewbo_graph/plugins/wiki/`. The reusable
substrate they drive — code graph, multiplex memory, embedder, retriever, store, domain
models — lives in `mewbo_graph.wiki`; see `packages/mewbo_graph/CLAUDE.md`. Everything
readable straight from the code is left out.

## The pipeline

A fixed seven-phase state machine running inside a normal Mewbo session, not a separate
service: an agent owns the run, the wiki built-in tools persist state, SSE streams progress.

```
clone → scan → graph → enrich → plan → pages → finalize
```

**GraphRAG ordering law.** The knowledge graph is built BEFORE generation and generation
CONSUMES it. `enrich` (a `wiki-enricher` fan-out mirroring `wiki-page-writer`) mints entities
from AST symbols + SOURCE prose (docstrings/comments/READMEs), never from generated page
prose, grounding each LLM-proposed entity against high-confidence AST symbols; anything that
cannot attach to a symbol/span is dropped. `plan` is entity-aware; `pages` query the entity
graph via `resolve_entity` instead of re-extracting. Leiden/Louvain community detection is
deliberately SKIPPED (non-reproducible on low-degree code graphs) — pages are planned by the
free AST/module/package/directory hierarchy plus entity co-occurrence.

## Progress — one writer, two transports

`emit_phase(ctx, name)` (`plugins/wiki/_ctx.py`) is the ONE writer of the current phase: it
writes the SSE event AND updates the persisted job snapshot in the same call, which is why
the landing card and the indexing page cannot drift.

| Field | Written by | Read by |
|---|---|---|
| `IndexingJob.phase` | `emit_phase` | `/v1/wiki/projects` snapshot + SSE `phase` |
| `IndexingJob.phase_started_at` | `emit_phase` | FE `IndexingProgress` ETA extrapolation |
| `IndexingJob.total_pages` | `commit_plan` | Landing card page-bar denominator |
| `IndexingJob.pages_submitted` | `submit_page` | Landing card page-bar numerator |
| `IndexingJob.scanned_count` / `total_count` | `scan`, per file | Scan sub-progress, both views |

Add a new progress signal HERE, never in one transport only. The FE atomic class
`apps/mewbo_console/src/components/wiki/progress.ts` reads all of them.

- **A phase is stamped when its work STARTS, never at a predecessor's return.** Each phase has
  one emitter at its boundary tool except `enrich`, which has no tool: `emit_phase_once` fires
  from inside the first real enrich write (`mint_entity`), guarded on `ctx.job_id` because QA
  agents mint entities too and a QA ctx has no job to advance. Stamping it at the tail of
  `wiki_build_graph` marked the phase started when its PREDECESSOR ended, reading as a
  multi-minute "graph plateau" stall. A future tool-less phase emits the same way.
- **Phase is MONOTONIC within a job.** A resume re-clones and re-scans, so those boundary tools
  legitimately re-run on a job far past them and re-stamp `clone`/`scan` with a later
  `phase_started_at` — the bar walked backwards. `emit_phase` asks
  `IndexingJob.regresses_to(name)` (order lives beside the `IndexingPhase` literal as
  `PHASE_SEQUENCE`) and drops a backward move from **both** surfaces; dropping it from one
  breaks the can-never-drift property. A deliberate RESTART is exempt.
- **Read ctx and job DEFENSIVELY at this seam.** It is reached with duck-typed stand-ins
  carrying only what their own tool needs; a hard attribute read crashes unrelated tools inside
  progress reporting.
- **`emit_scope_preview` is the same seam again** — the `scope_preview` job event AND
  `IndexingJob.scope_preview` in one call. Never add a second channel. `ScopePreview` is a
  validated model, ABSENT (not zeroed) on a full rebuild: zeros would claim a scope was
  examined and found empty.

Legacy `IndexingStatus` (`queued|scanning|finalizing|complete|cancelled|failed`) is the coarse
six-state lifecycle — keep it for compatibility, never as fine-grained phase.

## `_job_wire` — the one job→wire serialiser

`routes.py:_job_wire(job)` serialises an `IndexingJob` for both `GET /index/<job_id>` and
`GET /jobs/active`. The console types both responses with one `IndexingJob` interface, so a
field stamped by only one route would make that type lie.

- **`sessionId` is stamped at READ time** from `store.get_job_session(job_id)`, not carried on the
  model: the binding lives on its own store surface (`attach_job_session`), and two writers of one
  fact would mean every resume had to remember both. A graph-only index is sessionless, so the key
  is ABSENT rather than empty — the FE reads absence as "nothing to watch" and renders no affordance
  rather than a disabled one. Best-effort: a store hiccup degrades the jump, never a progress poll
  running twice a second. It deliberately does NOT ride the SSE stream — the binding is job state,
  not a phase transition, and duplicating it re-opens the two-writers problem.
- **`isActive` is `IndexingJob.is_active` projected onto the wire** so the console reads ONE answer
  instead of keeping its own status list. Derived, never stored. The snapshot dumps
  `exclude_none=True`, so a plain bool is always present and absence never reads as false. New
  lifecycle questions get a predicate on the model, projected here.

**`get_qa_snapshot` stamps `sessionId` identically** from `store.get_qa_session(answer_id)`
(binding on `attach_qa_session`, outside `QaAnswer`), same absent-never-empty, same
best-effort. The two sites duplicate a four-line try/except deliberately; a shared helper plus
the plumbing to inject it into two unrelated routes is bigger than what it removes.

**`GET /v1/wiki/sessions/<id>` resolves both kinds through one reverse lookup**, but they route
differently: an indexing session belongs to a PROJECT, a Q&A session to one ANSWER
(`?answer=<id>`). The `qa` branch returns `answerId`/`fromPageId`/`question` alongside
`slug`/`kind`, all off the already-loaded `QaAnswer` at no extra lookup. `fromPageId` keeps the
deep link honest — omit it and the console route falls back to a hardcoded `core` page id
(`router.ts`) a project need not have. That is a route default, not a landing page:
`landingPageId` remains the only canonical "enter this wiki" target.

## Capability gating and the indexer ceiling

Wiki AgentDefs (`wiki-indexer`, `wiki-enricher`, `wiki-page-writer`, `wiki-qa`,
`wiki-qa-probe`) load only when the session advertises the `"wiki"` capability.
`WikiIndexingJob.start` and `WikiQaSession.start` append `{"client_capabilities": ["wiki"]}` as
a context event right after creating the session. Without it `spawn_agent` cannot look up the
wiki-* AgentDefs and the run appears "stuck after scan" — the parent finishes scan with no
child to hand off to. Renaming or adding a wiki capability means updating BOTH `jobs.py` and
`agent_registry.py`.

`_start_indexer_session` (`jobs.py`) is the shared seam `start()` and `resume()` both call,
passing `allowed_tools=INDEXER_TOOLS, strict_tool_scope=True, enable_skills=False`.
**`strict_tool_scope` is what makes the allowlist real** — the permissive branch unions every
non-MCP builtin back in, and an AgentDef's `disallowedTools` frontmatter is discarded by the
body-only playbook loader, so without it NEITHER layer enforces a ceiling.
`mint_entity`/`relate_entities` stay OUT of `INDEXER_TOOLS`: those are writes the enricher
CHILD performs, and a parent need not hold a tool to grant it to a child (a child's specs are
filtered against its OWN allowlist).

## Developer mode — graph-only indexing (zero-LLM, sessionless)

`runtime.developer_mode` unlocks an AST-only index driven by `GraphOnlyIndexer`.

- **The gate is at the `post_index` ROUTE only**, forcing `graph_only=False` unless the flag is
  on. Mode is STICKY thereafter (the submission sidecar round-trips it), so refresh and recovery
  preserve it. Do NOT re-gate at `start()` — that flips a project's mode mid-life.
- **Graph-only jobs are SESSIONLESS** (`GraphOnlyIndexer.run` on a daemon thread,
  `session_id=""`). Three consequences:
  - **QA short-circuits BEFORE the session**: `post_qa` returns
    `documentation_unavailable_response(slug)` when `project.graph_only`, because the store's
    raise fires INSIDE a probe's `wiki_read_page` SessionTool where the route-level
    `@app.errorhandler(DocumentationUnavailableError)` never runs.
  - **Recovery never reaches the agent path**: `WikiIndexingSessionEndHook` cannot match a
    sessionless job, so `WikiResume.resume` branches on the sticky `submission.graph_only` and
    re-drives `_start_graph_only_index` (idempotent from scratch — graph-only builds no
    `ResumePlan`). Both auto-recovery and manual `/resume` funnel through `WikiResume.resume`,
    so one branch covers both.
  - **Cancel is cooperative**: the indexer re-reads job status at each phase boundary and bails
    before `_finalize` (the ONLY `complete`/Project writer), so a cancel is never clobbered by a
    late terminal write.
- `DocumentationUnavailableError` → `documentation_unavailable` (409, `retryable:false`) via
  `WIKI_CODE_STATUS` + the errorhandler. MCP maps the 4xx to a non-retryable envelope for free;
  graph-exploration tools are unaffected.
- **Flipping `graph_only` ON drops the project's existing pages** at finalize: the Project is
  stamped `graph_only=True`, so surviving pages are unreachable behind the doc-read guard and
  would collide with fresh ones on a flip back.

## A wiki `Project` is an INDEX of a repository, not the record that one exists

A **repository** is a product-level object in the registry at `/v1/git/repositories`; a **wiki
`Project`** is what exists once someone indexed it. Registering is inert; indexing is a paid agent
run, and nothing about registering starts one — the wizard can be opened already naming a
registered repository, a prefill and the only coupling.

- **A `Project` row is MINTED in exactly two places**, both terminal-phase writes:
  `plugins/wiki/finalize.py` and `GraphOnlyIndexer._finalize`. A `Project` is a display snapshot of
  a COMPLETED index, so there is no correct place to mint one earlier. (A partial `update_project`
  on an existing row is a different verb, with the graph phase stamping `resolution` as a third
  caller.)
- **The registry reconciles TOWARD the wiki, never the reverse.** Its list endpoint adopts any
  indexed slug it is missing; the wiki reads nothing from the registry, which is what keeps every
  wiki flow working where the registry is empty or never called.
- **`Project.slug` and a registry slug are the SAME canonical string**
  (`host[/namespace…]/owner/repo`, one grammar in `mewbo_core.repositories.RepositoryRef`). A
  two-segment slug is not a repository identity and is skipped by adoption rather than coerced.

## `Project` is a snapshot; `ProjectSettings` is the editable record

**`Project` is rebuilt WHOLESALE on every successful (re)index** — both finalizers construct a
full `Project(...)` and upsert — so anything PATCHed onto it directly is wiped by the next
reindex. `Project` is NOT the edit target.

The edit target is **`ProjectSettings`** (`mewbo_graph.wiki.types`): one slug-keyed record
holding the `WizardSubmission` contract minus the token, plus a `desc` override.
`WikiIndexingJob.refresh` consults it FIRST, which is the only reason an edit takes effect.
Distinct from the job-keyed **submission sidecar**, which is immutable history of what ONE job
ran with.

- **Resolution ladder — ONE definition, walked by both `refresh` and the façade** so the UI
  cannot show settings a refresh would not use: settings record → newest job sidecar
  (`jobs._latest_job_submission`) → `Project` fields (`jobs.submission_from_project`). A project
  indexed before the settings record existed has none; the first PATCH materialises one from that
  ladder.
- **Sort attempt history by `phase_started_at`, NEVER by `job_id`.** `job_id` is a `uuid4` hex
  and sorts RANDOMLY, so a `key=lambda j: j.job_id` picks an arbitrary job as "latest".
  `IndexingJob` has no `created_at`; `phase_started_at` is ISO-8601, so lexicographic ==
  chronological. The freshness baseline shares this trap.
- **`start()` re-seeds the record on every first index AND every refresh** (refresh re-enters
  `start`), so it MERGES: an existing `desc` override is carried forward, not clobbered by a
  submission that never had one.
- **finalize read-preserve.** `finalize._resolve_project_desc` is THE seam shared by
  `wiki_finalize` and `GraphOnlyIndexer`: user override → platform-API fetch → previous record's
  `desc`. Without it a rebuilt `Project` overwrites an edited description. `model` needs no such
  seam — it is not a `Project` field at all.
- **`update_project(slug, fields)`** is a partial upsert whitelisted to
  `WikiStoreBase.PROJECT_UPDATABLE = {"desc", "resolution"}`; every other field is rebuilt by the
  next index or is identity. `desc` is written to BOTH surfaces — snapshot (console updates now)
  and settings override (next index re-applies it). `resolution` is NOT user-editable; it is
  listed only because an INDEX's write patches an existing row.
- **The two update verbs invert one convention:** on `update_project` a `None` means "not
  supplied" and is ignored; on `update_job` a named `None` is a value to be WRITTEN.

**`Project.resolution`** records whether the stored graph's cross-file edges were resolved exactly
or guessed by name — unanswerable from the graph itself, since an unresolved graph is fully
populated, passes validation and renders. The model, its `faithful`/`degraded` properties and the
two-writer read-preserve seam are owned by `packages/mewbo_graph/CLAUDE.md`. Api-side: it rides
`GET /v1/wiki/projects` for free (that route dumps `Project` with `by_alias=True` and no
`exclude_none`, so the key is always present or explicit `null`), **`None` reads as UNKNOWN and
never as a healthy pass**, and the console carries no `resolution` field yet — the addition is
additive, so do not write against a console type that does not exist.

**Settings routes** (`settings.py` — `WikiProjectSettings`, a DI'd atomic class whose
`WikiHTTPError` raises map through the registered handler; the Blueprint handlers are thin
adapters):

- `GET /v1/wiki/projects/<slug>/settings` → effective settings + `credential` presence (**scope
  + scopeType only, never a value**, via the ONE durable-tier walk
  `jobs._durable_credential_scope`) + a camelCase `editable` map. Catalog projects get a reduced
  `kind:"catalog"` shape.
- `PATCH /v1/wiki/projects/<slug>` → 200 / 400 / 403 / 404 / 409 / 410.
  - **`extra="forbid"` on `ProjectSettingsPatch` is load-bearing**: a `token`, a `slug` rename,
    or a system-owned field is a 400, not a silent no-op. Credentials go through the ONE registry
    at `/v1/git/credentials/<scope>`.
  - **Dev-mode RE-GATE on `graph_only` (403).** The index-route gate plus sticky mode means a
    blind PATCH would hand an unprivileged caller the zero-LLM path `POST /index` refuses them.
    Turning it OFF is not privileged.
  - **Repo-identity guard (409).** The slug keys pages, graph, jobs, credentials and freshness,
    so a bare URL swap re-points the next clone while leaving all of them pinned to the old repo.
    Identity is compared through `CredentialScope.from_repo_url`, which normalises (host
    lowercased, `.git`/trailing slash stripped), so a same-repo re-normalisation passes and only
    a real (host, owner, repo) change is refused. `platform` alone is not identity.
  - **Omitted ≠ null.** `model_fields_set` (not an `is not None` filter) decides what was sent,
    so `{"ref": null}` unpins a branch while an absent `ref` persists.
  - **A PATCH never starts a re-index** — everything but `desc` takes effect at the next one,
    which is also why it cannot bypass the per-IP indexing rate limiter.

**Not on the RESTX spec.** `/v1/wiki/*` is a plain Flask Blueprint contributing ZERO paths to
`docs/openapi.json` (verified), so the Flask-RESTX `example=`/Scalar convention does not reach
this surface. A wiki route documents itself in its docstring.

## Non-git catalog ingestion

`CatalogIngestor` (`mewbo_graph.wiki.catalog`) writes pages + a content-addressed graph node
directly — no agent, no tree-sitter. Api-side rule: **refresh REJECTS catalog projects**
(`repo_url is None` AND no git submission), and catalog nodes reuse `type=File` with a
`file="catalog/"` prefix, which is what `doc_total` counts by.

## SSE plumbing — proxy buffer + resume

`events.py:_SSE_PRIMER` is a 2 KB padded comment frame emitted once at stream start:
OpenResty/NPM and similar HTTP/2 proxies buffer responses up to ~4 KB by default, so the first
real events never reach the browser until the buffer fills or the connection closes.
`_heartbeat_frame()` is padded for the same reason. Do NOT shrink either "to save bytes".

`_to_sse` emits `id: <idx>\nevent: <type>\ndata: <json>\n\n`. The `id:` line is mandatory: a
native `EventSource` sends the last id back as `Last-Event-ID` on auto-reconnect and the route
honours that header, so a proxy dropping mid-stream resumes without replaying the transcript.
The current FE uses `fetch` (to send `X-Api-Key`), so only the server-side resume path uses it
today — keep emitting it.

## Git credential resolution (security-sensitive)

A submitted token or SSH key MUST NOT land in the persisted submission, the session transcript, or
any event log — sessions are visible in Langfuse/Mongo and the transcript is treated as
semi-public.

**The database and the ambient git credential are the ONLY two sources of truth.** There is
deliberately no in-process token cache: a third source drifts, and a revoked stored token then
permanently shadows a still-valid ambient credential with no fallback.

- `RepoCredential` carries NO scope field: the scope is the store KEY, stamped into the blob at
  save, so there is one binding rather than two that can disagree. `value` is stripped at
  definition — a PAT pasted with a trailing newline silently 401s.
- `CredentialStore` (`mewbo_graph.wiki.credentials`) is the single read/write chokepoint, keyed by a
  validated **`CredentialScope`**, never a bare `str`: a full slug (`host/owner/repo`) or a bare
  host, shared by every repo on that host. `CredentialScope.covers()` IS the host-covers-repo rule
  and `.kind` (`host|repo`) is what the `scopeType` wire field mirrors, so the BE has ONE definition
  and the FE (`api/git.ts`) mirrors it. Plaintext at rest behind an identity `_encode`/`_decode`
  seam — encryption is a one-line swap there.
- **NEVER log `RepoCredential.value`** or echo it into an SSE event, transcript, or tool result.

**`resolve_chain(store, slug, *, arg_token=None)` is the ONE canonical order**, used identically
by clone, branch listing, freshness and the description fetch:

1. `arg` — an explicit override (the wizard testing a not-yet-saved token).
2. `store:repo` → 3. `store:host` — the durable credentials.
4. `ambient` — `git credential fill` (read-only, `GIT_TERMINAL_PROMPT=0`, 10 s timeout).
5. `anonymous` — always last.

Consumers advance on an **auth-class failure only** (`is_auth_failure(stderr)`, the ONE
classifier). Its markers are ANCHORED to real git/HTTP auth text (`error: 403`, `http 401`),
never a bare `401`/`403` substring, which misreads `port 8403: Connection refused` as a
rejection. A non-auth failure propagates immediately; it will not succeed on retry with a
different credential.

**Every git subprocess runs through the hardened executor in
`mewbo_graph.plugins.wiki.clone`** (`run_git_with_chain` plus the shared `build_clone_command` /
`build_ls_remote_command` / `hardened_git_env`): clone, `ls-remote`/branches, freshness, and the
credential-validate route. That is what disables git's OWN credential helper
(`-c credential.helper=`) and prompting everywhere. The reason: the api container mounts
`~/.git-credentials` READ-ONLY, so git erasing a rejected entry fails with `Device or resource
busy` — and that EBUSY MASKS the real auth error, killing an otherwise fine clone. We read the
ambient credential ourselves and inject it into the URL so git never touches the mounted file. A
hand-assembled argv reintroduces the bug — see `mewbo_graph/CLAUDE.md` → "Git auth", including
why a git success does NOT prove a credential is valid.

Token → URL injection (`x-access-token:<token>@host`, or a stored `username`). SSH key → temp
file (`0600`) + `GIT_SSH_COMMAND="ssh -i <tmp> -o StrictHostKeyChecking=accept-new"`, deleted in
a `finally`.

`jobs.start` saves the credential durably BEFORE stripping the token from the persisted
submission. `jobs.refresh` does **no restore step** — the clone tool's own `resolve_chain` reads
the durable credential at clone time. So `_render_user_query`'s auth note derives from
`_durable_credential_present(store, slug)`, NEVER from `submission.token`, which refresh no
longer carries; reading it made every refresh of a private repo render "public repo assumed".
Finalize's description fetch and the freshness compare re-walk the chain through
`_platform_api.api_get_json_with_chain`, because a public repo clones fine with a revoked token
and no REST caller may trust the clone's winner. Project-delete removes ONLY the exact
repo-scoped credential; a host-scoped one is shared and must never cascade.

## `/v1/git/credentials*` — the product-wide registry

`git_credentials_routes.py` mounts this OUTSIDE `/v1/wiki/*`, even though it registers from the
same `init_wiki` and reads the same `WikiStoreBase` surface. Wiki is the first consumer;
task/vcs-pickup flows are expected next.

- **Every route validates its `<path:scope>` through `CredentialScope` first** — a malformed
  scope is a clean 400 `validation` AT THE BOUNDARY. Accepted, it is written under a key the
  resolution chain can never look up and surfaces later only as an opaque "the clone fell back
  to anonymous".
- **No route ever returns a `value`** — a listing carries `valueHint` (`"…" + value[-4:]` for a
  token, `"ssh key"` for a key). The write path is the only direction a secret travels, and its
  body is `CredentialUpsert` (`extra="forbid"`, so smuggling a `scope`/`updatedAt` is a 400)
  validating into `RepoCredential` before `CredentialStore.save`.
- `POST .../<path:scope>/validate` runs ONE `git ls-remote` with the stored credential injected,
  built from the SAME builders the clone uses (20 s cap), threading the credential's own `username`
  through `_inject_token` exactly as the clone chain does. A GitLab `oauth2`/deploy-token credential
  validated under a hardcoded `x-access-token` would authenticate differently here than in the clone
  that follows, which is worse than not validating. `repoUrl` defaults to `https://<scope>` for a
  repo scope and is REQUIRED (400) for a host scope. **An `ssh_key` credential requires an SSH-form
  `repoUrl`** and returns `ok:false` with an explanatory `detail` otherwise: an `https://` URL
  ignores `GIT_SSH_COMMAND` entirely, so probing one runs an ANONYMOUS ls-remote and hands back a
  verdict that says nothing about the key. `detail` is scrubbed through `clone._redact`.

## The refresh path — one decision, three consumers, two runners

`POST /v1/wiki/projects/<slug>/refresh` does not always rebuild. The knob is an optional body
`{"mode": "auto"|"full"}` (absent means `auto`); the outcome is a `RefreshDecision`.

**`mode` and `path` are deliberately different vocabularies.** `RefreshMode` (`auto|full`) is what a
caller may ASK for; `RefreshPath` (`scoped|full`) is what was CHOSEN. Collapsing them lets a request
value be stored as an outcome, and `auto` is not something a job can have run. There is no
`"scoped"` mode: a caller cannot demand a scoped refresh of a project whose artifacts nothing
fingerprinted, so the only honest knob is "try" or "don't".

**`RefreshDecision.decide` is PURE** — project record and fingerprint in, verdict out; no store,
clock or subprocess, with every probe resolved at the edge in `jobs.refresh` and handed in, which is
what makes the policy testable with no repository on disk. Order is cheapest-and-most-decisive
first, so the reason a reader sees is the one that would still hold if everything after it were
fixed: `requested` → `graph_only` → `no_prior_index` → the `FingerprintDecision`
(`fingerprint_unknown` ≠ `fingerprint_mismatch`, the three-state honesty `RepoFreshness.check` also
enforces). A validator pins `reason is None` IFF `path == "scoped"`, so a consumer checks one field
rather than two that could contradict. **It is deliberately NOT wrapped in a try/except degrading
to a full rebuild:** `RefreshFullReason` has no member for "the probe itself failed", so a fallback
could only report a reason that is not the true one.

**One record, three consumers**: the route response, `IndexingJob.refresh_decision` (what the
console renders), and the resume branch. Stamped once at job creation and never rewritten, which
lets it survive a restart without a second sticky field on `WizardSubmission`.
`refresh_decision=None` is a THIRD state, not a synonym for full.

- **`full`** re-enters `WikiIndexingJob.start`, byte-for-byte the standard path.
- **`scoped`** drives `ScopedRefreshRunner` on a daemon thread with NO Mewbo session. **Only
  `full` may reach `_start_indexer_session`** — a scoped refresh that quietly minted an indexer
  session would cost exactly what it exists to avoid; a test asserts that.

Both sessionless runners share `JoblessIndexRunner` (`plugins/wiki/_jobless.py`): clone, scan,
cooperative cancel-polling, terminal-failure recording, and the
never-raise-into-the-daemon-thread driver. A subclass declares its pipeline as data via
`_phases()`. A scoped refresh reuses the EXISTING `clone/scan/graph/finalize` vocabulary (the
whole delta pass runs inside `graph` — it IS a graph re-index, scoped), so the console phase map,
the progress bar and `PHASE_SEQUENCE` need no special case.

**A scoped finalize must never call `supersede_graph_artifacts`.** That reaper deletes every
artifact for the slug NOT stamped with the kept commit. A FULL index re-stamps the whole graph,
so both full finalizers call it correctly; a SCOPED refresh re-stamps only the files it
re-parsed, and every untouched file keeps the commit of the index that built it. Calling it after
a scoped pass deletes the untouched majority of the graph — silently, because a dangling read is
dropped rather than raised. The multi-commit union this leaves is the intended steady state: a
row's commit stamp records which index last touched that file, and the next full rebuild
re-stamps and reaps the lot.

For the same reason the scoped finalize does a PARTIAL `Project` update
(`commit_sha`/`commit_short`/`indexed_at` only): it regenerated no pages, so `pages`, `desc`,
`landing_page_id` and `fingerprint` all still describe reality, and the fingerprint is equal by
construction (any mismatch forces the full path).

**The two transports spell a scoped `reason` differently, deliberately.**

| Surface | A scoped decision serialises as |
|---|---|
| `POST .../refresh` response | `{"path":"scoped","reason":null,"mismatches":[]}` |
| `IndexingJob` snapshot via `_job_wire` | `{"path":"scoped","mismatches":[]}` — key ABSENT |

`_job_wire` dumps `exclude_none=True` for the whole job and that recurses into nested models; the
route response does not. Do not reconcile them: the alternatives are flipping `exclude_none` for
the entire snapshot, reshaping every optional field on a wire two clients decode, or
special-casing one nested field inside a serialiser whose whole value is having ONE rule. It is
safe because `reason` is non-`None` only when `path == "full"`, where it is a real string in both;
the console types it `reason?: null` on the scoped arm to cover both.

**A scoped pass does NOT regenerate documentation.** The doc planner scores every page and
records the verdict; `RefreshReport.pages_to_regenerate` is the work-list a future gated
page-writer would act on. A page scored `edit` or `regenerate` is still the OLD page, and the
scope preview says so in numbers rather than the run pretending otherwise.

## Repository freshness — `GET /v1/wiki/projects/<slug>/freshness`

`RepoFreshness.check` compares the indexed commit against the remote HEAD via `git ls-remote`
plus a per-platform compare API over the same credential chain. Response `{indexedSha, remoteSha,
behindBy, upToDate, checkedAt}`; `behindBy`/`upToDate` are `None` when the compare could not run
— an honest "unknown", never a false "up to date" (the FE renders "Update available" with no
count).

- **Baseline sha** is `Project.commit_sha`, falling back for older projects to the latest
  `complete` job's commit ordered by `phase_started_at`, never `job_id`.
- **Cache**: in-module TTL dict, `_FRESHNESS_TTL_SECONDS` (300), keyed by slug. `?force=1`
  bypasses a cached READ and recomputes, still refreshing the entry. EVICTED on `refresh_project`
  (a "behind by N" badge against the commit being rebuilt is a lie) and on `delete_project` (so a
  re-created slug cannot inherit the dead one's badge). Negative results are cached like any other
  body, so an unreachable remote cannot re-block every request.
- **Served synchronously, so the worker class matters.** `--threads` is INERT on gunicorn's
  default *sync* worker, so a cold check (seconds on a slow remote) blocked the WHOLE API.
  `docker/Dockerfile.api` runs `-k gthread`; do not drop it "because threads are already set".

## Branch picker — `POST /v1/wiki/branches` + ref threading

`post_branches` resolves the remote's heads via the down-layer `RemoteBranchLister`
(`git ls-remote --symref`, host-agnostic). Credential resolution is the same `resolve_chain` but
**jobless**: before the first index there is no job_id/slug, so the route falls back to the URL's
bare HOST scope (`CredentialScope.from_repo_url(repo_url).host_scope()`). A non-auth failure
propagates as the standard `repo_access` envelope.

**An explicit body `token` is EXCLUSIVE — never part of that chain.** The wizard sends one only
when testing a not-yet-saved credential, so the route tries THAT token and nothing else: an
auth-class rejection returns `400 validation` (`fields: {token: "rejected"}`) instead of falling
through. If a stored/ambient/anonymous candidate could succeed behind a rejected typed token, the
wizard would report success and the index would then durably PERSIST the bad token.

The chosen branch is `WizardSubmission.ref`. `_render_user_query` emits a `ref:` line ONLY when
set, so an omitted ref keeps the rendered query byte-identical to default-branch behaviour and
the golden render tests do not churn. A RESUME pins the recorded `commit_sha` as the clone ref,
NOT the branch's latest HEAD.

**A pinned `commit_sha` cannot ride `git clone --branch <ref>`** — git resolves that flag's value
as a branch/tag name on the REMOTE, so a raw SHA always fails. The clone tool pins server-side:
`git init` + a depth-1 `fetch` of the exact object + `checkout FETCH_HEAD` (`clone_at_sha`). It
VERIFIES rather than overwrites — a checked-out HEAD disagreeing with the recorded `commit_sha`
is a `repo_access` failure, not a silent rewrite of the pin. Never have the model pass a `ref`
for a pinned resume job: the ref IS the pinned sha.

## Restart durability is checkpoint-aware resume

`JobRecovery` (`recovery.py`) finds recoverable jobs on startup (`IndexingJob.is_recoverable`),
marks the non-`interrupted` ones `interrupted`, and re-drives `WikiResume.resume` ONCE per
distinct slug; the durable credential authenticates the re-clone. `interrupted` is itself
recoverable: if the API died after marking a job interrupted but before re-driving it, the next
restart must still retry.

A SLUG-KEYED retry cap (`JobRecovery.MAX_RETRIES`) lives on its own persistent surface
(`store.{get,bump,reset}_recovery_attempts` — `recovery/<slug>.json` / `wiki_recovery`, NOT the
submission sidecar) so it bounds AUTOMATIC re-drives across recovery generations and new job_ids.
On exhaustion recovery moves the job to terminal `failed` (`_mark_failed`) rather than leaving it
`interrupted` forever: the FE suppresses a completed tile while its slug has any active job, so a
permanent `interrupted` makes a finished project read "Indexing now" indefinitely.

**Resume reuses the SAME job_id** (continuous event log), re-clones at the recorded `commit_sha` —
NOT latest HEAD, so the reused graph stays consistent; re-indexing at HEAD is the distinct
`/refresh` path — and SKIPS the expensive idempotent phases whose artifacts exist.

- The decision is the atomic `ResumePlan` (`mewbo_graph.wiki.resume`): `build(store, job)` computes
  `skip ⊆ {graph, enrich, plan}` (graph non-empty → skip graph; entities exist → skip enrich;
  committed plan → skip plan) plus `pages_done`/`pages_remaining`. `clone`/`scan` ALWAYS run (cheap,
  and page-writers need the source on disk); `finalize` always runs (idempotent). It is computed
  ONCE at resume time and persisted via `store.save_resume_plan`, then rebuilt cheaply per tool call
  by `resolve_job_ctx` (`ResumePlan.from_persisted` — a tiny dict, no graph re-query). Phase tools
  consult it with a one-line `ctx.resume_plan.should_skip(...)` guard and short-circuit, still
  emitting the phase and returning a cached summary; `mint_entity` carries the same guard for the
  tool-less enrich fan-out. **Done-detection lives ONLY in `ResumePlan`.**
- **`ResumePlan`'s artifact counts fail CLOSED**: the shared read helper RAISES rather than
  returning 0 on a store exception, because one transient Mongo hiccup silently selecting a full
  rebuild is the failure it prevents. The sharp edge: a refusal to resume must NOT consume the retry
  budget, or fail-closed trades a silent full rebuild for a silent PERMANENT failure after a few
  glitches. `JobRecovery.recover_interrupted` catches that raise specifically and skips
  `_bump_attempts` for THAT cause only.
- **Per-job artifact attribution makes "the graph for THIS commit" expressible.** Graph
  nodes/edges/embeddings and the entity family carry `commit_sha`/`job_id`; pages get the same
  pair as a store-side attribution sidecar (keyed alongside the page, not a `WikiPage` field) so
  the console's wire type stays byte-identical. The commit-scoped counts `ResumePlan` queries and
  the supersede logic both key off exactly these fields — without them the store is the union of
  every index ever run for a slug.
- User-initiated resume (`POST /v1/wiki/index/<job_id>/resume`) resets the per-slug cap; the
  automatic path keeps it. An optional `{"restart": true}` body forces a full rebuild while
  reusing the job_id. `GET /v1/wiki/jobs/recoverable` lists non-complete jobs whose `ResumePlan`
  has reusable work. `submit_page` is idempotent, so a re-submitted done page is harmless.
- **A `cancelled` job is NOT resumable, and neither is a `complete` one**
  (`_NON_RESUMABLE_STATUSES`). Cancelling is the escape hatch for a wedged job and it is one-way:
  the next index for that slug is a fresh job.

## Four lifecycle questions, one home — the `IndexingJob` predicates

`is_active` / `is_recoverable` / `is_terminal` / `is_resumable` live ON `IndexingJob`. They are
**four genuinely different questions and must stay four sets** — merging them is a behaviour
change, not a cleanup. A route that re-spells one is the drift they exist to prevent: a recovery
surface must offer exactly what the resume guard accepts, which is guaranteed only by calling the
same predicate.

| Predicate | Asked by | `interrupted` | `failed` | `cancelled` |
|---|---|---|---|---|
| `is_active` | `/jobs/active`, the `isActive` flag | live | dead | dead |
| `is_recoverable` | `JobRecovery` on boot | re-drive | leave | leave |
| `is_terminal` | `WikiIndexingSessionEndHook` | not settled | not settled | settled |
| `is_resumable` | `WikiResume`, `/recover` dispatch | resume | resume | refuse |

The disagreements are the point. `failed` is NOT `is_recoverable` (an automatic re-drive of a job
that exhausted its budget is how a dying index loops the API) yet IS `is_resumable` (a human may
still ask, and `ResumePlan` decides what can be reused). And `failed` is NOT `is_terminal`,
because a session ending cleanly on a failed job is a mismatch worth asserting — that predicate
answers "may I leave this alone", not "will this ever run again".

Plain `@property`, not `computed_field`, for the `extra="forbid"` round-trip reason above.

## Reconcilers — session end, terminate, honest terminal state

Job status is advanced only by the tools the indexer calls (`clone`→`scanning`,
`commit_plan`→`finalizing`, `finalize`→`complete`), so a session ending WITHOUT reaching
`wiki_finalize` leaves the job non-terminal. Two hooks reconcile:

- **`WikiIndexingSessionEndHook`** (registered in `routes.register()` beside `QaSessionEndHook`)
  marks a non-`is_terminal` job `interrupted`, handing off to `JobRecovery` on the next restart. It
  catches tool-internal infra failures — a network/IO/timeout inside a phase tool where the LLM
  catches the error, reports it, and exits cleanly with `done_reason="completed"` plus an error
  field — which would otherwise leave the job in its last phase status forever, invisible to
  recovery. On the happy path `wiki_finalize` already set `complete`, so it no-ops.
- **A TERMINATE is not a session end and needs its own cascade.** `WikiJobTerminationCascade`
  rides `SessionRuntime.register_on_terminate`. Without it a terminated indexer's job stays
  `is_active` and `is_recoverable`, so the next boot restarts the run the user stopped. The two
  callbacks carry OPPOSITE intents and cannot share a registration — session-end is a HANDOFF
  (`interrupted`), terminate is irreversible (`cancelled`, out of both sets). Three constraints:
  - It touches a job only while `is_active`. `store.cancel_job` guards nothing but a second
    cancel, so an unguarded call would rewrite a `complete` index.
  - It returns `None`, never a count — `terminate_session` sums an int return into
    `cancelled_triggers`, which belongs to the trigger store.
  - It appends its `log` explanation BEFORE `cancel_job`: the SSE stream closes on the `cancelled`
    event, so anything written after never reaches a watching client.
- **Every indexing start path must thread the real `hook_manager`** — `post_index`,
  `refresh_project`, `resume_index`, and `JobRecovery` via `init_wiki` → `_run_recovery`.
  `hook_manager=None` is not "no hooks": `Orchestrator.__init__` swaps in a FRESH EMPTY
  `HookManager`, so a start path that omits it produces sessions for which the end hook can never
  fire, and a job dying mid-phase there is never handed to recovery at all.
- **Supersede at finalize** (`finalize.py:_supersede_stale_jobs`): a `complete` index marks every
  *other* NON-TERMINAL job for the same slug `failed`, so older stuck attempts drop out of
  `/jobs/active`. It rewrites only non-terminal siblings, so a manually `cancelled` job stays
  cancelled.
- **Completion correctness** (`finalize.py:_graph_is_populated`): finalize REFUSES to mark
  `complete` when the graph is empty ("completed without creating the graph" ⇒ `failed`, code
  `validation`) — soft-gated so a graph-less install is not blocked. "Error AFTER the graph was
  built" stays a distinct `failed`-with-populated-graph state.

**Order attempt listings by the authority, never by `job_id`.** A "still worth resuming" listing
must also ask whether a LATER attempt already succeeded: `Project.commit_sha` IS the current
indexed state of a slug, while `store.list_jobs()` is a log of every attempt ever made. A surface
deciding currency by scanning the log disagrees with the authority the moment an old attempt
outlives its usefulness without being deleted.

## Terminal accept-state for indexing

`wiki_finalize` is a **terminal `SessionTool`** (overrides `should_terminate_run()` /
`terminal_reason()`). On a successful `handle()` it sets `_terminate_run_pending`; the loop polls
it and breaks immediately with `done_reason="completed"` and **no extra post-finalize LLM turn**.
Without it the loop takes one more turn so the model can produce exit text — ~100K tokens per
successful index, plus a window where a stuck child wedges `asyncio.run(loop.run(...))` before
terminal events are written. Mirror this for any new wiki tool that signals end-of-index; the base
`WikiSessionTool.should_terminate_run()` returns `False`.

## Q&A — model default, terminal status, replay

`post_qa` makes `model` genuinely optional via `_resolve_qa_model()`, the one helper for the
`wiki.default_qa_model → wiki.default_model → llm.default_model` chain, reused by
`get_meta`/`get_wiki_defaults`/`_build_condense_model`. The route accepts `project` OR `slug` in
the body but reports validation against the PUBLIC `project` name.

`QaAnswer.status` (default `"running"`, `QA_TERMINAL_STATUSES = {complete, cancelled, error}`) is
the terminal flag a NON-streaming consumer needs — the MCP `ask_wiki` poll over
`GET /v1/wiki/qa/<id>`; the SSE stream already had its `complete` event. It is set at each accept
state — the terminal `wiki_emit_answer` (`QaFinalizer.close` → `complete`, or `error` on a
zero-block close) and `WikiQaSession.cancel` → `cancelled` — always through
`store.save_qa(answer)` so both backends round-trip it. **Any NEW QA terminal path MUST set the
status**, or a snapshot poller waits out its timeout.

That snapshot is also the **idempotent-replay source for the console `?answer=<id>` URL**: a
completed answer is fully reconstructable (blocks + cited + accessed + models), so a refresh or
share replays it with zero LLM.

**Grounded-structured slug resolution.** `resolve_qa_ctx` falls back to the
`structured_workspace` context event → a slug-only `WikiQaCtx` (`answer_id` Optional — retrieval
tools need only `slug`; emit/QA tools guard on `answer_id`). The session store is reached via a
**process singleton in `_ctx`**, NEVER `create_session_store()` per tool call, which leaked a
Mongo connection pool and added per-call latency against the sub-1.5 s budget.

## Q&A architecture — probe fan-out + terminal submission

`wiki-qa` is a **hypervisor, not a flat retrieval agent**: the root decomposes the question, fans
out `wiki-qa-probe` sub-agents through the existing `spawn_agent`/`check_agents` hypervisor (NO new
control loop), fuses their findings, and emits one cited answer.

- **The root has NO retrieval tools in its allowlist** (`QA_TOOLS` = list_pages / emit / insight /
  spawn / check). That is what FORCES delegation; handing the root the retrieval surface regresses
  it to read-one-page-and-stop. Structurally enforced, not merely prompted:
  `SessionToolRegistry.build_for` treats a non-empty `allowed_tools` as a ceiling over the
  capability gate too, so the root cannot bind `wiki_read_page`/`wiki_query_graph` despite holding
  the `wiki` capability. An empty/`None` `allowed_tools` still auto-surfaces a gated tool — that
  exception preserves the ordinary session surface. `strict_tool_scope=True` narrows the
  *stateless* surface (shell/edit) on top.
- **The run is read-only + self-approving (`approval_callback=auto_approve`)** with no
  hand-maintained admit-list: probes need a wide, evolving retrieval surface called freely
  turn-to-turn, and a restrictive callback turns each legitimate read-only call into an unanswerable
  ASK in a headless flow — the `awaiting_approval` stall.
- **ONE atomic `wiki_emit_answer` call IS the accept state** (`EmitStructuredResponseTool`
  pattern): the whole answer arrives as a single `{blocks: [...]}` call — every block
  schema-validated, positional errors riding the tool feedback loop, exactly one `sources` block
  required LAST — and the tool fans the array into per-block `block_open`/`block_close` events
  server-side, drives `QaFinalizer.close`, and sets `should_terminate_run()`. **Align a tool
  contract to the composition grain rather than stacking prompt guards:** a per-block
  `wiki_emit_block × N` choreography fights how models compose (whole answer in one pass), and a
  weaker model narrates the sequence as plain TEXT and delivers nothing.
- **`WikiEmitAnswerTool.required_terminal = True` + `terminal_satisfied()` gate the
  natural-completion branch** (`if not response.tool_calls:`), which is otherwise blind to an unmet
  terminal-tool obligation: a model narrating the emit call as text stamps
  `done_reason="completed"` with the reply discarded and the `QaAnswer` stranded at
  `status:"running"` forever. The loop refuses the clean terminal while the obligation is unmet,
  nudges in-band (bounded), and on exhaustion stamps the honest `done_reason="unmet_goal"`.
- **`QaSessionEndHook` is the NET and does NOT re-drive** — a re-drive dispatched from inside the
  run's own `finally` can never start, because the `RunRegistry` slot is still held. It settles the
  answer via `QaFinalizer.close` (a still-empty close is an honest `error`, never an empty
  `complete`) and reports an `OutcomeAssertion` when that close was a genuine surprise.
- **`QaFinalizer` lives DOWN in `mewbo_graph.wiki.qa`** beside `QaAnswer` and the store, so both the
  terminal emit (same layer) and the API net (imports down) call it. It rebuilds `blocks` +
  `summary_sources` from the append-only log; without that a reloaded or shared answer comes back
  empty and the SSE stream ends only by idle timeout.

### Citations

`summary_sources` is the LLM's curated sources block. `accessed_sources` is the retrieval trail,
and it is **bounded + score-ranked, never every node a probe touched**: each retrieval tool records
a `QaAccessRecord {ref, score, rank, tool, op, ok}` via `WikiSessionTool._record_qa_access`,
graph-NAVIGATION tools record only their seed (navigation ≠ grounding), ranked search tools record
only hits clearing a score floor (default 0.5× the top hit), and
`QaFinalizer._accessed_from_events` folds → dedupe-by-ref (best score) → score-desc → top-N cap
(`MEWBO_WIKI_QA_ACCESS_TOPN`, default 12). An unranked dump produces ~200 sources on a real run.
`HybridRetriever` stays the one ranker. `QaFinalizer._summary_sources` then folds the non-page refs
off that bounded trail into the cited set — curated pages first, then the files/symbols the answer
grounded on — because files are the most-read source while the curated block is almost entirely
page slugs.

- **Page citations are re-schemed at the EMIT seam, by id OR title.** A cited wiki PAGE would
  otherwise be treated as a source FILE and 404 on `/source`.
  `QaFinalizer.tag_page_citations` turns a bare ref into `wiki:<page-id>` when its slugified form
  matches a page id **or a slugified page TITLE**, since the model frequently emits the human title.
  Match reuses the core `_slugify` as a symmetric normalizer; the scheme guard is anchored to the
  closed `wiki|graph|src|entity` set so a colon-bearing title still matches. One seam fixes LIVE
  stream + snapshot together.
- **Graph hashes are resolved at READ and NON-destructively.** `accessed_sources` stores
  `graph:<node_id>` (content-addressed sha1, opaque in the panel) and `GET /v1/wiki/qa/<id>`
  humanises via `AccessedSourceResolver.resolve_refs` (AST node → `file#Symbol`, entity →
  `name (type)`, miss → `unknown (<hash[:8]>)`). The stored snapshot keeps raw ids because
  `QaMemoryDepositor` anchors off them, and resolving at read stays current across a re-index. The
  FE wire stays `string[]`.

**Answer depth is prompt-controlled** — `QaFinalizer` never truncates. Two guards live in
`wiki-qa.md` / `wiki-qa-probe.md`. A **structured-output floor scoped to ARCHITECTURAL questions**
(a lead `p` direct answer + ≥1 `h2` facet + ≥2 supporting `p`, then `sources`): without it the model
reads "quick + authoritative" as "be brief" and one-paragraphs an architectural answer, while a
narrow question answers concisely and must NOT be padded — **"quick" means LATENCY, never
brevity**, and the probe contract stays un-capped because a terse-claim cap starves the fused
answer. And **inline citations are `src:` links** (`[path:line](src:path#L<a>-<b>)`), so probes MUST
pass `start_line`/`end_line` to `wiki_read_file` or the citation cannot open the right lines. Only
emit kinds in the `types.py` block union — there is **no `ol`**; express ordered lists as markdown
prefixes inside a `ul`/`p`.

`GET /v1/wiki/projects/<slug>/source?path=&start=&end=` backs the cited-source cards: the console
fetches each `path#L<a>-<b>` excerpt LAZILY per card, because excerpts are deliberately NOT on the
SSE wire — that keeps stream payloads small and the `sources` block shape unchanged. It reuses
`WikiSourceAccess._safe_path` (the load-bearing traversal guard — absolute/`..`/symlink escapes
403) and `resolve_qa_clone_dir`; whole-file reads cap at `_SOURCE_MAX_LINES` while `totalLines`
still reports the true count.

### Q&A follow-up continuation

`WikiQaSession.follow_up(answer_id, question, *, runtime, hook_manager=None)` drives the SAME
engine primitive the generic continuation path uses: `runtime.start_async` re-engaging an EXISTING
`session_id`. It snapshots the just-finished turn into `QaAnswer.turns` (oldest-first), resets the
top-level fields, and re-drives with `QA_TOOLS` / `strict_tool_scope=True` / the wiki-qa playbook
referenced from the SAME module constants. **No new `answer_id` is minted.**

- **The QA scope is first-class persisted session state, so the GENERIC re-engage path honours it
  too.** `WikiQaSession.start` writes `mcp_tools` (=`QA_TOOLS`), `strict_tool_scope`,
  `session_step_budget` and `skill_instructions` into the SAME context event that advertises the
  `wiki` capability; `backend.py`'s `_extract_*` helpers read them back generically at BOTH
  re-engage sites (`/message`, `/recover`), so a QA session continued through the generic endpoint
  keeps its narrow scope instead of silently widening to the unscoped default. Every extractor is
  generic — no QA-specific branch in `backend.py` — so a future session type gets the same fidelity
  by persisting the same context keys.
- **`POST /v1/wiki/qa` accepts an optional `answerId`**: present ⇒ continuation (only `question` +
  `answerId` required); absent ⇒ a fresh `answer_id`. Unknown → `404 not_found`; a session already
  mid-run → `400 validation`, since this route has no 409 code. The `meta` SSE event carries
  `sessionId` so a continuation is addressable.
- **`QaAnswer.turns: list[QaTurn]`** is the additive, oldest-first history of PRIOR completed turns;
  the top-level fields keep describing the LATEST turn only, so MCP `ask_wiki`/`get_wiki_answer` and
  a plain `GET /v1/wiki/qa/<id>` stay byte-compatible. MCP is deliberately single-shot: a caller
  reads `turns` but cannot drive a new turn.
- **`QaFinalizer.current_turn_events` is THE turn-boundary rule; never re-derive it at a call
  site.** A follow-up's `wiki_emit_answer` restarts block indices at 0, so an unscoped fold collides
  a later turn's index-0 block with an earlier turn's in `_blocks_from_events`'s index-keyed dict
  and corrupts both. A turn opens **two** ways, and both arms are load-bearing:
  - a `meta` event — a NEW question;
  - a `block_open` landing AFTER a terminal, re-opening from just past it. Needed because a
    **recovery re-drive never re-emits `meta`**: it re-engages the existing run, so the slice still
    carries the previous run's terminal `error`, `close()`'s idempotency guard matches, and a
    correct answer is written to the log and thrown away — every recovery of a failed answer is
    then structurally unable to succeed.

  A terminal ALONE does not open a turn; the boundary moves only once a `block_open` follows one, so
  a straggler `access` after a clean `complete` cannot form an empty turn that `close()`'s
  empty-success refusal would re-stamp as an error. It is a `@classmethod` so `wiki_emit_answer`'s
  atomicity guard scopes to the SAME call instead of the cumulative log, where a follow-up seeing
  turn 1's `block_open` is refused as "already emitted". `qa_sweep.py` is the third consumer.
  `models_used` (`QaFinalizer.enrich`) stays session-WIDE: it answers "which models ran at all",
  not per-turn content.
- **No hardcoded fan-out and no turn-history cap:** the prior turn's findings are already in the
  hypervisor's carried-over context, so it decides whether fresh probes are worth spawning, and
  context growth relies on the session's existing compaction.

## Embeddings — LiteLLM, not LangChain

`embedder.py` wraps `litellm.embedding(...)`; we do NOT depend on `langchain-openai`, whose 1.x
requires `openai>=2.26.0` while `litellm` pins `openai==2.24.0` exactly — installing both leaves
one broken at import.

- The configured `wiki.embedding.model` MUST resolve to a name LiteLLM routes through our
  OpenAI-compatible proxy. Bare names like `gemini-embedding-001` dispatch to a provider SDK and
  bypass the proxy, so `Embedder` prepends `openai/` to any model name without a `/` — mirroring
  `LLMConfig.proxy_model_prefix`.
- **Embedding failure is non-fatal** — `build_graph.py` catches it, warns, and falls back to
  BM25-only retrieval. Don't make it a hard fail; some operators run against proxies exposing no
  embedding model and the wiki still produces useful pages.
- **Indexing pays embedding cost as bounded-concurrent, and `batch_size` is not the lever.**
  Per-connection throughput is roughly flat near 50 texts/s regardless of batch size, so raising it
  only trades request count for per-call latency; a serial loop over batches is pure socket wait
  (505 sequential round-trips on a 32,262-node repo ≈ 18 minutes at ~0.55% CPU). The pacer contract
  (`wiki.embedding.concurrency`, retry/backoff, order-by-index reassembly) is owned by
  `packages/mewbo_graph/CLAUDE.md`.
- **Embedding widths are NOT interchangeable** — a store holding both returns wrong neighbours
  rather than erroring, which is why `IndexFingerprint.embedding_model` forces a full rebuild on a
  model change. The deployment default is `openai/text-embedding-3-small` (1536 dims): faster per
  connection and half the per-vector cost in `vector_search`'s `O(collection)` Python-side scan.

## Finalize housekeeping

**`prune_pages` — slug-drift dedup.** LLM stochasticity gives the same topic a slightly different
slug on each re-index, so without pruning every run accumulates duplicates. `finalize.py` calls
`ctx.store.prune_pages(slug, keep)` with `keep = {plan_page_ids} | {landing_page_id}`; Mongo
overrides with one `delete_many({slug, page_id: {$nin: list(keep)}})` for atomicity, JSON uses the
per-page loop. To keep a page not in the committed plan, add it to `keep` — never disable pruning.

**Description fetch + private TLDs.** `_fetch_description` calls the platform's public API, which
returns `""` on a token-less refresh against a private host — so finalize reads the existing record
and keeps its `desc` rather than blanking it. `_is_private_host` (from `clone.py`) is reused to
disable TLS verification for `.home`, `.local`, `.internal`, `.lan`, `.intranet`, `.corp`, the same
carve-out the clone uses; do not add other TLDs without a strong reason.

## KG endpoint — `GET /v1/wiki/projects/<slug>/graph`

The route is a thin adapter over `KnowledgeGraphView.for_slug(...).to_wire()`, which lives in
`mewbo_graph.wiki.graph`. **Its contract — commit scoping, the multiplex assembly of AST + entity
+ memory layers, `node_limit` covering view-synthesized `External` nodes, and the synthetic
`folder:__external__` bucket — is owned by `packages/mewbo_graph/CLAUDE.md`.** Read it there
before changing anything the payload's shape depends on. Two api-side consequences:

- **`stats.kinds` must tally EVERY node class on the wire**, not just the AST layer. The FE derives
  its whole legend from that map (a kind shows only when its tally is positive, and the sum decides
  which LAYERS to offer), so adding a node class to `to_wire` without adding it here draws those
  nodes with no legend row and no toggle — visible but unnameable and un-hideable.
- **`folder:__external__` is keyed by literal in the console** (`EXTERNAL_BUCKET_ID`), so the id is
  reserved and must stay byte-stable across builds.

## SessionRuntime session tags

| Surface | Tag |
|---|---|
| Indexer | `wiki:job:<job_id>` |
| Q&A | `wiki:qa:<answer_id>` |
| Scoped refresh, act stage | `wiki:act:<job_id>` |
| On-demand maintainer | `wiki:maintain:<slug>` |

`runtime.resolve_session(session_tag=...)` upserts the session and attaches the tag, so a process
restart can still find the running session if the SSE stream is reopened. Use the same tag in
producer (start) and consumer (resume) paths — a different tag in `routes.py` means the session
never reattaches.

**`min_parts` is a MINIMUM, not an arity.** The wiki row of `SessionTag._KINDS` requires three
segments to yield facets and indexes segment 2 for `wiki_id`, so a FOURTH segment parses
identically — same `session_type`, same `wiki_id` (a slug carries no `:`). An unknown sub-word is
the case that silently classifies as that row's `wiki_qa` default with nothing raised.

That is what `SessionTag.wiki_maintain_fresh(slug, session_id)` exploits.
`POST /v1/wiki/projects/<slug>/session` takes an optional `{"newSession": true}`
(`MaintainerSessionRequest`, `extra="forbid"`, camelCase like the rest of this Blueprint —
`populate_by_name` keeps `new_session` working) that ALWAYS mints, tagged
`wiki:maintain:<slug>:<session_id>` — the composer's shape, which asks to start a NEW
conversation and must never be handed the canonical maintainer's transcript to append to, while
the default get-or-create stays the project card's. The canonical three-segment tag STAYS on the
canonical session, because a second session taking it would steal it and revoke the first
session's write ctx. `WikiMaintainerSession._mint` is the ONE mint both paths share, so the
capability, tool ceiling, playbook and slug binding cannot drift between them; only the tag
differs, and it is derived after the create because the fresh variant is keyed by the session's
own id. Nothing in `mewbo_graph` changed: `maintainer_slug` already read the slug back through
`SessionTag.parse`.

**The maintain tag is an AUTHORIZATION, not a label — the tag authorizes a slug-bound ctx, and
the slug is only the address.** A session's context is writable by whoever drives it
(`backend.py`'s `_build_context_payload` merges a request's `context` verbatim), so a `slug`
context key proves only that someone asked; `resolve_job_ctx`'s project tier therefore requires
the TAG, which only `WikiMaintainerSession.open` stamps and only after the route validated that
slug against an indexed project. The slug is read back OUT of the tag for the other half of the
same reason: a tag cannot be re-pointed by a later turn, so a session authorized for one project
can never be re-addressed at another. **Do not read the tag check as redundant with the
project-row lookup sitting beside it** — the lookup validates the address, and only the tag
carries the permission. The `slug` context value is still written and still read; it is what
binds `SessionSpec.slug`, and nothing more.

## Multiplex memory + docs overlay

The memory + docs graph overlaid on the code graph (`memory.py`, `refresh.py`,
`structure_provider.py`, `retriever.py`) is substrate: identity (`entity_key = file#Qualified.Name`,
offset-free so anchors survive a re-index), content-addressed atomic notes, the three-tier dedup
ladder, invalidate-don't-delete, and the delta-refresh closure are all specified in
`packages/mewbo_graph/CLAUDE.md`. What belongs here is the api-side surface and one config trap:

- **One ingestor, three surfaces.** `InsightIngestor.from_store` backs the SessionTool
  `wiki_submit_insight`, REST `POST .../insights`, and MCP `submit_insight`. The in-session tool is
  deterministic; raw human text condenses on the REST/MCP path. **REST insights returns 200
  (`ok:false`), NOT 422**, on a fully-rejected well-formed request, so the MCP `RestClient` facade
  does not raise.
- **The QA→memory flywheel is off the latency path.** `QaMemoryDepositor` fires from
  `QaSessionEndHook` post-delivery, best-effort, idempotent, skipping an empty slug, and reuses
  `InsightIngestor.ingest(condense=True)` — no second fan-out, no new memory writer.
- **Naming a config type is not enough to say a knob is READ — name the construction site.** A knob
  whose constructor default equals its config default is INERT undetectably: a fresh install behaves
  exactly as the config claims, under a green suite. The seams: `wiki.refresh.*` thresholds are read
  ONCE in `RefreshOrchestrator.from_store` and passed as constructor args to `GraphDeltaIndexer` /
  `MemoryReconciler` / `DocStalenessPlanner`; `wiki.memory.*` at `InsightIngestor.from_store` and
  `MultiplexExpander.from_store`, which `HybridRetriever._fuse_memory` calls;
  `wiki.refresh.default_mode` at `_configured_refresh_mode` in `routes.py`, where `"incremental"`
  maps to `"auto"` because `auto` IS the incremental strategy and `RefreshMode` has no third member.
  The stages read NO config and stay pure DI, so an injected collaborator still overrides — config
  decides the DEFAULT, never whether. `max_insight_chars` carries `le=200`, refusing an
  over-ceiling value at the file rather than clamping it silently later.

## Testing notes

Tests live in `tests/wiki/`; mock at the I/O boundary only (`litellm.embedding` — NOT
`OpenAIEmbeddings`, that import is gone — and a JSON store fixture for store/route tests). A wiki
test must never spawn a real LLM or hit a real proxy.

## When in doubt

`apps/mewbo_console/src/components/wiki/README.md` (the FE-side spec) documents the wire shapes
verbatim and is the source of truth for the BE↔FE API contract.
