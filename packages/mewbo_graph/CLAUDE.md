> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [wiki/](src/mewbo_graph/wiki/CLAUDE.md) · [plugins/scg/](src/mewbo_graph/plugins/scg/CLAUDE.md)

# Mewbo Graph — Capability-Library Guidance

`packages/mewbo_graph/src/mewbo_graph/` — the optional knowledge-graph substrate shared by
**MewboWiki** and **Mewbo Search**. Imports **down** into `mewbo_core` + `pydantic` only.
`wiki/` holds the tree-sitter code graph, the multiplex memory layer, the dual JSON/Mongo
`WikiStoreBase`, the `Embedder` and the credential chain; `entities/` the abstract-entity
layer; `scg/` the Source Capability Graph; `plugins/{wiki,scg}/` the capability-gated
SessionTools + AgentDefs, which ship with the substrate they wrap so they import it down.

The indexing job lifecycle is `src/mewbo_graph/wiki/CLAUDE.md`; the SCG tools are
`plugins/scg/CLAUDE.md`; product-level wiki/search decisions live under `apps/mewbo_api/`.

## The API/library boundary

The api is a thin shell — routes, SSE framing, run/job lifecycle, persistence glue — and
never re-hosts an engine.

- Domain models travel with the store: `Project`, `WikiPage`, `IndexingJob` live in
  `wiki/types.py`. SSE *event* models stay in the api; they are transport.
- `ScgConfig` stays in the api. It reads `scg.enabled` and is consumed only by api glue, and
  keeping it there is what stops this library reaching for app config wiring.

### Down-only seams

The plugins sit below the api but need state it owns, so the api registers into seams
declared here: the wiki store singleton (`wiki/store.py`), `MapPhaseSink`
(`scg/map_phase.py`), `SearchLauncher` (`scg/search_launcher.py`), `ActLauncher`
(`plugins/wiki/act_launcher.py`), and `register_builtin_root` for plugin discovery. Never
invert one — core must not import up to find plugins.

- **Every launcher method opens `if cls._impl is None: return None`**, so an unregistered
  launcher degrades the feature to absent rather than crashing. `MapPhaseSink` with no writer
  is likewise a no-op: the SCG structure write already happened and the phase is cosmetic.
- **`ActLauncher` is `start`/`wait` as TWO calls, not one blocking call.** A scoped refresh is
  sessionless except for its act phase; splitting mint from wait is what lets the runner
  persist which stage a job died in, because a blocking call that never returns records
  nothing.
- Tools resolve the store via `_ctx.resolve_runtime()`; tests patch each tool's module-level
  `_resolve_runtime` alias.

## "Optional" means BOTH layers

Heavy deps sit behind the `treesitter` and `retrieval` extras AND an import-guard at every
call site — both, or they don't go in. `mewbo-api[wiki]` forwards to
`mewbo-graph[treesitter,retrieval]`, and the api's graph imports are lazy/guarded so a base
install boots graph-less (`init_wiki` returns `False`, `SourceCatalog` falls back to empty).

**Grammar loading is a network download, not a bundle.** `tree-sitter-language-pack`
(`>=1.12,<2`) fetches each grammar on its FIRST `get_language()` call, so a first index in an
offline container aborts mid-parse. Pre-warm with `python -m mewbo_graph.wiki.prefetch`, which
derives the language list from `graph.py`'s `_LANG_BY_EXT` and calls the pack's `prefetch()`.
A manual `ctypes`+`cache_dir()`+`download()` path does nothing — `download()` is a no-op at
this pin. Docker's prefetch `RUN` needs its OWN `WIKI_EXTRAS=1` guard, since the module's
guard cannot fire when `mewbo_graph` isn't installed, and pins
`XDG_CACHE_HOME=/opt/mewbo-cache` so build-time root and runtime uid-1000 share the cache.

## SCG substrate

**The store is GLOBAL per-source** — flat `{source_id}#` keys, content-addressed node ids —
**never partitioned by workspace**: the SCG is one tenant of the shared multiplex graph and
the wiki/search memory layers cross-pollinate. A workspace is a scoped VIEW: `ScgScope`
(`scope.py`) carries a source-id allowlist on a `ContextVar` and `ScgRouter.route` drops any
recipe whose steps reach an out-of-scope source, so mappings stay shared while
routing/insights stay isolated. The ContextVar is the seam because the plugin tools call
`ScgCore.store()`/`router()` with no scope argument. Workspace is a `ws:<id>` attribution
label, never a partition.

**Two protocols named `StructureProvider`; never merge them.**
`wiki/structure_provider.py`'s (`resolve`/`resolve_many`/`entity_key_of`) resolves an
`entity_key`↔node for the memory layer; `scg/providers/base.py`'s `SourceStructureProvider`
(`build_structure`) builds a connector subgraph from a raw descriptor. The SCG memory bridge
must hand `InsightIngestor` a `ScgAnchorResolver` (the former protocol, over `source_key`) or
connector insights are written and then silently dropped on read.

`ScgAnchorResolver` resolves a `source_key` KIND-AGNOSTICALLY over
`_ANCHORABLE_KINDS=(capability, entity_type)`, because `node_id=sha1(source_key|kind)`: an
MCP-tool-list source is `capability`-only, so a fixed-`entity_type` probe drops every anchor —
notes persist, the ANCHORS edge never exists, the read returns nothing.

