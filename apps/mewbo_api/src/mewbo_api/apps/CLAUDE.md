> ↑ [apps/mewbo_api/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# Mewbo Apps — API Subsystem Guidance

Scope: `apps/mewbo_api/src/mewbo_api/apps/` — the backend of Mewbo Apps (LLM-built,
trigger-maintained mini apps: a versioned stlite frontend + agent-authored data pipelines + a
per-app data namespace, bound to a workspace). `agentic_search` is the shape precedent: store
app-side, routes as an atomic controller, capability-gated agent plugin. Everything readable
straight from the code is left out.

## The two entities

- **`AppSpec`** (`models.py`, keyed `app_id`) — the durable, append-only-versioned manifest:
  `frontend`, `collections`, `pipelines`, `policies`, `workspace_ref`, `owner_session_id` +
  `maintainer_session_id`, `status` (draft→building→live↔paused / broken / archived).
- **`PipelineRun`** — the provenance ledger, opened at a trigger fire and closed at the
  maintainer run's end. It is the freshness signal, the `/system` backing and the repair-loop
  input at once. `docs_written` is incremented by `app_data` writes.

**Strategy-on-model rule** (as `TriggerSpec`): `AppSpec.transition()`, the `PipelineRun`
freshness classmethods and `AppReadToken.is_valid(now)` live on the models; stores are DI'd;
models never import I/O and NOW is always a method arg, never a wall clock. Extend
`_ALLOWED_TRANSITIONS`; never bypass `transition()` with a bare `self.status =`.

## Stores

Three families in `store.py`, each `Base ABC → Json + Mongo + create_*()` plus a process
singleton (`get_app_store` / `get_pipeline_run_store` / `get_app_data_store`). The driver comes
from CONFIG (`storage.driver`), never the `MEWBO_MONGODB` env.

- The routes controller AND the plugin's `app_data` tool both resolve through those factories —
  that shared singleton is why `app_data` needs no wiring push. `set_stores_for_tests` is the ONE
  swap seam; a parallel store the route cannot see tests nothing.
- `AppDataStore.upsert(collection_spec=)` and `query(sort=)` are additive optional kwargs over
  frozen positional signatures, and the plugin's local Protocols declare the same shape. Keep them
  additive.

## Token model — `token_id` IS the credential

`tokens.py:AppReadTokenSigner` mints stateless HMAC-SHA256 render tokens keyed by
`api.apps_token_secret` (`x-secret`) when set, else the API master token with a startup WARNING
(`backend.py:_build_apps_token_signer`). Rotating the secret invalidates outstanding tokens,
which is safe — they are 30-min short-lived.

The whole signed blob `<app_id>:<exp>:<token_id>:<sig>` IS the `token_id`; there is no separate
secret. The client presents it verbatim in **`X-Mewbo-App-Token`** (also accepted as
`Authorization: Bearer` / `?token=`) and `verify(token_id, now)` re-derives app_id + expiry.
Read-auth on the data/system GETs is a master/issued key OR a token whose `app_id` matches the
path: a valid token for ANOTHER app is 403, forged or expired is 401. The master key never enters
the browser or WebView.

## Pipeline arming — the pipeline DECLARES, the platform arms

`PipelineSpec` carries a discriminated `schedule: PipelineSchedule | None` (`CronSchedule
{kind:"time.cron", cron}` / `AtSchedule {kind:"time.at", at}`, each owning its validator plus a
`to_trigger_spec(wake_prompt, session_id, now)` strategy building the core `TriggerSpec`), an
`on_demand: bool`, and `trigger_ref: str | None` which is **PLATFORM-OWNED OUTPUT** — the builder
never sets it. A `@model_validator` makes the silent-unscheduled state (no schedule, not
on-demand, no ref) **unrepresentable**, so submit refuses a pipeline that would never run.

`AppLifecycle.submit` arms a declared schedule on the **maintainer** session
(`created_by="user"`), stamping a long-lived `expires_at` (`_SCHEDULE_TRIGGER_TTL`, 10y)
**BEFORE `TriggerPolicy.admit`** — the policy stamps its 7-day `default_expiry` only when
`expires_at is None`, so an unstamped app heartbeat dies silently a week in. A spec arriving with
a `trigger_ref` and no schedule has one armed on some other session:
`_rehome_pipeline_trigger` moves it onto the maintainer and cancels the original. A policy-cap
rejection (over `max_armed_per_session`, cron too tight) SKIPS that one trigger and never sinks
the submit — the app keeps the pipelines that fit.

`pause`/`resume`/`archive` are "app == its triggers": every maintainer-owned trigger transitions
in lockstep, identically for an armed and a re-homed one.

## The run lifecycle

All three seams ride existing backend seams (`_trigger_deliver`, `start_async`, the
`on_session_end` hook) — no new engine, no headless executor.

- **Builder kickoff.** `create_draft` persists the `building` draft, mints the builder session,
  then kicks off the build through an injected `AppRunStarter` (a Protocol field;
  `_RuntimeAppRunStarter` in `backend.py` wraps the `_trigger_deliver` idle-start idiom). The
  kickoff message hands the root the intent + workspace + **the assigned `app_id`**, so
  `submit_app` reuses it and the submit reconciles onto the SAME draft row. An unwired run-starter
  is a logged no-op; the draft still persists. The chat-builder path has no `create_draft` and no
  row — `submit` handles both.
- **`submit` reconciles the draft row: identity vs content.** When a row exists for `app_id` it is
  the authority for IDENTITY (`owner_session_id` / `created_at` / `workspace_ref` preserved) and
  `submit` replaces only builder-authored CONTENT. A submit whose existing row is NOT
  `building`/`draft` is REFUSED (`ValueError` → a builder reask) **unless the submitting session
  is server-bound to that app** — the live-overwrite guard, a real data-loss vector when a chat
  builder reuses a live `app_id` belonging to someone else. What it bounds is a DIFFERENT
  session reusing a live id, not an app's own session updating it; scoping it to
  `maintainer_session_id` alone made every composer-opened session fork a second app.
  A same-session double submit therefore now writes v2 rather than erroring — append-only, with
  rollback available, which is the maintainer resubmit's own behaviour.
- **Opening and closing a `PipelineRun` are two DIFFERENT seams, necessarily.** `start_async`
  returns a `run_id` immediately and never observes completion, so `AppPipelineRunTracker`
  (`pipeline_tracker.py`) opens on `_trigger_deliver` — matching the firing `trigger_id` against
  `PipelineSpec.trigger_ref`, with `trigger_id` arriving on the structured `TriggerFireContext`
  payload — and closes on `HookManager.on_session_end`, which is where the run's `error`, the
  outcome `succeeded`/`failed` is classified from, is available.
- **`tools_allowlist` is authoritative least privilege in BOTH branches.** A non-empty
  `PipelineSpec.tools_allowlist` runs under that list + `app_data`; an EMPTY allowlist runs under
  just `["app_data"]`. Both under `strict_tool_scope=True` so built-ins are capped too, threaded
  into `_trigger_deliver`'s `allowed_tools`. An undeclared unattended fire can write its own data
  and nothing else; a pipeline needing a connector, `web_search` or a file read must DECLARE it.
  `pipeline_scope` returns `None` only when the fire is not an app pipeline at all.
- **`on_pipeline_failure`.** The tracker's close hands the app plus a `PipelineIssue` to
  `AppLifecycle.handle_pipeline_failure`: `repair` starts a repair run on the maintainer via the
  same `AppRunStarter`; `pause` calls `lifecycle.pause`; `notify` emits `app_issue` only. TWO
  reasons reach it — a FAILED close, and a SUCCEEDED close that regressed a collection.

## Code pipelines — the execution engine

`PipelineSpec.mode`: `agentic` (default) re-engages the maintainer LLM session on a fired trigger;
`code` runs a deterministic `entrypoint` (`def run(params, ctx) -> Any`) with NO LLM call via
`AppPipelineRunner` (`pipeline_runner.py`).

- **Placement.** Pipeline files live in the SAME `frontend.files` map under `pipelines/…` keys
  (`entrypoint` names one), since `submit_app` reads every bundle file already.
  `PIPELINE_ALLOWED_MODULES` + `lint_pipeline` are canonical in `pipeline_runner.py`; `submit_app`
  routes `mode="code"` entrypoints to `lint_pipeline` and the runner re-runs it at execution, so ONE
  allowlist gates both boundaries.
- **Pipeline source is NOT served to the browser**, and the strip is strategy-on-model:
  `AppSpec.served_frontend_files()` excludes every `mode="code"` entrypoint and anything under
  `pipelines/`, and `_render_spec`/`_render_version` DELEGATE to it rather than re-deriving the
  predicate. It covers the top-level rendered spec AND every `versions[]` snapshot ON THE WIRE, so
  a version echo cannot leak what the live strip withholds. The STORED spec keeps every file — the
  runner reads it.
- **Substrate = in-process `exec` in a curated namespace**, not a subprocess: pipeline code is
  agent-authored under the SAME trust envelope as an agentic run, which already shells out via
  `aider_shell_tool`. Gated three ways — the pipeline lint (import allowlist + the app linter's
  dynamic-exec ban), a curated `__builtins__` (no `open`/`eval`/`exec`/`compile`; a guarded
  `__import__` admitting only the allowlist), and a thread wall-clock watchdog + output size cap
  bounded by `PipelineSpec.timeout_seconds` — whose `le=600` is **deliberately WIDER than the real
  ceiling** (`PIPELINE_TIMEOUT_CEILING_SECONDS`, 240) and is NOT the enforced bound; see
  "Pipeline timeouts" below — with the runner's constructor value as an optional global override. **Watchdog honesty: Python threads are not preemptible, so
  an uncooperative loop LINGERS as a daemon thread** — the watchdog returns control with a `timeout`
  error but cannot kill it. A hard kill needs subprocess isolation.
- **`le=600` is a PARSE bound, not the real ceiling — three-part enforcement, not one number.**
  `PIPELINE_TIMEOUT_CEILING_SECONDS` (240, `models.py`) is the real ceiling: a `mode="code"`
  pipeline invoked over REST runs SYNCHRONOUSLY on the single gunicorn worker
  (`docker/Dockerfile.api`'s `--timeout 300`), so a pipeline honoured at its full 600s could outlive
  the worker and take every in-flight request down with it. The FIELD stays parse-wide because the
  app store is APPEND-ONLY: tightening it would make an already-stored manifest (or an
  `app_versions` snapshot) fail to VALIDATE on a detail read, exactly the
  `ensure_wakeable`/`ensure_unique_pipeline_names` trap. So the real ceiling is enforced at the two
  boundaries that CAN'T strand existing data instead:
  1. **Submit** — `AppSpec.ensure_pipeline_timeouts_fit()` refuses a new/edited pipeline over 240s,
     the same shape as `ensure_unique_pipeline_names`: a plain method `AppLifecycle.submit` calls,
     never a `model_validator`.
  2. **Execution** — `RunPipelineTool.execution_timeout` and `AppPipelineRunner.execute` BOTH clamp
     a stored-but-over-ceiling value down to 240s and log a WARNING naming the app/pipeline when
     they do (a silent narrowing is the fail-open shape this repo forbids). A pipeline already over
     240s in the store still parses and still runs, clamped; only its NEXT submit is refused.
  3. **The tool boundary is a separate, TIGHTER bound, and the asymmetry is deliberate.**
     `submit_app.py`'s `SubmitPipelineArgs.timeout_seconds` is `le=PIPELINE_TIMEOUT_CEILING_SECONDS`
     (240), not 600 — it is a TOOL ARGUMENT, never parsed from storage, so refusing at DEFINITION is
     the ordinary trust-boundary rule rather than the append-only trap the model field is dodging.
     Two spellings of one number, opposite constraints, both correct: never "harmonise" them in
     either direction. `SubmitPipelineArgs` covers the `submit_app` tool path only — a hand-built
     spec, a rollback replay, or any future producer still needs (2) above, which is why (1) exists
     on the model rather than only at this one tool's wire boundary.

  **Why the model field must stay wide, stated as the failure it prevents:** tightening it to 240
  was tried and BROKE PRODUCTION DATA. The store already held a manifest declaring 300, so the
  parse-time bound made that manifest — and its `app_versions` snapshots — fail to validate.
  `MongoAppStore.get`/`get_version` call `model_validate` with no `try/except`, and no errorhandler
  covers a store-read `ValidationError` (every one in `routes.py` guards a REQUEST BODY parse), so
  every route that loads the app first raises. **The sharpest edge: `list_apps` list-comprehends
  `model_validate` over EVERY row with no per-row guard, so ONE bad row takes down the whole
  listing.** The default gallery survives only because its `status != "archived"` filter runs
  BEFORE validation — that is luck, not design, and it stops being luck the moment anything
  archives an affected app. The full suite was green throughout, because no test constructed a
  spec from a STORED over-ceiling value. Same ruling as `ensure_unique_pipeline_names`: validate
  new data at the boundary it crosses; stored history re-crosses the parse seam under the contract
  it was written with.
  - **`list_apps(include_archived=True)` is the sharpest edge of the "field stays wide" choice.**
    `MongoAppStore.list_apps` list-comprehends `AppSpec.model_validate` over every row with NO
    per-row guard, so one pipeline over 600 (impossible once submit refuses new ones, but not
    impossible for a value written before this design existed) or any OTHER validation failure in
    ANY row takes the WHOLE listing down. The DEFAULT gallery listing is spared only because
    `{"status": {"$ne": "archived"}}` filters an archived row out BEFORE validation ever runs —
    that is luck, not a guarantee, and does nothing for `include_archived=True` or for a live/paused
    app that happens to hold a bad row.
- **`ctx` surface:** `ctx.params`/`ctx.now`, `ctx.glob`/`ctx.read_file` (workspace-scoped,
  traversal-guarded — resolved UNDER the workspace root only),
  `ctx.collection(name).upsert|query|delete`, `ctx.llm(...)`, `ctx.exec(...)`. Writes ride the
  REAL `AppDataStore` with `collection_spec=` and `max_docs=`, so schema validation and the cap
  are the SAME enforcement seam `app_data` uses. `dry_run` exercises the identical path but
  performs NO durable write and COUNTS what would write.
- **Failure is RAISED (`PipelineExecutionError` with a `code` bucket), never encoded in the
  result** — `PipelineResult` (`{output, evaluated_at, cache, docs_written}`, frozen) only ever
  represents success.
- **Two methods for two clients:** `execute(app, pipeline, params, *, now, dry_run) ->
  PipelineResult` (object-keyed — `routes.py` and the fire seam) and `run_pipeline(app_id,
  pipeline_name, *, params, dry_run) -> dict` (id-keyed — the `run_pipeline` SessionTool's
  `PipelineRunner` Protocol; maps `cache` → `cache_hit`, uses the injected clock). `app_store` DI
  exists to resolve the id-keyed seam.
- **Submit validates a `mode="code"` entrypoint is IN the bundle**
  (`AppLifecycle._validate_code_pipelines`): the model enforces "entrypoint present IFF
  mode='code'" but cannot see the bundle, so this cross-field check is an actionable reask at
  submit. `init_apps` constructs the runner, injects `_resolve_app_workspace_cwd` (reusing the
  maintainer session's own cwd resolution), calls `register_pipeline_runner(runner)`, and hands it
  to the tracker.
- **A trigger fire on a code pipeline runs the ENGINE, not the LLM.** `_trigger_deliver` calls
  `AppPipelineRunTracker.run_code_pipeline_fire(trigger_id)` FIRST; a `mode="code"` fire executes
  synchronously, writes ONE closed `PipelineRun {kind:"scheduled"}` with the result
  (`params_hash`/`cache`/`docs_written`), dispatches `on_pipeline_failure` on a raise, and returns
  `True` — **the maintainer session is never re-engaged**, which is the point. Agentic, non-app or
  unwired-runner fires return `False` → the unchanged agentic path.

### Caching — `cache_mode: "ttl" | "source"`

`"ttl"` (default) is time-driven, keyed `(app_id, version, name, sha256(params))` with
`cache_ttl_seconds` (0 = never); a hit does no writes.

`"source"` is the live tier: `PipelineContext` records every path read or globbed, and the runner
persists that manifest plus a stat FINGERPRINT (`sha256` over sorted `(path, mtime_ns, size)` +
the glob RESULT sets — stat-based, NO content hashing) in a dedicated `_source_cache`. A later
`execute()` re-stats and re-globs the manifest and serves the cache IFF the fingerprint matches. A
NEW file matching a recorded glob busts it (the result list is part of the fingerprint), as does a
touched mtime or size; the first run has no manifest and always executes. `"source"` IGNORES
`cache_ttl_seconds` — liveness is stat-driven, not time-driven. `dry_run` never serves or
populates it.

`ctx.glob` and the fingerprint share ONE `_glob_under_root` resolver so a recorded glob re-globs
identically, and `ctx.glob` records `{pattern: resolved list}` at glob time so the post-run
fingerprint consumes those results with no re-walk. The pre-run check against a PRIOR manifest
still re-globs — inherent, since a NEW matching file can only be found against the live
filesystem. `execute()` computes the prior fingerprint ONCE and reuses it as both the hit-check AND
the `ctx.llm` cache salt.

### `ctx.llm` — the bounded LLM step

ONE schema-shaped round-trip returning a dict validated against `output_schema` (jsonschema; ONE
retry with the validation error appended, then `PipelineExecutionError("llm", …)`).
**`output_schema` MUST have root `type:"object"`**, validated at entry BEFORE any model call — the
synthesis seam returns an object, so a non-object root only wastes a round-trip.

The runner takes a DI'd `llm_invoke: Callable[[str, dict, int], dict] | None`;
`backend.py:_apps_llm_invoke` wires a thin in-process adapter over
`mewbo_api.structured.synthesis.SynthesisRunner` — **not** a fresh `build_chat_model` client and
never HTTP-to-self. Every `ctx.llm` call is therefore a session-backed, Langfuse-traced structured
run with the observability an interactive synthesis run has. It stamps `surface="apps-pipeline"`
(→ a distinct `source_platform`) so pipeline spend is filterable; `structured:fast` is reused as
the base tag because a custom `session_type` would mean bypassing `SynthesisRunner`'s hardcoded
tag AND teaching core's provenance classifier a new prefix, else it reclassifies to the `user`
origin. Unwired ⇒ a clean "not configured" error.

Budget is DECLARED per-pipeline (`PipelineSpec.llm_budget_tokens`; `0` = llm FORBIDDEN).
**`PipelineContext.llm_tokens_spent`** — the ctx owns the state, not a boxed closure — tracks
cumulative `max_tokens` per RUN and the runner refuses a call past the cap; a cache HIT costs no
budget. Results are cached content-addressed by `sha256(prompt+schema[+source-salt])` in the
runner's `_cache` (slot sentinel `__llm__`); under `cache_mode="source"` the key is SALTED with the
run's source fingerprint so a source change busts a stale answer even when the prompt is
byte-identical. `ctx.llm` IS allowed under `dry_run` — it cannot mutate collections and the builder
needs to test it, so budget is still charged and a cache HIT still served — **but a dry run never
WRITES the llm cache**: a preview must not mint an entry a later real, charged run would serve
un-provenanced.

**The budget bounds REQUESTED tokens and the CALL COUNT, not metered spend.** A schema-reask
retries the model on a round-trip the budget never charges, and the synthesis seam accepts no
per-call token cap, so `max_tokens` drives accounting but is not threaded into the round-trip. The
real spend bound is `timeout_seconds` plus the call-count cap.

### `ctx.exec` — controlled CLI/network egress

The sandbox's default posture is NO subprocess, NO network. `ctx.exec(argv, *,
timeout_seconds=None)` is the narrow DECLARED opt-in that lets a `mode="code"` pipeline shell out
to a vetted binary instead of being forced into `mode="agentic"` just to reach `aider_shell_tool`.
Two `PipelineSpec` fields, both empty by default: `allow_exec` (a subset of
`models.PIPELINE_ALLOWED_EXEC = {git, tea, gh}`) and `allow_egress` (bare hostnames, validated
against `_HOSTNAME_RE`). A `model_validator` rejects either on a `mode="agentic"` pipeline — an
unreachable grant that reads as capability is a lie an audit has to re-derive.

**The AUTHORIZATION rule is a method on the model (`PipelineSpec.check_exec_allowed(argv)`, argv
as a method ARG), and the I/O edge is one atomic class (`PipelineExecutor`) with the
pipeline/workspace/redactor injected as fields.** Neither is a free function.

- **`git` is SHAPE-GATED because of a reachability asymmetry.** Declaring `git` is otherwise
  equivalent to granting a shell: `git -c alias.x='!sh -c …' x` executes, as do `core.pager`,
  `protocol.ext`/`ext::`, `--upload-pack`, `--exec-path`. The "same trust envelope" argument that
  justifies the in-process `exec` substrate does NOT cover it, because **the GET pipeline-invoke
  route is reachable by a browser holding a short-lived app READ token** while `aider_shell_tool`
  needs an agent session behind the master key. So `PipelineSpec._check_git_shape` permits only
  read-shaped subcommands (`_GIT_SUBCOMMANDS`) and rejects `_GIT_BANNED_FLAGS` plus any `::`
  token. **Both halves are load-bearing** — banning the flags alone still leaves
  `git config alias.x '!sh'` then `git x`, two ordinary calls. A pipeline needing an unlisted
  subcommand is refused until the list grows; that cost is the point.
- **`allow_exec` is a DECLARATION, not a sandbox.** `tea`/`gh` are not shape-gated and no argv
  inspection bounds what a binary does once it runs. The value is an explicit, auditable,
  closed-by-default declaration. Documenting it as a boundary against a hostile pipeline would be
  the lie that gets relied on.
- **`allow_egress` guards argv-shaped remotes ONLY.** It cannot constrain a bare remote name
  (`git fetch origin` — the host lives in `.git/config`), a submodule URL, or `gh`/`tea`, whose
  host comes from auth state. A real egress boundary needs a network namespace or a proxy.
- **In host extraction a skipped token fails OPEN — the asymmetry to watch for in any extractor.**
  Two shapes make it concrete. git's scp remote is `[user@]host:path` with the user part OPTIONAL,
  so matching only tokens containing `@` lets `git ls-remote example.com:repo.git` reach any host.
  And `urlparse` returns hostname `None` for `-c remote.x.url=https://evil.example.com/r.git`,
  because the scheme candidate `remote.x.url=https` contains `=` — the token is skipped and the
  call ALLOWED. Hence `_URL_HOST_RE` with `finditer` (one token can carry two hosts, as
  `url.<a>.insteadOf=<b>` does) rather than a parse. `_SCP_REMOTE_RE` requires a DOT in the host,
  or every ordinary refspec (`HEAD:refs/heads/x`) reads as a host and is refused, whose only
  workaround is training authors to declare junk in `allow_egress`.
- **The env is INHERITED IN FULL, deliberately** (`dict(os.environ)` plus a prompt/pager overlay;
  `git` routes through the shared `hardened_git_env`). The credential posture depends on
  inheritance — an SSH agent needs `SSH_AUTH_SOCK`, `tea`/`gh` need `HOME`. Stated rather than
  hidden: an allowlisted binary sees the API process's environment.
- **Inheriting `HOME` is not reaching it.** Under `agent.shell_sandbox` the Landlock scope grants
  sibling DIRECTORIES at each ancestor level, so `~/.config` resolves and `tea`/`gh` keep their
  logins, while loose files at `$HOME` root (`.gitconfig`, `.git-credentials`) do not and `$HOME`
  itself is not listable. Costless for git — its global config is blanked anyway (below) and its
  argv already disables the credential helper. **UNVERIFIED:** whether an SSH agent still resolves.
  Its socket normally sits outside the denied set, but that is reasoning about the scope, not a
  measurement of it.
- **Inheriting the ENV is not inheriting git CONFIG.** `hardened_git_env` blanks the global and
  system git config, so a pipeline's `git` reads only the repo's own `.git/config` — `tea`/`gh`
  are untouched and keep their ambient state. This is the env-side half of the `-c` ban:
  `allow_egress` vets argv, and an operator's global `url.<a>.insteadOf=<b>` then rewrites the
  remote afterwards, so an argv naming a declared host silently reached an undeclared one
  (verified: `git ls-remote https://declared.example.com/x.git` under such a config contacts
  `elsewhere.example.com`). The cost is that a pipeline relying on a host-level `insteadOf` or
  `http.*` override now sees the un-rewritten URL. It costs NO commit identity — `_GIT_SUBCOMMANDS`
  is read-shaped, so `git commit` is refused at authorization, as is the `git -c user.email=` form
  that would supply one; a `GIT_AUTHOR_*` default here would guard an unreachable path.
- **git argv carries `-c credential.helper=`**, matching `build_clone_command` /
  `build_ls_remote_command`. Without it a pipeline's git call is the one path that re-opens the
  EBUSY-masks-auth trap `mewbo_graph/CLAUDE.md` documents.
- **Bounds:** cwd is ALWAYS the workspace root; the per-call timeout is clamped by the pipeline's
  `timeout_seconds`; the child runs in its own session and is killed as a PROCESS GROUP so a spawned
  helper dies with it; output is truncated on a real BYTE budget, because a `str` slice counts
  codepoints and overshoots ~4× on multi-byte output. stdout, stderr **and the timeout message** are
  redacted — the timeout text embeds argv, which can carry a credential-bearing URL, and it lands
  verbatim on the `PipelineRun.error` row `/system` renders. A non-zero exit is NOT an error (the
  pipeline decides what a failed `git` means); only a refusal, a missing binary or a timeout raises.
- **REFUSED under `dry_run` (code `dry_run`), unlike `ctx.llm`**, which cannot mutate the world.
  `AppLifecycle.submit` dry-runs EVERY code pipeline as its verifier, so admitting exec would let
  merely SUBMITTING an app push to a remote nobody asked it to touch. `_verify_pipelines` classifies
  `dry_run` alongside `params`/`workspace` as a verifier ARTIFACT, so a shelling-out pipeline still
  verifies cleanly.
- **Credential honesty:** `ctx.exec` resolves and injects NO stored credential (unlike
  `wiki_clone_repo`'s `resolve_chain`) — it rides ambient state: an SSH agent, a `tea login`/`gh
  auth login` session. **Not a git `credential.helper`**, which the flag above disables, so an HTTPS
  git remote has NO credential here and only SSH-agent auth resolves one. Reusing
  `hardened_git_env` but NOT `run_git_with_chain` is correct rather than a DRY gap: the chain
  iterates a `CredentialStore` keyed by `store`/`slug`, neither of which a pipeline's `git log` has.

## On-demand fire, go-live seed, re-arm

ONE fire seam, `AppPipelineRunTracker.fire_pipeline(app, pipeline, *, now)`, with three callers:
`POST /apps/<id>/pipelines/<name>/fire`, the go-live seed in `AppLifecycle.submit`, and `rearm`'s
optional seed.

- **Fire is read-auth on purpose; rearm is key-only.** A served app refreshing itself via the SDK's
  `pipelines.refresh(name)` is deliberate (same ruling as the GET invoke), bounded by the agentic
  cooldown; `/rearm` is an operator repair and rejects app tokens outright.
- **`mode="code"` fire = `record_code_run(kind="on_request", dispatch_failure=False,
  require_effect=False)` — an explicit fire ALWAYS ledgers**, deliberately unlike the REST invoke's
  `require_effect=True`. One primitive, two effect policies: an invoke is a render (spam-prone), a
  fire is a refresh (provenance-worthy).
- **`mode="agentic"` fire: guards in order (live → maintainer present → open-run 409 → 300 s
  cooldown 429), then OPEN the ledger row, THEN wake.** The prompt is `pipeline.wake_prompt` read
  off the SPEC — never dereference `trigger_ref`, since an on-demand or never-armed pipeline has
  none, and that independence is what lets fire rescue stuck apps. Cooldown is strategy-on-model
  (`PipelineRun.cooldown_remaining`, latest `started_at` of any kind/status) and agentic-only; code
  fires are cache-protected.
- **The check-then-open is serialized by the tracker's `_open_lock` across BOTH open seams**
  (`open_run` scheduled + `_fire_agentic` manual). Unserialized, two gthread workers double-open
  and `close_runs` settles only the newer row — the older stays `running` forever and 409s every
  later fire until the restart sweep. In-process locking suffices because prod is gunicorn with a
  SINGLE worker; the one-process invariant carries it, not the tunable thread count. A second
  worker process would need a store-level unique-open primitive.
- **A refused wake is a 409, never a 202.** `start_app_run` returns `"started"|"steered"|"refused"`
  and can refuse when a run ends between `is_running` and the steer, or on a concurrent-start
  rejection; `_fire_agentic` then closes the just-opened row `failed` and returns `wake_refused`.
  The 202 wire carries ONLY `started|steered` — the console's union relies on it.
- **Go-live seed: `submit` fires every pipeline once, best-effort async** (code on a daemon thread,
  agentic inherently fire-and-forget), guarded on `lifecycle.tracker` being wired, so freshness is
  never born "never". An agentic seed leaves an OPEN on_request row, so an immediate manual fire of
  the same pipeline correctly 409s/429s until `close_runs` settles it. Seed failures log and never
  raise into submit; seed writes touch only `run_store`, never racing submit's `app_store` version
  persist.
- **`rearm` re-reads the manifest UNDER its per-app lock — the re-read is the cure, not the lock
  alone.** A racing rearm that already committed must be seen, else the lost update leaves an
  orphaned armed trigger double-firing with no sweep to catch it. A non-None, non-armed,
  non-terminal `trigger_ref` (the paused-then-resumed case) is CANCELLED before its replacement is
  minted. Response `{armed, unchanged, seeded}`; `seeded` lists pipelines the seed was ATTEMPTED
  for, a refusal included.

## The submitter seam

Plugin tools are built through the ordinary `SessionToolRegistry` manifest path, which feeds a
constructor only `session_id` + `event_logger`.

- **`app_data`** resolves its three stores from the process-wide factories — no wiring.
- **`submit_app`** has no store factory (the `AppLifecycle` is constructed once in `backend.py`),
  so it resolves its submitter through the down-only `register_app_submitter(lifecycle)` push in
  `init_apps()`, mirroring `register_builtin_root`. That is the ONE line of runtime wiring the
  plugin needs; miss it and `submit_app` returns a clean "apps runtime not configured" error, not a
  crash. Do NOT also inject `submit_app`/`app_data` via `extra_session_tools` — a second instance
  shadows the wired one, since dispatch is first-by-id.

## Plugin suite layout

`plugin/` IS the suite: its manifest sits at `plugin/.claude-plugin/plugin.json`, not in a
sub-suite. So `backend.py:init_apps` registers `PLUGIN_ROOT.parent` (the `mewbo_api.apps` package
dir) with `register_builtin_root`, because `discover_builtin_plugins(root)` scans a root's
IMMEDIATE subdirectories for `<suite>/.claude-plugin/plugin.json`. `init_apps` fires a loud startup
warning when discovery finds nothing, so a layout drift fails visibly instead of shipping an apps
deployment with no app-builder AgentDef. `plugin/__init__.py` holds ONLY `PLUGIN_ROOT`.

## Capability flow — `apps` is a string three sides agree on

There is no central enum of capability ids (core CLAUDE.md → "Capability gating"). `apps` is real
only because three sides name it: the console/Aura clients ADVERTISE it (`X-Mewbo-Capabilities`),
the plugin manifest GATES on it (`requires-capabilities: ["apps"]`), and the lifecycle STAMPS it on
every app agent session (`client_capabilities: ["apps"]`, builder and maintainer alike). That stamp
is load-bearing: a trigger-rewoken maintainer sees the `app-repair` AgentDef and the `app_data`
tool only because its session carries `apps` and `SessionToolRegistry.build_for` surfaces a
capability-gated tool to a session holding the capability with NO explicit allowlist — the
maintainer's shape.

## `workspace_ref` → agent session scope

`AppSpec.workspace_ref` points at the SAME project primitive ordinary sessions anchor to, never a
new workspace entity. `AppLifecycle` maps it onto the builder + maintainer context only — the
frontend never inherits workspace capability.

- **`kind="shared"`** → the `key` IS an existing project key (config name or `managed:<id>`),
  emitted as the session `project` context field, so a trigger fire resolves the maintainer's
  cwd/MCP scope through `_resolve_session_cwd` exactly like a console-created session.
  **`AppLifecycle._validate_workspace_ref` enforces that at the SUBMIT boundary** through the
  DI'd `ProjectCatalog` (`project_catalog=None` ⇒ skipped, logged once), with `resolve` rather
  than `find` — `find` accepts a key that is listed but has no usable directory. **The predicate
  it must match is `backend.py:_resolve_project_cwd`, NOT `catalog.resolve`**: that runtime
  recovers from an `unavailable` MANAGED project or worktree by creating the directory Mewbo
  owns, so refusing there would make submit stricter than the thing it predicts and block a
  legitimate app. That one arm is mirrored (decision only — the validator never `makedirs`);
  every other code still refuses, `unavailable` on a CONFIGURED project included, since that
  path is the operator's. **The mirror is a SECOND COPY and a cost**: the cure is for the
  recovery to live on `ProjectCatalog` so both callers read one implementation. It is a
  submit-boundary METHOD, never a `WorkspaceRef` `model_validator` — same append-only reason as
  `ensure_unique_pipeline_names`: stored snapshots already holding a bad key must keep parsing.
  Two namespaces with nothing comparing them is how an APP ID became a session's `project`, and
  the failure was quiet on the other side too — `backend.py:_resolve_session_cwd` swallowed the
  refusal and the maintainer ran in an empty scratch dir forever; it now logs and still falls
  through.
- **`kind="own"`** → isolated default scope: NO `project` key (the session runs in its default temp
  cwd), tagged to the app by `app_id` alone. There is no "own-workspace" primitive to compose, so
  none is invented; do not fake a private workspace by minting a managed project here.

## SDK delivery — server-side, at the render seam only

The served stlite frontend `import mewbo_app`; the backend injects that SDK source rather than the
console/Aura bundling it. `_load_app_sdk_files()` reads `plugin/sdk/mewbo_app.py` once at startup
and caches it on the controller (`sdk_files`); `AppsRoutesController._render_spec` makes TWO edits
to the RENDERED `spec.frontend.files` at the `_detail` seam (GET / PATCH / rollback all return
`AppDetail` through it), in order:

1. **STRIP pipeline source**, delegating to `AppSpec.served_frontend_files()`. Unconditional (a
   security concern), and applied to every `versions[]` snapshot on the wire via `_render_version`
   so a version echo cannot leak what the live strip withholds.
2. **INJECT the SDK**, which wins a name collision so the sanctioned SDK always runs. Opt-in on
   `sdk_files`, and NOT applied to history — history renders read-only and never runs, so the SDK
   is upgradable without a version bump.

The STORED `AppSpec` and its version history keep every file: the STORE retains the pipeline source
for the runner, the WIRE does not. A missing SDK file logs loudly and degrades to no injection —
served apps fail their `import mewbo_app` visibly, never a server crash.

## Write-back — `user_writable` pipelines + the write-scoped token

The one write path a served frontend has: `app.pipelines.submit(name, params)` (the SDK) →
`POST /api/apps/<id>/pipelines/<name>`, params in the JSON body so object/array values work, unlike
the scalar-only GET `run`. Least privilege stays STRUCTURAL:

- **`PipelineSpec.user_writable` declares a `mode="code"` pipeline's params are user form input.**
  `submit_app`'s `SubmitPipelineArgs` passes it through (validator: `user_writable` requires
  `mode="code"`), and the SAME floor is a `PipelineSpec` `model_validator`. That floor can live on
  the model — unlike `ensure_wakeable` — only because the field defaults `False`, so no stored
  snapshot can hold `user_writable=True` and fail to parse. No path (chat-builder, rollback replay,
  hand-built spec) can persist a `user_writable` agentic pipeline that would mint an unusable write
  token.
- **A WRITE token is mintable ONLY for an app that declares one.** `mint_token` gates
  `scope="write"` two ways: the literal master token (`require_master_token`) AND
  `app.has_user_writable_pipeline` (a strategy-on-model property; else 403).
- **The POST/form invoke additionally requires the TARGET pipeline be `user_writable`.** The write
  token is APP-scoped, so `invoke_pipeline` gates the POST path (`json_body`) on
  `pipeline.user_writable` (else 403): a write credential invokes a form pipeline, never a sibling
  effectful one on the same app. That also collapses the stale-token-after-despec risk with no
  extra bookkeeping — a pipeline flipped to `user_writable:false` 403s there, a removed one 404s.
  The GET path stays open to ANY code pipeline including effectful ones (a served app refreshing
  itself is deliberate), so `user_writable` gates the POST/form path only, never GET-reachability.
- **The console mints write IFF the spec declares a `user_writable` pipeline** and injects the
  token's `scope` into `_app_context.json`; the SDK reads it (absent ⇒ `read`) and raises a clear
  client-side error on `submit` from a read-token page rather than a silent 403. Aura mints
  read-only and injects no `scope`, so an Aura-rendered form fails the same client-side check;
  write-back from Aura needs Aura to detect the user_writable app, mint `{scope:"write"}`, and add
  `scope` to its `mewbo-app-payload` `app_context`.

## Routes

- **A Resource must carry `add_resource()` registration ONLY, never also an `@apps_ns.route(...)`
  decorator.** The decorator copy registers WITHOUT `resource_class_kwargs`, so the controller is
  never injected and every request 500s. `add_resource` in `init_apps_routes` is the sole
  registration (the `triggers/routes.py` idiom). Controller-direct tests structurally cannot see
  this; `tests/apps/test_apps_routes_flask.py` (Flask test client) is the guard.
- **`/system` is the ONE introspection surface** — the console, Aura and the injected SDK all read
  `GET /api/apps/<id>/system` (`{app_id, status, freshness, triggers, runs, maintainer, pipelines,
  unscheduled_pipelines}`); do not split it into granular sub-routes. `pipelines` carries the
  declared per-pipeline tier (`{name, schedule: <union|null>, on_demand, trigger_ref, armed}`) so a
  client renders "refreshes hourly" vs "on-demand" vs the unscheduled warning.
  `unscheduled_pipelines` is LOCKED with the console (derivation: `trigger_ref` not armed), so an
  on-demand pipeline appears there and the console suppresses the false warning via
  `pipelines[].on_demand`. Do not "fix" `unscheduled_pipelines` to exclude on-demand — that is the
  console's job with the other field.
- **Error envelope = a top-level `message`.** `_error()` returns `{"message": ...}` (the
  `ApiResponseKit` `shape="message"` decorators document it) so the console's `readJson` reads
  `data.message`, matching `agentic_search`. `ApiResponseKit` has no generic runtime error builder
  (only the 410 `terminated_response()`), so the shape is produced directly; do not invent a nested
  `{error:{...}}` here.
- **The data plane is READ-ONLY at REST.** `AppData` / `AppSystem` implement only `get`; any write
  verb 405s. Writes happen agent-side through the schema-validated `app_data` SessionTool.
- `AppData.get` declares `O(collection)`, bounded: at most `_MAX_DATA_LIMIT` (500) documents per
  call, walked with `offset`. `read_data` fetches `limit + 1` purely to set `truncated`.

### A bound that cannot be honoured must be SIGNALLED or REFUSED, never silently narrowed

A clamp that still returns 200 is a fail-open in the same family as the `?after=` filter fail-open.
**The compounding interaction is the non-obvious part: `AppDataStoreBase.query`'s default sort is
`updated_at` DESC, so a truncated read drops the OLDEST-WRITTEN rows, and a pipeline that ingests
in alphabetical order therefore loses its alphabetically-EARLIEST keys first.** Measured: a
586-document collection asked for `limit=1000` returned exactly 500 and seven whole ingest groups
vanished from the app, having been written alphabetically-early; a 2000-request page lost 101 of
181 families. A small test collection never trips this — the loss appears only past the cap and
reads as missing SOURCE data, not a read bug.

**Mirror one of the two honest precedents in this package rather than inventing a third shape:**
`plugin/get_app.py`'s `count_capped` SIGNALS a cap was hit; `plugin/app_data.py`'s Pydantic
`le=_MAX_QUERY_LIMIT` REFUSES a limit it cannot honour at the boundary, before any read. A route or
store cap is fine — the cap is not the defect — but it must be one of those two.

## Health honesty — "succeeded" is not "did its job"

Deriving health from `stale = last_run.status != "succeeded"` misses the failure a user notices: a
run that writes 1 doc to one collection and 0 to the collection every frontend page reads closes
`succeeded`, every signal stays green, and the dashboard renders empty. `PipelineRun.wrote_nothing`
does not catch it either — it asks whether the run wrote ANY doc anywhere.

`PipelineRun.unwritten_collections(declared_collections)` is the predicate that does:
succeeded-only, declared names minus the keys in `docs_written`. It takes the declared names as a
METHOD ARG, since the run row does not know the app's collections and models never reach for I/O.
Surfaced on the run payload and `/system` freshness as a NEW sibling field; **`stale` keeps its
exact prior meaning**, because silently repurposing a shipped field breaks clients invisibly.

Two bounds: it is **latest-run-only** ("current behavior misses a declared collection", not "this
collection has ever been empty"), so a pipeline that stops targeting a collection it once wrote
fires this on every poll; and `wrote_nothing` is mirrored into no console type and rendered
nowhere, so it stays backend-log-only.

## Auto-repair on an integrity violation

`PipelineRun.new_integrity_violations` routes the violation through the SAME
`on_pipeline_failure` dispatch a raising run uses — one dispatcher, one policy enum, one repair
path, fed a second KIND of reason.

- **The run stays `succeeded`; the violation is a SEPARATE axis from run status.** Marking it
  `failed` would corrupt `stale`/`last_success_at`/freshness. So the reason travels beside the
  status as a **`PipelineIssue`** (`models.py`) rather than a bare `error: str | None`. Two shapes
  (`run_failed`, `unwritten_collections`), each owning its `describe()`/`repair_brief()` prose,
  because a repair agent told only "something went wrong" hunts for an exception that never
  happened. `_repair_prompt` composes only the PROCEDURE and delegates the DIAGNOSIS to the issue.
- **Anti-spam is TWO rules, both on the model, both load-bearing.** (1) *Regressed, not
  never-populated*: a collection counts only once some earlier succeeded run of THIS pipeline
  actually wrote it, so a first-ever run and an app whose upstream is legitimately empty dispatch
  nothing, forever. (2) *Edge-triggered, not level-triggered*: a violation the immediately preceding
  succeeded run already reported is subtracted, so one break dispatches ONCE — including when the
  repair it spawned did not fix it. Suppression is per-collection (a NEW regression alongside an
  ongoing one still gets through) and the edge RE-ARMS after a recovery. Visibility stays
  level-triggered on `/system`; only the ACTION is edged.
- **The baseline is the LEDGER, deliberately not a live `AppDataStore` read** — the more direct
  question ("does this collection hold docs?") is the trap. Collections are declared APP-wide while
  runs are per-PIPELINE, so a store read cannot attribute a collection to the pipeline that feeds
  it: in a two-pipeline app every run of A would report B's collection as regressed, forever. The
  ledger baseline is pipeline-attributed by construction and needs no I/O.
  `INTEGRITY_HISTORY_LIMIT` (20) bounds the scan — a bound, not a tuning knob, and deliberately not
  a time window, since the question is ordinal and a quiet week must not empty a slow pipeline's
  baseline.
- **`dispatch_failure` is reused verbatim, so the manual-fire law holds for free.** A scheduled
  fire dispatches; a REST invoke and a manual `/fire` do not — a user hammering a broken pipeline
  must never auto-repair or auto-pause. The agentic close seam gates on the same asymmetry via
  `kind == "scheduled"`.
- **An integrity issue ALSO emits `app_issue` under EVERY policy** (`needs_own_event`), unlike a
  failure: a failed run is already user-visible as a failed ledger row, while an integrity
  violation's row is a green `succeeded`, so without the event `repair`/`pause` would act on
  something the user was never shown. The payload keeps its `{app_id, error}` shape and gains
  `kind`/`pipeline`/`collections` additively. No client consumes `app_issue`, so a violation
  reaches a user through `/system` and the backend log.

### The repair wake — three concerns, three homes

A repair fires into a FRESH maintainer context holding no memory of the app and no staged files, so
a prompt naming only the problem burns its first turns rediscovering its own toolbox:

| Home | Owns |
|---|---|
| `PipelineIssue.repair_brief()` (`models.py`) | The FACTS — which pipeline, which collections, whether anything raised. Varies per dispatch, so it belongs to the data |
| `AppLifecycle._repair_prompt` (`lifecycle.py`) | The SURFACES — `get_app` get/**stage**, `run_pipeline(dry_run)`, `app_data`, `submit_app`, "delegate via `spawn_agent`". A pointer, never the advice |
| `plugin/agents/app-repair.md` (AgentDef) | The durable HOW — hypothesis set, ordering, verification bar. Reached identically when a USER reports "my dashboard is empty" and no dispatched prompt exists |

The hypothesis list belongs in the AgentDef: it is the same advice for both entry paths, so a copy
in a prompt string is a second copy to drift. `get_app`/`stage` leads the surface list because
staging is ephemeral and normally already gone when a repair fires, and it is the ONLY route back
to a `mode="code"` pipeline's SOURCE.

**The AgentDef must carry the no-exception case.** A diagnosis instruction of "read the ledger
error" mis-teaches it: for a green run there ISN'T one, since a run that raised would have closed
`failed`. It points at the ABSENCE of writes as the evidence (`get_app` doc_counts + `app_data`
query) and carries the four causes — source resolving to nothing, **a swallowed error** (the
highest-frequency one, and the reason the run reports success at all), a filter excluding every
row, a collection renamed on one side only. Verification bar for this case: **a clean `dry_run`
does NOT prove the fix**, because a clean dry run is exactly what the broken pipeline already
produced — the count must be non-zero.

The AgentDef declares `disallowedTools: [spawn_agent, exit_plan_mode, activate_skill]`, so it
**structurally cannot activate a skill**; never instruct it toward one.

## Policy enforcement

- **`max_docs_per_collection` is enforced at the STORE.** `AppDataStore.upsert` takes an additive
  `max_docs` kwarg; a NEW key breaching the cap raises `CollectionCapExceeded`, while an UPDATE to
  an existing key is always allowed — the store alone knows new-vs-existing plus the live count, so
  it owns the cap. `app_data` passes `app.policies.max_docs_per_collection` and maps the exception
  to a clean agent-visible `code:"cap"` error the agent self-corrects on.
- **`on_pipeline_failure` is enforced at the run-tracker close seam** — see "The run lifecycle" and
  "Auto-repair on an integrity violation".
- **`retention_days` is UNENFORCED, an honest gap.** `AppPolicies` carries it and the manifest
  stores it, but nothing sweeps expired docs; that needs a background reaper (a trigger-driven
  maintenance pipeline or a store TTL index). There is no trivial enforcement point, since an
  upsert cannot reason about age-based deletion of OTHER docs.

## `get_app` — agent-facing introspection + recovery

The only update path is "resubmit the WHOLE app" (`submit_app` reads the entire bundle off disk),
so without a read surface a fresh maintainer context that no longer holds the staged frontend files
cannot update the app at all. `plugin/get_app.py` provides `get` (the live manifest, no file
bodies) and `stage`.

- **Resolution mirrors `run_pipeline`: no `app_id` argument.** The app is whichever one this
  session owns (`owner_session_id`) or maintains (`maintainer_session_id`) — structural cross-app
  protection, not a runtime check. A session bound to no app reads a uniform `not_found`, no
  existence leak, as with `app_data`/`schedule_trigger`.
- **`stage` is the canonical recovery path.** It re-materializes the RAW `spec.frontend.files`
  (every frontend file AND every `mode="code"` pipeline SOURCE — never the browser-served
  `served_frontend_files()` projection, since a maintainer editing a pipeline needs its source)
  into `<apps_root>/<session_id>/<app_id>/`. The staging directory is EPHEMERAL and must never be
  assumed present across turns or a restart. Confined twice: the app dir must resolve under this
  session's dir (the model's validator bans only `:`, not `/`/`..`, so a hostile stored `app_id`
  could otherwise relocate within the apps root), and every file path must resolve under the app
  dir.
- **`trigger_declared` is an honesty split, not a naming accident.** It reports whether the manifest
  carries a platform-stamped `trigger_ref` — DECLARED state, resolved only from the stores this tool
  holds. `/system`'s `armed` checks the LIVE trigger store, and the two DIVERGE for a paused or
  broken app (the ref persists on the manifest while its trigger is paused/cancelled), which is
  exactly the state a repair agent must see unconflated. `doc_count` carries the same discipline:
  no store `count()` exists, so it is `len(query(..., limit=1000))` with a `count_capped` flag
  rather than a silently-wrong exact number.
- **Terminal-free** (the `app_data`/`run_pipeline` shape): reading or staging is normal iterative
  work, never a run exit — `should_terminate_run`/`terminal_reason` are defined explicitly, since
  `SessionTool` is a structural Protocol with no inherited bodies.

## Reaching the maintainer session — `POST /apps/<id>/session`

`submit` mints (or reuses) the maintainer session as a side effect of going live, and
`handle_pipeline_failure`'s repair wake can reach it directly off `app.maintainer_session_id` —
but before this route, nothing else could. An operator, a console "open session" action, or any
caller with no in-flight repair had no way to open a conversation with an app's own maintainer.

Resolution order, `AppLifecycle.get_or_create_maintainer_session`: an existing, non-terminated
`maintainer_session_id` wins; else an unsubmitted draft's `owner_session_id` (the builder
session — reused, never replaced, see below); else a fresh mint through the SAME seam `submit`
uses, persisted onto the manifest.

**Why a plain new session cannot substitute — the trap.** `submit_app`/`run_pipeline`/`app_data`
resolve their app by matching the CALLING session's id against `maintainer_session_id` /
`owner_session_id`, and by nothing else. A session minted any other way — however it is scoped,
however faithfully it copies the `apps` capability stamp — is invisible to those tools until its
id actually lands on one of those two fields. **The `app_id` CONTEXT key resolves nothing
anywhere and must never be read as authorization**: it is merged verbatim from a request
(`backend.py:_build_context_payload`) and re-writable on any later turn, so it addresses an app
without proving anything about it — the same ruling the wiki tier carries for `slug`.

**`get_app` is the ONE exception, and its tier is the TAG.** `AppStagingArea.app_for_session`
falls back to the server-stamped `app:<app_id>[:<session_id>]` tag, parsed through the core
grammar (product `apps`, facet `app_id`) rather than prefix-matched. That is what lets an
ADDITIONAL session opened against an app (below) read and stage it.

### `new_session` — an additional session, read-plus-stage

`POST /apps/<id>/session` takes an optional `{"new_session": true}` (`AppSessionRequest`,
`extra="forbid"`, snake_case like the rest of this RESTX surface) that ALWAYS mints. The default get-or-create is the app detail header's "open
session" action; `new_session` is the composer's, which asks to START a conversation and must never be
handed the maintainer's transcript to append to.

- **It takes the per-session-unique `app:<id>:<session_id>` tag, never the bare one.** A tag maps
  to exactly ONE session, so a second claimant on `app:<id>` would STEAL it from the maintainer.
- **It is NOT written back to `maintainer_session_id`.** The repair wake dereferences that field,
  and two claimants would make the resolvers' first-match scan order load-bearing for which
  session keeps working — the same reason a reused builder session is never promoted.
- **It may read, stage AND submit — but only against the app it was opened for.** `get_app` and
  `submit` both read the tag tier; `app_data` (gated on `maintainer_session_id`) and
  `run_pipeline` (the id-field scan) still return the uniform `not_found`.

  `submit`'s membership test is **"is this session server-BOUND to this app"**, resolved through
  the one seam `_bound_app_for_session` → `AppStagingArea.app_for_session`, not
  `builder_session_id == maintainer_session_id`. Binding is a server-stamped tag or an id field;
  **the `app_id` CONTEXT key proves nothing** — it is merged verbatim from a request and
  re-writable on any later turn, so it addresses without authorizing, exactly as the wiki `slug`
  does. A test pins that distinction.

  Narrower than it sounds, because the test cuts both ways: a session bound to app A is refused
  when it tries to INSERT app B. That insert path previously had no guard at all, and it is where
  a duplicate app actually came from — a composer session, refused on the update, told by the
  refusal string to "submit a new app_id instead", and complying. **A refusal must never name
  forking as its remedy.**

  **The maintainer field is still never re-pointed, and triggers still belong to it.** The
  resubmit branch cancels and re-arms on `existing.maintainer_session_id`, never on the
  submitter — cancel for the wrong session and the real maintainer's armed triggers survive to
  fire a stale `wake_prompt` forever. Two tests fail if that is ever simplified back.

  The one residue is unchanged: a `building`/`draft` app has no live-overwrite guard, so such a
  session could submit over a draft exactly as any builder session can.

**Archived apps are not refused.** `submit`'s maintainer-resubmit branch already revives an
archived app: it overwrites the in-memory `status` to `"building"` before calling
`transition("live", ...)`, which is what lets that call succeed against the transition table's
otherwise-terminal `archived` entry. Refusing this route for an archived app would sever the one
session that can ever reach that revival again, for no matching gain — archiving already
cancelled every trigger the app owned, so a reachable maintainer session grants no more than a
conversation, not a live re-arm.

**A reused builder session is never promoted into `maintainer_session_id`.** The resolvers above
already find the app through EITHER field, so writing the reused id into the other one would
give the app two live claimants and make those resolvers' first-match scan order load-bearing for
which one keeps working. Reuse leaves the stored manifest exactly as it was.

## Submit-time verifier

`_verify_pipelines` dry-runs every code pipeline through the SAME `AppPipelineRunner` the fire seam
uses (via the already-wired `tracker.pipeline_runner`), catching what lint cannot — an import
error, a bad `ctx.read_file` call — before a pipeline goes live.

- **Ordering is the whole point: verify runs BEFORE any mutation** — before the maintainer session
  is minted, before a resubmit's prior triggers are cancelled, before pipelines arm, before the
  manifest/version persist, before the go-live seed fires. A refusal therefore leaves NO state. It
  runs AFTER the live-overwrite guard, so a double-submit collision is reported as that rather than
  masked by a verification error.
- **`dry_run=True` plus calling `execute()` directly (never the tracker's `record_code_run`)
  guarantees no durable write and no ledger row.** A dry run still makes a real, budget-bounded
  model call when the pipeline declares `ctx.llm`; only agentic pipelines are genuinely
  model-call-free, and only because they are skipped.
- **The verdict map is `{pass, fail, skipped}`, persisted on `AppVersion.verification` keyed by
  pipeline name — but `fail` never reaches storage.** A genuine failure
  (lint/import/runtime/syntax/traversal) raises a `ValueError` refusing the submit as an actionable
  reask before any version row is written; the member exists for honesty should that refusal ever
  soften. `skipped` covers four verifier ARTIFACTS, never pipeline defects: `mode="agentic"` (a
  wake is judgment, not smoke-testable); the runner unwired (logged once, never a crash); a dry run
  failing on `params` (a `params_schema` requiring input a `params={}` smoke cannot supply, e.g. a
  `user_writable` form); and a dry run failing on `workspace` (no app has a bound workspace at
  submit time — the maintainer session is not minted until after verification, so an unconditional
  `ctx.read_file()` trips this every time). Only `params`/`workspace` codes get this treatment.
- **The go-live seed is not a replacement** — it is the post-live real fire that gives freshness its
  first data point; the verifier is a pre-live smoke test.
- **THE DISARM TRAP — a pipeline that swallows `PipelineExecutionError` makes this entire gate
  unreachable.** The dry run classifies only what PROPAGATES out of `execute()`, so a pipeline
  wrapping `ctx.read_file`/`ctx.glob` in a broad `except` returning a default verifies `"pass"` no
  matter what broke, and defeats the `params`/`workspace` skip classification too, since those are
  also just codes on a raised error. Live-verified: an absolute `ctx.read_file` path (a `traversal`
  code, which this gate treats as a genuine failure) was eaten by an `except Exception: return []`;
  the app went live, every scheduled run reported `succeeded`, and it wrote zero documents to the
  collection its whole frontend read. `check_pipeline_error_swallow` is the structural fix.

## Pipeline lint rules are a LIVE-FLEET MIGRATION, not a submit gate

`lint_pipeline` is re-run by the runner on EVERY execution, not only at submit — the "same
allowlist gates both boundaries" law. Its consequence is easy to miss: **adding a rule to
`_PIPELINE_RULES` can retroactively fail an already-live pipeline at its next scheduled fire**, an
outage vector with no relation to anyone submitting anything.

Before landing any new rule: **lint every STORED app version against it** (live specs and
`app_versions` snapshots alike), then repair the apps it breaks or consciously accept stranding
them. Rollback is a shipped feature, so a version that now fails lint is a version the user can no
longer roll back to. Accepting that is fine; discovering it in production is not.

`check_pipeline_error_swallow` is deliberately permissive about re-raising: ANY `raise` in the
handler body counts, including one under an `if`. Requiring every path to re-raise would need real
reachability analysis and would false-positive the legitimate narrow-tolerance shape
(`if getattr(exc, "code", None) == "read": return []` then `raise`). The accepted gap — a contrived
`if False: raise` — needs deliberate evasion, not the sloppiness the rule targets.

## Version transparency

`AppVersion` carries `summary: AppVersionSummary | None` and `verification: dict[str,
PipelineVerdict] | None` so a submit's effect is a stored, re-readable fact rather than something
reconstructed from a diff of two spec blobs.

- **`AppVersionSummary` is a pure, strategy-on-model diff** (`models.py`): `compute(prev, new)`
  takes both specs as method args (no store, no I/O; `prev=None` ⇒ everything reads as "added"),
  `describe()` renders one compact line. Files diff by path; pipelines and collections by NAME.
  **`trigger_ref` is deliberately excluded from "changed"** — it is platform-owned output the
  lifecycle re-mints on every arming pass, so including it would false-positive "pipeline changed"
  on every resubmit even when the builder touched nothing.
- **Stamped at both submit AND rollback, with different baselines:** submit diffs the incoming draft
  against the PRIOR LATEST version's spec (read before persisting the new snapshot); rollback diffs
  the current LIVE app against the snapshot being rolled back TO — the reverse direction, what
  reverting changes. Rollback's `verification` stays `None`, since the target snapshot's pipelines
  were verified at their original submit and rollback re-arms without re-executing them.
- **Wire delivery needs no route change** — `_render_version`/`model_dump` surface additive fields
  structurally; rows without them carry `null`/absent and every consumer must tolerate that.
- **`submit_app`'s success echo reads the version back** rather than carrying the summary through in
  memory: it calls `get_app_store().get_version(app_id, version)` after `AppLifecycle.submit`
  returns and appends `summary.describe()` plus any non-`pass` verdicts to the tool result, so the
  agent sees in one round trip exactly what a console user sees on the Versions rail.

## Pipeline-name uniqueness — a submit-boundary method, not a validator

`AppSpec.ensure_unique_pipeline_names()` refuses duplicate pipeline names, but as a plain method
`AppLifecycle.submit` calls on every incoming draft, NOT a `model_validator` — the discipline
`PipelineSpec.ensure_wakeable()` shares. The app store is append-only, so a stored `AppVersion`
snapshot that already holds a duplicate (pipeline resolution has always been first-match, so a dup
silently shadowed one pipeline rather than erroring) must keep PARSING for a detail read; a
parse-time floor would 500 every detail read of any app holding one. Validation of new data belongs
at the trust boundary it crosses; stored history re-crosses the parse seam under the contract it was
written with.

## Boundaries

- **Upstream-API only.** This is a thin product surface; the CLI never imports it (the
  `agentic_search` precedent — store lives app-side). No domain engine grows here.
- **The REST pipeline invoke ledgers a `PipelineRun` ONLY when the invoke had a real effect.**
  `AppsRoutesController.invoke_pipeline` routes execution through
  `record_code_run(kind="on_request", dispatch_failure=False, require_effect=True)` when a `tracker`
  is wired (`None` falls back to `runner.execute()` directly — no ledger, unwired-tolerant).
  `require_effect=True` persists a SUCCESSFUL run only when `cache=="miss"` AND `docs_written` is
  non-empty, so a cache hit or a read-only render mints NO row: a polling dashboard on a
  `cache_ttl_seconds=0` pipeline must not mint one per render. A FAILURE is ALWAYS ledgered (a
  genuine break is real provenance however it was invoked), but `dispatch_failure=False` means it
  never triggers `on_pipeline_failure` — only an autonomous SCHEDULED fire auto-repairs or pauses.
  `record_code_run` returns `(PipelineRun, PipelineResult | None)`, where `result is None` ⇒ the run
  failed and `run.error` carries why, so the controller gets BOTH the ledger write and the response
  payload from ONE execution with no forked bookkeeping. A WRITING invoke legitimately moves
  `/system` freshness and "last success"; `kind` distinguishes on-request from scheduled. Open gap:
  every failed invoke ledgers, so a persistently broken pipeline hammered by a client accretes rows
  — a per-pipeline cooldown on repeated on-request FAILURES would bound it.
- **The `run_pipeline` SessionTool ledgers through the SAME seam, on the `/fire` policy**
  (`record_code_run(kind="on_request", dispatch_failure=False, require_effect=False)`): a
  model-driven invoke is an explicit refresh, so it ALWAYS ledgers, unlike the REST render path.
  It used to call the runner's id-keyed `run_pipeline` adapter directly, which writes NO row —
  so a maintainer asking "did my run land?" read `last_run_status` off a manifest describing an
  EARLIER run and took it for its own (measured on a real session: the `succeeded` the model
  accepted as its answer had finished ~7 minutes before it invoked anything). The tool surfaces
  `run_key` on the success envelope AND, when the failed run was ledgered, inside the
  `{"error": {...}}` envelope — additive, since `_SessionToolError.parse` reads only
  `code`/`message`/`permanence` and ignores other keys. **A `dry_run` deliberately does NOT
  ledger** (no durable write to record; `record_code_run` has no dry-run mode) and reports
  `run_key: None`, as does a deployment with no ledger wired.
- **The ledger reaches the plugin by a down-only push, `register_pipeline_ledger`**
  (`plugin/runtime.py`, mirroring `register_pipeline_runner`), wired in `backend.py:init_apps`.
  **The tool uses it only when the ledger's own `pipeline_runner` IS the runner that call
  resolved** — a ledger executes through the runner it holds, so an unpaired one would silently
  run a different executor and discard the resolved one, with nothing failing. `init_apps`
  registers the runner and builds the tracker around that same instance, so production pairs by
  construction; a mismatch logs and runs unledgered rather than routing around the caller.

## Testing

Contract tests from the caller site, stub only I/O (JSON stores real, sessions faked, injected NOW).
Never run a real LLM or hit a real proxy.

| File | Covers |
|---|---|
| `test_apps_lifecycle.py` | submit / re-home / pause / rollback, workspace scope, verifier verdicts, version summary, every policy value under an integrity issue |
| `test_apps_routes.py` | controller domain logic incl. SDK-free default, `mint_token` scope gating, read/write authorization |
| `test_apps_routes_flask.py` | Flask client — the dual-registration guard + real startup wiring (submitter registered, SDK loaded, runner wired) |
| `test_apps_integration.py` | the two cross-stream seams: capability build surfaces `app_data`; SDK injection at the detail seam vs the clean stored spec |
| `test_apps_tokens.py` | signer mint/verify round-trip, write scope, 4-part blob compatibility |
| `test_apps_pipeline_endpoints.py` | pipeline list surface, GET/POST invoke incl. params coercion/validation, agentic 409, unwired 503, runner-exception 502, the write-scope token matrix, the ledger-effect matrix |
| `test_apps_get_app.py` | get/stage, session scoping, hostile-`app_id` staging guard, `trigger_declared` honesty |
| `test_run_pipeline_ledger.py` | the `run_pipeline` tool's ledger seam: an invoke ADVANCES the ledger past an older row, a timed-out invoke ledgers `failed` with its partial `docs_written` and returns the `run_key`, `dry_run` ledgers nothing, no auto-repair, unwired/unpaired degrade cleanly |
| `test_apps_models.py::TestNewIntegrityViolations` | the two anti-spam rules as pure model tests — never-populated, cache-hit, edge suppression, edge re-arm |
| `test_apps_pipeline_tracker.py::TestIntegrityDispatch` | the agentic close seam end-to-end: a regression dispatches while the run stays `succeeded`, repeated fires dispatch ONCE, a manual fire never dispatches, a sibling pipeline's collection is never blamed |
| `test_apps_pipeline_runner.py::TestCodeFireIntegrityDispatch` | the code tier of the same loop |