Routing blends a `ScgMemoryBias` term into `cosine+edge` — a vector read + polarity-weighted
sum, NO LLM, best-effort/empty without embeddings, `ScgScope`-respecting. Polarity rides
existing `MemoryNode.labels` as `scg:<pol>` (positive `+0.6` / dead_end `-0.8`, asymmetric; no
new field); `boost_for_steps` is the MAX over steps.

## QA closers (`qa.py`, `qa_access.py`)

`QaFinalizer` reconciles a Q&A answer snapshot from its event log and closes it; it lives here
rather than the api because it mutates `QaAnswer` + store, so the terminal `wiki_emit_answer`
calls it down-layer. `QaMemoryDepositor` distills a cited answer into atomic notes anchored to
the cited code entities — best-effort, idempotent, off the latency path.

- `resolve_qa_ctx` falls back to the latest `structured_workspace` context event, so a
  `StructuredResponder`-grounded run resolves a slug-only ctx instead of returning "wiki QA ctx
  not found".
- `tag_page_citations` accepts a page id OR a slugified page TITLE (`_slugify` symmetric
  match), so a model citing a human title still resolves. It runs from `wiki_emit_answer` so
  the live stream and the snapshot agree.
- `AccessedSourceResolver.resolve_refs` maps `graph:<node_id>` refs to labels at READ time and
  **must stay non-destructive** — the snapshot keeps raw ids because `QaMemoryDepositor`
  anchors off them.
- `QaAccessRecord` keeps `accessed_sources` bounded: graph-NAV tools record only their seed,
  ranked search tools score-floor their hits, and `_accessed_from_events` dedupes by ref, sorts
  score-desc and caps at top-N (default 12). `HybridRetriever` stays the one ranker.

## Abstract entities

Entities are new families on `WikiStoreBase`, embedded via the same `Embedder` and anchored
via the same `AnchorResolver`. `entities/resolver.py:ResolutionLadder` backs BOTH
`EntityResolver` and `InsightDeduper` — do not fork a second ladder. `Entity.id =
sha1(normalized_name|type)`, so every write is an idempotent UPSERT.

- **The KG is built BEFORE generation** — the entity stage is `enrich`, POST-AST. The
  `wiki-enricher` leaf grounds each LLM entity against AST symbols + SOURCE prose
  (docstrings/comments/READMEs), never generated page prose; ungrounded ⇒ dropped. Skip
  Leiden/Louvain: plan by the free AST/module/package/directory hierarchy + co-occurrence.
- **A persisted field the model cannot SEE is dead.** `Entity.labels` round-trips through the
  store but stays `[]` unless the tool schema exposes it. Surface every model-writable field on
  the schema, and UNION list-valued fields on BOTH resolution paths (`_apply_merge` AND the
  `_apply_new` idempotent re-mint fold) — a deterministic id means re-index always re-resolves,
  so a non-unioned `labels`/`mentions` silently regresses on the second pass.
- **The entity↔code bridge is a NOTE, not an edge family.** The enricher
  `wiki_submit_insight`s anchored to BOTH `entity:<id>` AND `file#Symbol`; the memory `ANCHORS`
  path already ties all corpora into one graph. The enricher reads the most prose, so it — not
  just the indexer — must own `wiki_submit_insight`, or the memory layer stays empty.
- **`KnowledgeGraphView` (api side) is the ONLY multiplex reader.** It reconciles cross-layer
  `ANCHORS` via `resolve_many` — classify a target by set-membership, NEVER id length.
- `rapidfuzz` is optional; absent, `fuzzy_ratio` falls back to exact match, i.e. cosine-only.

**Entity resolution is not deterministic under replay.** `EntityResolver.resolve` scores
`max(cosine, fuzzy_ratio)` against whatever the store holds AT CALL TIME, so a resumed `enrich`
replaying an identical mint can merge against a neighbour that did not exist on the first pass
and write a different entity id for a byte-identical call. Compounding it,
`EntityRecommendation.compute_id` = `sha1(action|sorted(subjects)|type)` folds `action` into
the hash, so `merge` and `distinct` priors on the SAME pair persist as two rows rather than one
superseding the other; `_recommendation_priors` folds per pair and the LAST row read wins,
which is insertion order on the JSON driver and unspecified on Mongo's `find()`. **The same
persisted recommendations can therefore resolve differently on a dev store than a deployed
one.** The resolver's docstring names the two candidate fixes.

### Hierarchy wire mode (the 3D "Code Galaxy" viewer)

`KnowledgeGraphView.for_slug(..., hierarchy=True)` (route `?hierarchy=1`) merges a
`FolderTree` scaffold of VIEW-ONLY `Folder` nodes so the FE can cluster and collapse by folder
— the level-of-detail that lets it render a ~17K-node repo without OOM. The stamp helper is a
no-op when off, so the default payload stays byte-identical and the SCG reuse path is
untouched.

- **`External` nodes are NOT parentless** — they hang off
  `folder_tree.py:_EXTERNAL_BUCKET_ID` = `folder:__external__`, minted only when at least one
  external exists. A null parent exempts every `External` node from the expand/collapse rule
  governing all real code, so the least interesting layer is the one that always renders. Its
  id and label are stable across builds; the console keys off both.
- **`node_limit` caps the WHOLE AST payload, view-synthesized externals included.** Prune
  persisted AST nodes by degree (ties on `node_id`), then give externals the remaining budget by
  the same rule, so an AST layer that spends the full cap leaves externals at zero. Any external
  the cap drops takes its inbound edges with it. Entity and memory layers are never counted
  against the cap; `total_nodes`/`total_edges` still describe the full AST graph so "showing N
  of M" stays honest.

## Every graph read names a commit — `CommitScope`

The store holds the UNION of every commit ever indexed for a slug. Reading that union renders
deleted code as live, grounds QA answers on superseded symbols, and lets finalize's "is the
graph populated" gate count generations other than the one it just built.

`query_graph` and `list_edges` take a **required** keyword-only `scope: CommitScope`
(`wiki/types.py`). No default: "every generation" as a default is the fail-open filter root
`CLAUDE.md` forbids, and with none, a missed call site is a typecheck failure rather than a
silent read of all history returning `200`.

**`CommitScope` is a type rather than a `commit_sha: str | None` parameter, and collapsing it
back reintroduces the trap.** `count_graph_nodes` and `supersede_graph_artifacts` take
`commit_sha`, where `None` means **stamped NULL — an exact match**, which is how they count and
preserve the pre-isolation generation. Reusing that name on a read with "unscoped" semantics
gives one parameter opposite meanings on two methods of one class. **`CommitScope.at(None)`
keeps the exact-NULL sense and is NOT unscoped**; "every generation" is `CommitScope.every()`,
a separate constructor that cannot be confused with it.
`tests/wiki/test_graph_commit_scope.py` asserts that parity. Both projections live on the type,
not the drivers, because JSON tests a loaded row while Mongo narrows a query document.

`store.live_scope(slug)` derives the scope from the project row's `commit_sha` and is what
nearly every reader should pass. A project with no commit (a catalog ingestion) has exactly ONE
generation, so it returns `every()` — the union and the live set are the same set, not a
fallback.

**Three readers deliberately read the union; each carries its reason at the call site:**

| Reader | Why the union |
|---|---|
| `CodeStructureProvider.entity_key_of` | Maps a node id recorded in the PAST to a label. A node id embeds `start_byte`, so any edit re-keys it out of the live generation and scoping turns a resolvable citation into `unknown(...)`. The other direction (`resolve`/`resolve_many`, key→node) IS live-scoped — an `entity_key` carries no offset. |
| `QaAccessRecord`'s ref resolution (`qa.py`) | Same shape, same reason: the refs come from earlier sessions. |
| `GraphDeltaIndexer` (`refresh.py`) | A refresh observes the store MID-TRANSITION: dirty files are re-parsed stamped with the NEW commit while untouched files still carry the previous one, so between the retract and finalize's supersede sweep the graph legitimately spans two generations. `_universe` de-dupes by `node_id`, since both generations can carry a byte-identical node. |

**Scoping the viewer severs the entity→code bridge unless anchors are repaired.** An entity
anchors by raw `node_id`, which embeds `start_byte`, so an edit re-keys it — unscoped that never
shows, because the superseded node is still in the payload to point at.
`KnowledgeGraphView._reanchor_entity_edges` re-points by `entity_key` (`file#name`,
offset-free), fetching superseded nodes by explicit id so the cost is bounded by anchor count
rather than by reading the union back in. An anchor whose symbol no longer exists stays dropped
— correct, not a loss. The memory layer needs none of this: a memory `ANCHORS` edge stores an
`entity_key` as its target.

## Developer mode — graph-only indexing (zero-LLM)

`runtime.developer_mode` unlocks a deterministic, sessionless, zero-LLM index for inspecting
AST graph construction without paying for doc generation. `GraphOnlyIndexer`
(`plugins/wiki/graph_only.py`) runs `clone → scan → graph → finalize`, reusing the phase tool
cores (`clone`'s credential/SSH helpers, `scan._collect_files`,
`build_graph.build_graph_core`, the `finalize` helpers) so there is no duplicated
git/scan/tree-sitter logic. It stamps `Project.graph_only=True`, writes zero pages, and uses
the synthetic landing id `"graph"`.

**ONE doc-content read seam raises for everyone.** `WikiStoreBase.get_page` is a template
method → `_assert_docs_available(slug)` raises `DocumentationUnavailableError` when the project
is `graph_only`. That single raise site is inherited by the api page route, MCP
`read_wiki_page`, and the QA orchestrator's probe reads — add no per-caller checks.
`list_pages` is deliberately unguarded: finalize prune, `WikiResume`, the retriever and
`tag_page_citations` walk it on deterministic internal paths, and guarding it breaks indexing
itself. `KnowledgeGraphView` never calls `get_page`, so a graph-only project stays explorable.

## Git auth — one chain, one hardened executor, no cache

Every git-touching path (clone, `ls-remote`/branches, freshness, the platform description
fetch) authenticates through the two modules below. There is no third way.

### `wiki/credentials.py` — the ONE resolution chain

`resolve_chain(store, slug, *, arg_token=None)` defines precedence for every consumer:
`arg` → `store:repo` → `store:host` → `ambient` → `anonymous`. The domain is Pydantic
end-to-end (`CredentialScope`, `CredentialSource`, `CredentialCandidate`) so no caller
re-derives a rule from a string.

- **It is a lazy generator, and that is load-bearing.** The ambient tier forks `git credential
  fill`, a 10s-capped subprocess; eager resolution pays two store reads plus that fork on every
  call even when the first candidate suffices.
- **Keyed by SCOPE, not by repo.** A bare host is shared by every repo on it; a full
  `host/owner/repo` is repo-pinned. **`CredentialScope.covers(other)` IS the sharing rule** —
  one predicate, not a `"/" in scope` check re-typed per call site.
- **Scope DEPTH is uncapped past `host/owner/repo`.** A GitLab subgroup
  (`gitlab.com/group/sub/proj`) is a legitimate 4-segment slug and 2-segment `owner/repo` is
  still in the wild, so `.owner`/`.repo` read the LAST TWO segments. Rejecting deeper scopes
  makes a subgroup repo's credential unresolvable and downgrades its clone to anonymous.
- **A malformed scope fails fast at the WRITE boundaries** (`from_slug`, the `<path:scope>`
  route → 400), because written under an unresolvable key it surfaces much later as an opaque
  "the clone silently fell back to anonymous". READ paths (`coerce`) stay tolerant and degrade
  to "no scope" rather than failing a clone that would succeed anonymously.
- **Never reverse-engineer a scope from a filename.** The JSON driver's
  `credentials/<scope>.json` encodes `/` as `__`, so inverting that corrupts any scope
  containing a literal `__`; the scope is stamped inside the blob at save instead.
- **`is_auth_failure(stderr)` is the one auth classification, and its markers are ANCHORED**
  (`error: 403`, `http 401`, `authentication failed`, …). Bare `401`/`403` substrings misfire on
  network noise — `port 8403: Connection refused` reads as a 403, burning the chain and emitting
  a false "credential rejected". `repository not found` is in the set deliberately: GitHub masks
  private repos as 404 to unauthorized clients.
- **`from_repo_url`'s scp branch (`git@host:owner/repo`) is reachable, not defensive** — the
  wizard accepts an scp remote and, with no slug chosen, that raw string arrives as the
  resolution scope. Normalisation is part of parsing: trailing `.git`/slashes dropped, host
  lowercased, **owner/repo case PRESERVED** (significant on some forges, and lowercasing
  re-keys existing credentials).
- **The remote GRAMMAR is not implemented here — the PROJECTION is.** Shape recognition and
  normalisation delegate to `mewbo_core.workspaces.repositories.RepositoryRef.split_remote`,
  the one grammar the api's `RepoIdentity` also parses through. What stays local is how a
  *scope* reads the parts, and it genuinely differs: with no structural host every token is a
  path segment, so a lone `git.example.com` is a bare HOST scope where a repository reference
  reads the same token as a repo NAME. Share the grammar; never push the projection into it.
- **`PlatformId` lives in `mewbo_core.workspaces.repositories`, re-exported from
  `wiki/types.py`** because the repository registry needs the literal from a base install,
  which cannot import this optional library. Keep the re-export or every importer of
  `mewbo_graph.wiki.types.PlatformId` breaks.
- **`CredentialCandidate` enforces "credential is None IFF source is anonymous"**, so a
  consumer never checks both, and an ssh key's username never leaks into the token-injection
  path.
- **Dedup keys on `(kind, value, username)`** — one value shared by two usernames (a GitLab
  `oauth2` deploy token vs a PAT) must try BOTH.
- **`RepoCredential.value` is stripped at definition**: a PAT pasted with a trailing newline
  401s with no hint that an invisible character is the cause. Safe for SSH keys — internal
  newlines survive and `clone._ssh_env_for` re-appends the terminating one.

**There is no token cache, deliberately.** A resolved-token cache is a third source of truth
alongside the store and the ambient credential, and it drifts: a revoked stored token
permanently shadows a still-valid ambient one with no fallback, producing cascading re-index
failures. The chain is lazy precisely so caching "to save a store read" is unnecessary.

### `plugins/wiki/clone.py` — the ONE hardened git executor

`run_git_with_chain(store, slug, url, build_argv, …)` is the single credential-iterating
subprocess runner: it walks `resolve_chain`, injects a token into the URL (or writes an SSH key
to a 0600 temp file), runs the caller's argv, redacts every tried secret from stderr, advances
ONLY on an auth-class failure, and aborts the chain on anything else. Its outcome carries the
`winner`, so a follow-up call can prefer it instead of re-resolving blind.

`build_clone_command` / `build_ls_remote_command` / `hardened_git_env` are the shared argv+env
builders, and **every git subprocess in this library goes through them**: the chain-driven ones
(clone, freshness, the api's credential-validate route), the DI'd `ls-remote` in `branches.py`
(which iterates no chain — that is the api route's job), and `clone._run_local_git`, the
network-free `init`/`checkout FETCH_HEAD`/`rev-parse` helper. The one git invocation outside
them is `credentials.py`'s read-only `git credential fill`, the ambient tier itself.

**Advance on REFUSAL, abort on FAILURE — the same law on both sides.** The git executor
advances only on an auth-class stderr; the REST helper advances only on `_AUTH_RETRY_STATUSES`
= 401/403/404, the 404 because platforms mask private repos from unauthorized clients. A
network error, timeout or 5xx returns immediately in both: a different credential cannot fix a
transport failure, and retrying burns valid candidates against a dead endpoint while the user
waits.

**THE TRAP: an inherited `credential.helper=store` with a read-only `~/.git-credentials` masks
the real auth error.** Git's post-auth `store`/`erase` fails with `fatal: unable to write
credential store: Device or resource busy`, that EBUSY replaces the real message,
`is_auth_failure` does not match it, and the chain aborts instead of falling through — an
otherwise successful clone dies with a nonsense error. Hence `-c credential.helper=` on every
git subprocess plus `GIT_TERMINAL_PROMPT=0`, and hence we read the ambient credential ourselves
and inject it into the URL. Git never touches that file. A call site that assembles its own
argv reintroduces this.

**THE SECOND TRAP: winning a git operation does NOT prove a credential is valid.** A public
repo serves `ls-remote` and `clone` happily with a REVOKED token — the git layer never
challenges it — so the chain never advances and `run_git_with_chain` returns a dead credential
as its `winner`. Hand that to an authenticated REST call and it 401s, and the caller degrades
silently (no description, `behind_by=None`) even though a later chain credential would have
worked. Measured: a revoked repo-scoped token cloned a public repo fine, then 401'd a compare
API that the ambient credential answered with `total_commits=155`. **A git success is evidence
about the REPO's visibility, not about the CREDENTIAL.**

So every authenticated REST call re-walks the chain itself through one retry policy,
`_platform_api.api_get_json_with_chain(...)`: `preferred_token` first (the git winner — usually
right, free when it is), then each remaining candidate on a refusal, capped at
`_MAX_CRED_ATTEMPTS`. **Anonymous is ALWAYS the terminal attempt and never counted against the
cap**, because a public repo still answers when every stored token is revoked. The walk is lazy
so a winning `preferred_token` never forks `git credential fill`. Neither caller owns a retry
loop, and a third authenticated call does not get one.

`plugins/wiki/_platform_api.py` is the ONE per-platform REST seam: the auth header table
(github `Bearer` / gitea `token` / gitlab `PRIVATE-TOKEN`), the GHE `api/v3` base-URL branch,
the private-TLD TLS carve-out, the guarded fetch that never raises, and the retry policy above.
Bitbucket is description-only: its app-password Basic scheme is not portable from a bare token
and it has no portable compare shape.

**`RepoFreshness.check`'s three-state result is deliberate.** `up_to_date=None` = could not
check; `behind_by=None` with `up_to_date=False` = the sha MOVED but the commits cannot be
counted; `behind_by=0` = fresh. Never collapse the two `None`s into a false green.

### Remote branch listing + ref-pinned clone

`RemoteBranchLister` (`plugins/wiki/branches.py`) backs the wizard's branch picker with one
`git ls-remote --symref` subprocess, no clone, the symref line naming the default branch.
**Host-agnostic by design:** `ls-remote` works identically on all six platforms, so we
deliberately do not call per-platform REST branch APIs, each a different path + auth +
pagination. It builds argv/env with the shared builders, so token injection, the SSH temp-key
env, the private-TLD TLS carve-out, the helper-disable and the stderr scrubber are byte-for-byte
the clone path — this endpoint is jobless and was the last place the EBUSY trap survived.
`username` threads a stored credential's own username (GitLab `oauth2`/deploy tokens) rather
than always injecting `x-access-token`.

`build_clone_command(clone_url, dir, *, ref, private_host)` is the SINGLE clone-argv builder —
both `WikiCloneRepoTool` and `GraphOnlyIndexer._clone` thread `ref` through it, so
`--branch <ref> --single-branch` and the private-TLS insert live in one place. `ref`
round-trips the submission sidecar like `graph_only`, so refresh and recovery preserve it.

## Code graph schema (`types.py`)

Nodes are per-kind subclasses of `GraphNodeBase` discriminated on the LITERAL `type` field
(wire compat is non-negotiable) — construct via a per-kind class or `make_graph_node(**data)`,
never a bare dict. `GraphNode` is a union type ALIAS + `GraphNodeAdapter`, deliberately not the
house `RootModel` union pattern: node fields are read pervasively, so a `.root` wrapper would
churn dozens of call sites. Nodes are `frozen=True`, so hierarchy stamping happens on the wire
dict in `KnowledgeGraphView`, never the node. `subkind: str | None` and namespaced `attributes`
are the two open extension axes; kinds stay structural and closed. `CodeGraph` validates the
whole graph at ingest and rehydration bypasses it via `model_construct` — an ingest-time gate,
not a read-time tax.

**Version the field, not the prose.** `CodeGraph`'s docstring says "schema v2", describing the
Python model layer. The machine-checkable value is
`CodeGraph.schema_version: Literal["1"] = "1"`; code every version check against the field.

**The tree-sitter EXTENDS block computes its edge SOURCE id from the subclass NAME token's
`start_byte`, not the class DEF node's, so it matches no persisted node and
`KnowledgeGraphView` silently drops the edge.** `CodeGraph` encodes this rather than fighting
it: any edge carrying `target_name` is exempt from endpoint resolution on BOTH ends. **Any new
capture family emitting edges must derive its source id from the DEF node's byte** — the
extension-function block (`fun Receiver.name()`) is the reference.

**The scip resolver does not supersede those edges, not even for Python.**
`_is_superseded_python_edge` reads the edge's SOURCE node to decide the language, and the trap
above guarantees that lookup returns nothing, so the conservative "unmappable source ⇒ keep"
branch fires on every EXTENDS edge. Measured on a real monorepo run, the filter dropped 83,183
CALLS and 7,001 IMPORTS and **0 of 1,017 EXTENDS**. A resolved index therefore carries dead
tree-sitter EXTENDS alongside the resolver's correct ones; the view drops the dead ones at read
time, making this dead weight rather than corruption.

**Subkind assignment picks the TIGHTEST containing def, not the first.** `_subkinds_for`
byte-contains a `@<family>.subkind.<value>` capture against same-family defs, and "first
containing def" mis-assigns a nested same-family construct's subkind to its OUTER container —
a data class nested in a sealed class, whose wider range also contains the inner's "data"
token. Applies to any nesting-capable construct.

**Capture lists are NOT mutually index-aligned.** `QueryCursor.captures()` groups matches per
capture NAME and the per-name lists' order varies run to run, so
`zip(captures["class.def"], captures["class.name"])` attaches a node's name to another node's
byte range — the captures dict itself is per-call nondeterministic. `graph.py:_by_position`
sorts each capture list by start byte before zipping; every captured def is a disjoint sibling
containing its own name, so document order is a sound join key. Same reason the resolver's
`_match_node` requires `node.name == leaf_name`.

**`captures()` already dedupes across overlapping patterns within one compiled query**, so
`_dedupe_nodes` is a backstop, not the mechanism. A def with no name at all (an anonymous
`companion object`) pairs via `_pair_defs_with_names` — byte-containment, not the strict
positional zip — and defaults to the literal "Companion".

## Faithful symbol resolution — `wiki/resolve/` (scip-python)

The tree-sitter extractor emits cross-file edges *by name*, mislinking every same-named
method/class onto one node. `wiki/resolve/` resolves them exactly behind a `SymbolResolver`
protocol; `ScipPythonResolver` is the Python backend. It only refines edge targets, so it is
reversible.

**There are TWO call sites and a change to the swap must satisfy both:**
`plugins/wiki/build_graph.py:_apply_resolver` (the full `graph` phase, before persistence) and
`wiki/refresh.py:ScopedEdgeResolver` (the delta path, calling that SAME `_apply_resolver`
behind an `_EdgeResolver` `Protocol`, because `wiki/` may not import the plugin package). **A
delta path with no resolver overwrites resolved edges with name-matched ones on every
refresh**, so connectivity decays monotonically, one refresh at a time. Measured across two
live projects on one deployment: the heavily-refreshed one held **21** `REFERENCES` edges, the
less-refreshed one **17,194**.

Neither call site is the read path. `ResolutionResult` edge targets are real `node_id`s
(`target_name=None`, bypassing the view's name-fallback). `SubprocessScipProducer` is a DI
seam, so unit tests inject a captured `scip print --json` and need no binaries.

Non-obvious scip-python facts, verified on a large Python monorepo:

- **One run per `[project]` root.** The monorepo root alone yields an EMPTY index. Discover each
  `pyproject.toml` with a `[project]` table (line scan, no `tomllib` → 3.10-safe), run
  scip-python per root, and write a per-root `pyrightconfig.json` whose `extraPaths` point at
  sibling import roots so cross-package imports resolve **without a virtualenv**.
- **Descriptor stitching.** A symbol is `scip-python python <pkg> <ver> <descriptor>`.
  Cross-*project* refs carry the WRONG `<pkg> <ver>` (no venv → no distribution map) but a
  byte-identical `<descriptor>` tail. Resolve order: exact-full-symbol → descriptor-single →
  **drop on descriptor-ambiguous, never mislink** → External. In-repo-but-unmodelled symbols
  (attributes/params = SCIP `.`/`(name)` leaves) are dropped, never faked external.
- **`--project-version` is required** or scip-python crashes inferring it from git on a non-git
  checkout. Pin `0`; the token is stripped during stitching anyway.
- **byte ↔ line:col.** SCIP ranges are 0-based `[line, col]` with
  `text_document_encoding=UTF-8`, so `col` IS a byte offset and a def occurrence points at the
  NAME token. Build a line→byte table per file and map by innermost byte-containment + name.
  SCIP `relative_path` is project-root-relative — join it with the project-root prefix to get
  the repo-relative path that **equals `GraphNode.file`**, the wiring contract.
- **Edge types are `REFERENCES` + `EXTENDS` only.** Not `IMPORTS`/`CALLS`: scip-python does not
  reliably set the SCIP Import role — imports surface as `ReadAccess` — so splitting them out is
  guessing. Fix at this seam if ever, never by sniffing roles.
- We resolve to NODES (only File/Class/Method/Function exist), so the headline rate reads lower
  than scip's def-*site* percentage, but **100% of in-repo refs to modeled kinds resolve with
  ZERO mislinks** (41.5k exact + 1.2k EXTENDS, 0 ambiguous).
- **The availability gate is a `shutil.which` on BOTH binaries, so deployment provisioning
  decides whether faithful resolution happens at all.** They sit behind `SCIP_EXTRAS` in the api
  image; with the flag off the leg no-ops and **the resulting graph looks healthy** — fully
  populated, passes validation, renders — while carrying NO cross-file edge whatsoever, so its
  connected-component count equals its File-node count and every "who calls this" answer is a
  name match. `_apply_resolver` reports which leg ran through its injected `on_report`; a
  `graph` phase whose job log names neither leg is itself a regression.
- **Neither subprocess may be CONFINED.** They run unsandboxed, with no Landlock
  `shell_preexec_scope` — that scope is the shell tool's control for opaque model-authored
  commands, while these are first-party read-only type-checkers whose argv we build. Confined to
  the project root, scip-python cannot see the interpreter's site-packages, so its own
  `pip list` dependency probe fails and **every root of every repo exits non-zero**, zeroing
  every cross-file edge while `is_available()` still reports the binaries present. No
  `shutil.which` check catches that. A sandbox here would have to admit the interpreter's own
  import paths, not just the clone.

**Superseding is gated on a COMPLETE pass — `GraphResolution.faithful`.**
`ResolutionResult.available` only means "at least one root indexed", and dropping the
name-matched edges of all nine roots on a pass that indexed six leaves three roots with no
cross-file edges at all: deleting the guesses and putting nothing back is strictly worse than
keeping them. `_apply_resolver` drops name-matched edges only when `faithful` (ran, indexed ≥1
root, indexed every root it discovered); a partial pass contributes its exact edges and
withholds the destructive half. Superseding per ROOT is not derivable at that seam —
`ResolutionResult`/`ResolutionStats` carry root COUNTS and no root identities — so fix it by
giving the stats identities, never by re-deriving the pass-level question inside the per-edge
predicate.

**The outcome is a persisted RECORD, not only a log line**, because a total resolution outage
is invisible behind a job-log line nobody reads unless already suspicious. `GraphResolution`
is stamped on `IndexingJob.resolution` by `build_graph_core` and copied onto
`Project.resolution` by `finalize.py:_resolve_graph_resolution` — the same read-preserve seam
`_resolve_index_fingerprint` uses, because `Project` is constructed WHOLESALE at finalize, so
without the read a first index persists nothing and every re-index overwrites a good value back
to `None`. `PROJECT_UPDATABLE` carries `resolution` so the graph phase's mid-run
`update_project` lands on an existing row; that is a partial-update requirement, not a statement
that the field is user-editable — nothing on the settings surface may set it. `faithful` and
`degraded` are plain properties, deliberately not `computed_field`s: the model is
`extra="forbid"` and round-trips through the store, so a derived key inside `model_dump` makes
every persisted record fail to re-validate. `None` reads as unknown, never as a healthy pass.

`ResolutionStats.dropped_unresolved` counts reference-SITE failures — an unreadable file, a
range outside the file's bytes, or no enclosing def to hang the edge's source on. Every other
counter describes what a reference pointed AT. Read the two families apart before diagnosing a
low resolve rate.

## Testing

Tests live under the root suite at `tests/wiki/` and `tests/agentic_search/scg/`. Inject fakes
at the seams (`reset_for_tests`, a fake embedder / fake LLM, `MapPhaseSink.reset()`); never
spawn a real LLM or hit a real proxy. The library is installed editable, so plugin
self-registration on import is live in tests.

## Performance — the offline/interactive boundary

Root `CLAUDE.md` sets the cost-class vocabulary and the law. This package is the main
legitimate home for `O(all history)` work — an indexing job, an embedding pass and a graph
build all scale with a repository's full content. The discipline is keeping that class on the
JOB side of the boundary, labelled, never inherited by an interactive reader.

| Surface | Class |
|---|---|
| The seven indexing phases | `O(repo)`/`O(all history)`, labelled; never a route handler |
| `GraphIndex.parse_repo` | `O(files in repo)`; reachable only from `build_graph_core` |
| `Embedder.embed_nodes` | `O(repo)`, batched + bounded-concurrent |
| `CodeGraph` ingest validation | `O(nodes+edges)`, offline only; bypassed on read |
| `wiki_query_graph` / `wiki_graph_neighbors` | interactive; must stay bounded regardless of repo size |
| `KnowledgeGraphView.for_slug` (api side) | interactive; one project's KG payload per request |
| `vector_search` / `memory_vector_search` | interactive, unbounded INPUT |

**Do not let an interactive reader originate offline-class work.** The tree-sitter grammar
download is the worked case: a cold parse on a request path pays it inline, so it is prefetched
at deploy time. Any new "load X on first use" (a model, a large cache, a compiled grammar)
needs the same treatment — warmed offline, or refused on a request path.

**`wiki_graph_neighbors` loads the whole live graph to answer a bounded query.** Its
constructor reads `store.list_edges` and `store.query_graph` — every edge and node of the live
commit — before the bounded BFS (`hops` ≤ 3, `limit` ≤ 500). The docstring accepts the
trade-off at a few thousand edges, but this is a tool an agent calls repeatedly in one session,
so past a few thousand nodes/edges the CONSTRUCTOR, not the BFS, is where to look. Commit
scoping improved the constant, not the class: the read is bounded by one generation (26,344
nodes here rather than 58,180), so it stops growing with re-index COUNT while still growing
with repository size. Both reads share ONE resolved scope — resolving twice could straddle a
re-index and hand the walk an edge table whose endpoints are not in its node table.

**Vector search has no input bound.** `WikiStoreBase.vector_search` / `memory_vector_search`
read every stored embedding for the slug into memory and score cosine in Python;
`ScgStoreBase.vector_search` does the same over EVERY embedding in the whole store, not scoped
to one source. All are reached from interactive paths. `k` bounds the OUTPUT; the read is
`O(collection)` in stored embeddings. First place to profile if a QA/search request shows
latency scaling with repo or store size — and a fix lands behind the SAME signature, never a
second ranking path.

### Complexity rules for indexing jobs

- **Batching bounds request COUNT; concurrency fixes throughput, and batch size is NOT the
  lever.** `Embedder._embed` batches to `wiki.embedding.batch_size` (default 64), so ~10k symbols
  issue ~150 calls rather than 10k — but sequential blocking round-trips measured 505 batches /
  ~18 minutes on a 32,262-node repo with the process near 0.55% CPU, i.e. pure socket wait.
  Per-call latency scales with batch size (1→0.87s, 16→0.29s, 64→1.20s, 128→2.51s), so
  per-connection throughput is roughly flat near 50 texts/s whatever the batch. `_embed` fans
  batches through a bounded `ThreadPoolExecutor` paced by `_EmbeddingPacer`
  (`wiki.embedding.concurrency`, default 4) plus optional sliding one-minute request/token
  budgets, both off by default — an invented ceiling either throttles a generous provider or
  under-protects a strict one, so `concurrency` is the real safety valve. A `429` honours
  `Retry-After` when present, else exponential backoff with jitter, and halves the ceiling for
  the rest of the run; a non-429 propagates immediately. **Order is a hard contract**: batches
  are submitted with their position and reassembled by index, never by completion order, because
  every caller depends on one vector per input text in input order. `_embed` stays synchronous
  (its callers run it from worker threads) and a single batch skips the pool, so `embed_query`
  stays cheap. `tenacity` is deliberately unused — only a transitive dependency — so retry uses
  the stdlib.
- **Vector width is part of `vector_search`'s constant, not just storage.** The deployment
  default is `openai/text-embedding-3-small` (1536 dims, 88.9 texts/s at batch 64) over
  `text-embedding-3-large` (3072 dims, 44.7 texts/s) or `gemini-embedding-001` (3072 dims, 48.3
  texts/s) — 1.84× the throughput and half the per-vector cosine work in the `O(collection)`
  scan. **The two widths are incompatible, not merely stale**: a store holding both returns wrong
  neighbours rather than erroring, so `IndexFingerprint.embedding_model` sits in the fingerprint
  compare set to force a full rebuild on a model change.
- **Cache what loads once per job, not once per file.** `GraphIndex` caches tree-sitter
  `Language`/`Query` objects for the life of the job instance, so `parse_repo` pays
  grammar/query compilation once per LANGUAGE.
- **A phase logs the count it is about to scale with** before the loop, so a stuck job is
  diagnosable from its own log. **Progress into the job record is throttled, never
  free-running** — `PhaseProgress` (`_PROGRESS_INTERVAL_S = 5.0`) stands between a long fan-out
  writing nothing for 25 minutes and hammering the store every iteration.
- **A skip-count failure must never read as zero.** `ResumePlan.build` raises `ResumeCountError`
  on a store exception rather than returning `0` — reading a transient hiccup as "nothing built
  yet" triggers a full re-index plus re-embed, the most expensive wrong turn this job can take.

### Index coverage — measured

`wiki_graph_edges` carries `_id_`, a compound unique key on `(slug, source, target, type)`, and
`ix_wiki_graph_edges_slug_commit` on `(slug, commit_sha)` for the supersede sweep and the resume
count. The `$or: [{source}, {target}]` neighbour lookup is **not** a collection scan despite
`target` not being an index prefix: `explain("executionStats")` on this repo's slug (79,143 live
edges) examined 104 docs to return 102, ~1.02 examined per returned. Do not add an index for it.
Caveat: one probe id with ~100 matches on one project, so a pathological hub is untested.
Whole-generation reads amplify not at all — nodes scoped 26,344/26,344 (~38 ms) against 58,180
unscoped; edges scoped 79,143/79,143.

### Profiling recipes

Read `apps/mewbo_api/CLAUDE.md` → "Debugging session errors" for the
`IXSCAN`/`COLLSCAN`/`totalDocsExamined` legend. Read the mongo credentials from the container's
own environment via `sh -c`, so they are never typed, never in shell history and never in this
file:

```bash
docker exec assistant-mongo-1 sh -c 'mongosh --quiet \
  -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin mewbo --eval "<JS>"'
```

Collections are `wiki_graph_{nodes,edges}` / `wiki_embeddings` / `wiki_jobs` /
`wiki_job_events`, keyed by `slug`; the SCG equivalents are
`agentic_search_scg_{nodes,edges,embeddings,recipes,sources}`, unscoped by slug.

**`wiki_job_events` rows carry no timestamp field**, but an un-supplied `_id` is a Mongo
`ObjectId` whose `getTimestamp()` IS the insert time — sort by `idx` (the durable order
`emit_phase` writes) and diff consecutive `phase` events to time a phase. `graph`/`enrich` have
no per-unit boundary tool, so poll `wiki_jobs`'s `phase`, `phase_progress_current`/`_total` and
`last_progress_at` instead.

## Pre-edit checklist

- [ ] Does new code import only `mewbo_core` + `pydantic` (down)?
- [ ] New heavy dependency: behind an extra AND import-guarded at the call site?
- [ ] New store query: does it filter on a PREFIX of an existing compound index, or need a new
      one? Add it in the SAME `_ensure_*_indexes` method (`background=True`). Reachable from an
      interactive path ⇒ run `explain("executionStats")` against a realistic node/edge count;
      `stage` should read `IXSCAN`, not `COLLSCAN`.
- [ ] New "read everything, then score/filter in Python": state its cost class in the docstring
      and confirm the call site affords `O(collection)`.
- [ ] New indexing phase: does it declare its resume behaviour (`wiki/CLAUDE.md`), log the count
      it scales with, and wire a long fan-out into `PhaseProgress`?
- [ ] New heavy first-use load: can a request path reach it? Then it needs the same prefetch
      treatment as tree-sitter grammars.
- [ ] New wiki plugin tool: subclass `plugins/wiki/_base.py:WikiSessionTool` and implement only
      `handle()` + the args schema.
- [ ] New AgentDef: its `tools`/spawn `allowed_tools` MUST use the REAL registered id
      (`wiki_code_search`, not `code_search`) — `filter_specs` silently drops unknown ids, so a
      typo becomes a tool the agent can never call.
