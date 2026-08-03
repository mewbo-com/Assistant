# Demo-as-code — agent guide

> ↑ [root](../CLAUDE.md)

Everything under `demo/` (plus `apps/mewbo_console/tests/demo/`) exists to
regenerate the committed doc artifacts (`docs/assets/img/mewbo-{console,wiki,search,settings}-*`)
from a seeded, containerized stack. `demo/README.md` is the operator view; this
file records the engineering decisions and the traps that are NOT re-derivable
from the files themselves.

## Three surfaces, three seeders, one stack

`demo/seeder` seeds all three console surfaces from ONE `make demo-seed`, each
through its own real store contracts — no live indexer, no LLM, no network:

- **Sessions/triggers** → `DemoSeeder` (`seeder.py`) via `SessionStoreBase` /
  `TriggerStoreBase` → the Tasks surface (`mewbo-console-*`).
- **Wiki** → `WikiSeeder` (`wiki.py`) via `MongoWikiStore` (`create_project` /
  `save_page` / `upsert_nodes` / `upsert_edges` / `create_job` /
  `append_job_event` / `save_qa`) → the Wiki surface (`mewbo-wiki-*`): a
  12-project gallery, a dense `bearlike/Grove` code graph, a wall of resumable
  index jobs, one in-flight job, and stored Q&A answers.
- **Search** → `SearchSeeder` (`search.py`) via `MongoAgenticSearchStore`
  (`save_workspace` / `create_run` / `append_run_event` / `append_past_query`) +
  the SCG store + the wiki memory store → the Search surface
  (`mewbo-search-*`): 8 workspaces, 10 completed runs, and a **54-node** SCG
  capability graph (6 sources + 48 tools; 171 edges — the counts the seeder
  prints and `test_search_seeder.py` asserts).

⚠️ **The SCG node count is not the number the landing renders.** The health band
shows `86·95`, which `search-landing.spec.ts` waits on: that band is a
PER-WORKSPACE summary folding in the 32 seeded memory notes, so `86 = 54 + 32`.
The SCG store never holds 86 nodes.

Each seeder is an atomic class with collaborators DI'd; every bundle model is
`extra="forbid"`; wall-clock instants are T0-relative OFFSETS the seeder rebases,
so a re-seed is byte-identical.

The two force-directed GRAPH views are SEEDED and render live but are not CAPTURED:
a force layout is not byte-reproducible. Every captured artifact is byte-identical
across cycles, including the `widgets` shot on a cold first-after-seed clean cycle.

## Position in the monorepo

`demo/` sits ABOVE the dependency DAG, at the altitude of `tests/` — it may
import from packages and apps, but **nothing may ever import from `demo/`**.
`demo/seeder` is a uv workspace member for local dev (tests, lint, types) only;
at runtime it is NOT installed anywhere — the `seed` compose service mounts it
read-only into the api image and runs it via `PYTHONPATH` (src layout). That is
deliberate: the seeder must never force an api image rebuild, and it may only
depend on what the api image already ships (mewbo_core, pydantic, pymongo).

## Load-bearing decisions (do not "simplify" these away)

- **Isolated bridge network, never host networking.** The deployed stack already
  owns ports 5125/3001/27018 wherever this runs, so the demo stack must be
  bootable next to it: `docker-compose.demo.yml` uses the default bridge network
  with `name: mewbo-demo` and publishes exactly one loopback debug port (3210).
- **`:demo` image tags.** Services build `mewbo-api:demo` / `mewbo-console:demo`
  with `pull_policy: missing`. Never tag over `ghcr.io/...:latest` — the deployed
  stack pulls those.
- **Ephemeral mongo = determinism.** No volume, no auth (`mongodb://mongo:27017`,
  isolated network). Every `demo-up` is a clean world; `demo-seed` recreates it.
- **API healthcheck is authed `GET /api/sessions`, NOT `/api/models`.**
  `LLMConfig.list_models()` short-circuits on an empty `api_key` today, but the
  moment live-mode sets a key it does a real `urlopen` against `llm.api_base`
  (8s timeout) — an outbound call on an isolated network inside a healthcheck.
  `/api/sessions` is Mongo-backed and structurally can never call out.
- **Seeder writes THROUGH store contracts** (`MongoSessionStore`, trigger store).
  Stores do **not** accept `client=` injection — they build their own
  `MongoClient` from `uri`/`database` params falling back to
  `MEWBO_MONGODB_URI`/`MEWBO_MONGODB_DATABASE` env, which is exactly how the
  `seed` service targets the demo mongo. Tests monkeypatch `pymongo.MongoClient`
  (mongomock), the established repo pattern.
- **Raw app screenshots are the baseline** — no window frame or shadow. Adding
  decoration back would be a post-step (ffmpeg/imagemagick), deliberately not
  done: fewer moving parts.

## Traps (each one cost real debugging time)

| Trap | Consequence | Rule |
|---|---|---|
| `docker/nginx-console.conf` proxies to `127.0.0.1:5125` (a host-net assumption baked into the console image) | console silently cannot reach the api on a bridge network | mount `demo/nginx-console.demo.conf` over `/etc/nginx/conf.d/default.conf`; it is a byte-faithful copy modulo `proxy_pass` host |
| nginx auto-301s `/api/sessions` (bare) onto the SSE prefix block's `/api/sessions/`, which Flask 404s | session listing AND creation break through the proxy | keep the exact-match `location = /api/sessions` block (in BOTH conf files) ahead of the SSE prefix block |
| `MEWBO_VITE_API_KEY` is baked into `runtime-config.js` when the console **container starts** | changing the key after boot does nothing | set env before `demo-up`; restart the console container to re-bake |
| Relative timestamps ("45 minutes ago") re-render every wall-clock minute | every capture differs → zero-diff gate can never pass | bundles store offsets from `DEMO_T0`; seeder rebases; flows freeze `page.clock` at the same `DEMO_T0` |
| `page.clock.install` freezes JS timers | the landing typewriter pins to its first phrase (fine, deterministic); anything polling-driven never advances | mount-time fetches still fire (promise/MessageChannel-driven); do not build flows that need timer-driven refetches |
| Playwright version vs browser image | pixel drift between environments | the `shots` service image tag must equal the resolved `@playwright/test` version in `package-lock.json` — bump them together |
| The api materialises a "'title' completed" notification LAZILY on the first `GET /api/sessions/<id>/events` after a completion event exists, deduped forever on `(session_id, completion_ts)` — dismissed records included | a completion TOAST appears in whichever capture first touches a seeded session; worse, the toast text contains the session title, so an unscoped `getByText(title).first()` clicks the toast instead of the session row | the seeder's `NotificationSettler` (REST-only — `NotificationStore` is a JSON file inside the api container, no store seam crosses that boundary) touches every seeded session's events endpoint then dismisses everything, once per seed; `openSession` scopes row lookups to `main` |
| **Compositor/wall-time paints ignore the faked clock**: scrollbar-thumb fade after a scroll, CSS transitions (the card expand-chevron rotation), caret blink | ~10-pixel nondeterministic diffs that survive every clock trick and appear in a different artifact each run | the capture stylesheet in `fixtures.ts` kills them all (`transition:none`, `animation:none`, scrollbar paint off, caret transparent) — reducedMotion alone only gates keyframes, NOT transitions |
| `page.clock` fully paused BEFORE navigation starves the load path | session list pinned on "Loading sessions..." forever | `install({time: T0})` (auto-ticking) for the load, then jump-and-pause at the FIXED instant T0+60s inside `settle()` — all pending timers fire to one run-independent moment |
| `locator.screenshot()` re-scrolls and re-rasterizes on its own terms | element closeups land on two attractor scroll states → flaky byte-diffs while full-viewport shots are stable | explicit `scrollIntoView({block:"center"})`, then an INTEGER `clip` crop of the page raster via `page.screenshot({clip})` |
| Fresh named volumes are created root-owned; `shots` runs as uid 1000 | `npm ci` dies with EACCES on the cache | `shots-cache-init` (root one-shot) chowns the volume; `shots` depends on `service_completed_successfully` |
| The TurnScroller margin rail measures live scroll geometry | a few antialiased pixels jitter run-to-run in the far-left gutter | hidden by the capture stylesheet — a still carries no scroll position |

### Wiki + search traps

| Trap | Consequence | Rule |
|---|---|---|
| `MongoWikiStore` reads `storage.mongodb.*` CONFIG, not the `MEWBO_MONGODB_*` env the session store honours; `create_wiki_store`/`create_scg_store` dispatch on `storage.driver` config | the wiki seeder wrote one db while the api read another → console showed an EMPTY wiki/search | the wiki seeder passes `uri=`/`database=` explicitly; AND `demo/configs/app.json` points `storage.mongodb` at `mongo:27017`/`mewbo_demo` and the `seed` service MOUNTS `./configs` — so every config-reading store (wiki/scg/search) and the env-reading session store all resolve to the SAME demo db |
| `scg.enabled=false` 503s `/scg` + empties the workspace graph; `MEWBO_AGENTIC_SEARCH_SEED` gates BOTH the fixture source catalog AND a lazy 6-workspace auto-seed on first `/agentic_search` touch | blank capability graph, blank source chips, or colliding fixture workspaces | `app.json` sets `scg.enabled=true`; api + seed set `MEWBO_AGENTIC_SEARCH_SEED=1` (catalog renders), and the seeder fills the store BEFORE any `/agentic_search` request (compose ordering), so the auto-seed's "if empty" guard skips |
| The landing "Incomplete indexes — N resumable" band reads `/jobs/recoverable` (status ∈ failed/cancelled/interrupted AND a non-no-op `ResumePlan`) MINUS active-slug jobs | `interrupted` jobs are ALSO "active" (excluded); a job whose slug has no graph/pages has a no-op ResumePlan (filtered) — so a wall of `interrupted` throwaway jobs renders the band EMPTY | seed the resumable jobs as `failed`/`cancelled` (not interrupted) AND give each a few `GraphNode`s on its own slug (the cheapest non-no-op ResumePlan; no `Project` doc needed); keep exactly ONE `finalizing` job for the "Indexing now" band |
| ⚠️ **The auto-seed guard is PROCEDURAL, not structural.** It asks only "is the store empty", so `demo-up` → shots (first `/agentic_search` touch installs the 6 fixtures) → `demo-seed` (adds the 8 seeded) → shots renders **14** workspaces. Mongo is ephemeral per `demo-up`, not per `demo-seed`, so a re-seed without an intervening `demo-down` is enough | a landing artifact depicting 6 api fixtures that never passed through a bundle, alongside the 8 seeded | re-seed only after `demo-down` (or run the full `make demo`), and treat the count anchor below as the real guard |
| A spec that anchors on seeded NAMES but not on their COUNT cannot see contamination — extra rows satisfy every `toBeVisible` | "green spec, wrong screenshot" | `search-landing.spec.ts` asserts `toHaveCount(8)` over `WorkspaceCard`'s `aria-label="Open workspace <name>"`. Prefer counting an existing accessible affordance to adding a testid. **Generalize this:** when a surface renders a seeded COLLECTION, anchor its cardinality, not just a few members |
| `DemoHelper.capturePage()` must honor `SHOTS[name].type` | hardcoding `type:"png"` writes PNG bytes under a `.jpg` filename for every full-page wiki/search shot | `capturePage` reads the per-shot type (jpeg-at-90 vs png), matching `captureElement` |
| A completed search run, a stored Q&A answer, and an in-flight index job each render from a STORED snapshot | needing a live LLM/POST would be nondeterministic | search run `/search?ws=&run=` (one `GET /runs/<id>` + SSE replay); wiki Q&A `/wiki/qa?answer=&page=&slug=` (static `isSnapshot`); wiki job progress replays its log via SSE `/v1/wiki/index/<id>/stream` — NOT `/events`, which is EMPTY for a seeded job |
| The wiki index wizard's Generation step fires `POST /v1/wiki/branches` (a real `ls-remote` to the repo host) | a LAN call inside a supposedly-offline capture + a nondeterministic branch list | the flow `page.route`-stubs `**/v1/wiki/branches` so the wizard falls back to "Default branch" |
| The search-landing `WorkspaceHealthBand` numbers load async from `/graph/summary` AFTER the labels render | waiting only on the labels captured blank number skeletons (also a blank↔number byte-diff) | wait for a rendered NUMBER (`86·95`), never just the label |
| `shots-cache-init` (a `shots`-profile one-shot) lingers between `compose run` invocations, pinning the network it was created on | a later run restarts it against a removed network → "network `<id>` not found" | `demo-shots-web` force-removes it first; `demo-down` names the `seed`+`shots` profiles so `down` actually tears them down |
| Duplicate accessible text: two `<h1>Grove Overview</h1>` (the page title `#page-top` AND the body's first markdown heading); the platform-tile "Gitea" also matching a type-tile description + a token-help link; a top-ranked result's slug appearing 5× (card title, card url, synthesis prose) | strict-mode "resolved to N elements" | flows anchor on stable ids (`#page-top`), start-anchored names (`/^Gitea/`), or `.first()`; locate by the ACTUAL seeded strings (the coordinator trace lane is `coordinator`, the probes `scg-path-probe`) |

### Widget shot (`mewbo-console-07-widgets.png`) traps

Captures a seeded session whose transcript carries a `widget_ready` event: a
Streamlit/stlite widget (a trending-repositories card grid) rendered inline in the
conversation. This is the ONE shot that boots a real Pyodide+Streamlit render, so
it has its own trap set.

| Trap / fact | Consequence | Rule |
|---|---|---|
| `widget_ready` INLINES the widget's files in its payload (`files:{"app.py","data.json"}`, frozen `extra="forbid"` at `submit_widget.py` + console `types.ts`) | seeding needs NO widget-file endpoint and NO `/tmp/mewbo/widgets` FS staging — `StliteWidgetPanel` boots the kernel straight from the event's inlined files | seed ONE Mongo `widget_ready` event via the `WidgetReadyEvent` seed kind (`models.py`); the shared `to_event(...)` kwarg contract carries `session_id` because this is the one kind whose wire payload embeds it. `requirements` MUST stay `[]` — an un-vendored pyodide wheel routes to PyPI and fails on the offline demo net |
| The demo console renders stlite OFFLINE | — | pyodide is vendored into `mewbo-console:demo` (`docker/Dockerfile.console` `npm run build` → `fetch-pyodide`); `nginx-console.demo.conf` serves `mjs`/`wasm` via its `^(/widget-host)?/pyodide/` `types` map. No api endpoint is involved |
| **Build the widget's `app.py` from NATIVE Streamlit components, NOT a hand-rolled `unsafe_allow_html` blob.** A whole-card `<a>` wrapper bleeds the theme `primaryColor` onto EVERY descendant using `color:inherit` | a custom-HTML card grid reads as a cramped orange blob, not a Streamlit widget | `st.container(border=True)` cards inside `st.columns`, native `st.markdown`/`st.caption`/`st.progress`, color via native markdown (`:green[...]`, code spans) — authentic typography, spacing and palette come free |
| **The widget renders at NATURAL scale** — the panel's old `zoom:0.85` is REMOVED (fractional zoom → fractional glyph raster → janky intra-word spacing + border-AA jitter; see `apps/mewbo_console/CLAUDE.md` → "Natural scale"), and `WidgetCard` sizes the card to content + chrome (`data-widget-chrome`) | re-introducing any scale hack, or breaking the chrome-aware measurement, brings back both the janky text AND the clip-edge nondeterminism | design content for natural-scale pixels: the card cap is `min(600px, 70vh)` and the seeded 2×3 grid + header lands ~570px INCLUDING chrome — inside the cap with slack, no internal scroll |
| **`st.container(height=N)` (fixed-height cards) reserves a scrollbar gutter** that narrows card content by ~10px — at that width the longest bold title wrapped to 2 lines and its content overflowed the fixed box, clipping the progress bar | ragged titles + a clipped bar, i.e. the exact untidiness fixed-height was meant to prevent | equalize card heights the NATIVE way: natural-height `st.container(border=True)` + make every card render the SAME line counts (titles ≤1 line, descriptions exactly 2 caption lines — extend a too-short seed description rather than fixing heights) |
| **Text-fit verified in an interactive browser (DPR 1) can still wrap in the pipeline (deviceScaleFactor 2)** — subpixel metrics at DPR 2 make bold text a couple px wider | a title that fits in your checking browser wraps in the committed artifact | judge text-fit ONLY on the pipeline's own PNG; keep decisive slack — the widget's `title()` helper drops the owner from any slug >20 chars and the seeded set keeps every rendered title ≤22 chars |
| **A console widget card is CAPPED at `min(600px, 70vh)` and scrolls overflow INTERNALLY**; content at the clip edge paints on WALL time, RACILY | any content at the clip edge paints (or not) run-to-run → a bottom-band byte-diff that survives the frozen clock AND the capture stylesheet | **SIZE THE WIDGET TO FIT THE CAP**: 6 natural-height cards (2×3) + header ≈570px including chrome → no scroll, no clip. More rows overflow the cap and reintroduce the race — the deliberate reason `data.json` is trimmed to 6 |
| A native card count (`[data-testid="stProgress"]`, one bar per card) reaching its target only means the DOM nodes EXIST; stlite STREAMS its render and `WidgetCard` re-measures height asynchronously (ResizeObserver → rAF), so the grid AND the card bottom can still move after the last node appears | a capture right after `toHaveCount` freezes a half-blank grid or a still-settling card edge, differing run-to-run | `page.waitForFunction` until the last bar's bottom AND the card's bottom (`.stApp`→`closest('.fixed')`) hold steady jointly across ≥8 consecutive 250ms polls, THEN `settle()`. The count alone is not a paint signal |
| ⚠️ **The geometry wait MUST run BEFORE `settle()` — the paused Playwright clock ALSO freezes `requestAnimationFrame`**, and WidgetCard's portal-mirror (ResizeObserver → rAF → state) dies with it: any card growth landing after the pause never reaches the portalled wrapper | a TRUNCATED widget card frozen mid-growth (second card row cut off, placeholder showing through beneath) | geometry quiesces while the clock is ALIVE; settle only freezes what the wait already verified |
| A ~13 CSS px band-diff at the card's bottom border is the SAME rAF-freeze at 1px magnitude, NOT compositor noise: the portalled wrapper freezes with a rect one pixel staler than the in-flow placeholder, so two `rounded-lg` borders sit 1px apart | chasing it as "paint nondeterminism" (warm-up reloads, sub-pixel waits on the BARS alone) reduces the frequency without touching the cause | the joint wait above closes it by construction |
| `openSession` leaves the pointer wherever it clicked the landing row, often over a card | that card renders its `:hover` state in the still, and the highlight moves if the click position ever shifts | `page.mouse.move(0, 0)` before `settle()` |
| The widget's session title matches the session-title `h2`, the sidebar row AND the widget heading (case-insensitive substring) | strict-mode "resolved to 3 elements" | anchor ONLY on the `stProgress` count (a Streamlit testid, unique to the widget — the console renders no Streamlit); never also assert the widget title text |
| **Seeding this session PERTURBS other shots.** It is the NEWEST session (offset −600, origin `user`, default-visible), so it renders as the top row of the landing recent-list (`front`, 01) AND the tasks sidebar (`tasks`, 02) | a `make demo` regenerates 01 and 02 with different bytes — committing only 07 breaks the byte-reproducible invariant | regenerate + commit `mewbo-console-01-front.png` + `-02-tasks.png` alongside `-07-widgets.png` — 3 shots move, not 1. The closeups (crop to one card) and the wiki/search shots (non-session home routes, no sidebar) are unaffected |
| The shot is 4:3 (1280×960); the other console shots follow their reference raster's ratio | — | a per-spec `test.use({viewport})` gives 4:3; the shared `playwright.demo.config.ts` default is used by no spec, so the VIEWPORT axis alone changes nothing else |

### Ask-user shot (`mewbo-console-ask-user-log.jpg`) traps

A CLOSEUP of one conversation column showing the three states an
`ask_user_question` card can hold at once: a timed-out-but-still-answerable group,
a `multi_select` group with the group-level notes box, and a bounded group awaiting
an answer. The `user_question` / `user_question_answered` seed kinds (`models.py`)
defer EVERY per-question rule to `mewbo_core.tooling.ask_user.AskUserQuestionArgs`
and re-serialize through it, so a seeded payload is the shape the api dispatcher
writes. To iterate on this shot alone, after `demo-up` + `demo-seed`:
`$(DEMO_COMPOSE) run --rm shots bash -c "npx playwright test --config=playwright.demo.config.ts ask-user-question"`.

**Content-anchor:** the three seeded question headers + the notes placeholder
(`shots.ts` `SEED.questionHeaders` / `SEED.questionNotesPlaceholder` ↔
`console-poc.json`), the per-card control counts (3 radios / 3 checkboxes / 2
radios), `waits up to 30m` (rendered from `timeout_seconds: 1800`) and exactly
two `Awaiting your answer` badges. Change an option list and the spec fails,
which is the intended staleness signal.

| Trap / fact | Consequence | Rule |
|---|---|---|
| Only a still-PENDING group renders the bounded-wait hint AND the empty notes box (`QuestionCard`: `answerable && !runMovedOn`) | give every group an outcome and the shot loses the two affordances it exists to document; a seeded `answered` outcome renders "No answer recorded." unless answers are paired to the questions | exactly ONE group carries the `timed_out` outcome; the other two stay unresolved. `answered` is not a seedable outcome at all (`UserQuestionAnsweredEvent`). This is the one liberty in the transcript — a live dispatcher always writes an outcome — and it portrays a genuinely reachable state, since a question the run stopped waiting on really is still answerable |
| The seeded session's `offset_seconds` is −259200, and that is FRAMING, not flavour | a newer session becomes a visible row of `front` (01) and the rail's recents (02), so those two committed artifacts change bytes too — the "3 shots move, not 1" hazard again | the rail's recents list caps at 15 rows and 15 seeded sessions are newer than 2 days, so anything older than −172800 lands off BOTH lists and 01/02 stay byte-unchanged. Two accepted costs: the session header renders an absolute date rather than a relative one, and the open session is not highlighted in the rail |
| The conversation column is capped at `max-w-[820px]` INSIDE the pane, so closing the workspace panel widens the cards by nothing | a full-viewport still spends most of its pixels on chrome and shrinks 11px option descriptions past reading at the 720px the docs render at | crop the column with `captureElement`, like the shell/file-read card closeups on the same docs page. The tall `test.use` viewport (1400×2000) is STAGING, not framing: a crop can only clip what is already inside the viewport, so the whole 1784px column has to be on screen at once |
| `toBeVisible` passes for a card scrolled out of the frame, and the conversation pane pins itself to the latest turn | a card can sit entirely above the viewport with every content assertion green — a wrong screenshot with a green test | the flow asserts the column's bounding box is wholly inside the viewport BEFORE capturing (`expectWithinViewport`) |
| The column carries `pb-32` so the live composer never covers the last turn | 128px of dead space at the bottom of a crop that contains no composer | zero the column's `padding-bottom` immediately before `captureElement` (1640×3570 → 1640×3314). Layout-mutating on purpose, the same licence `settle()`'s capture stylesheet takes when it hides the TurnScroller rail. Do the trim AFTER the viewport assertion, so the gate still measures the untrimmed column |
| The logs lane has no `user_question` renderer, so the workspace pane reads "No logs to show here" beneath a "Logs 6" badge | a docs still advertising an empty instrument panel and a count that contradicts it | the flow closes the pane (`Close panel`) first, which also re-centres the column the crop is aligned on |

## Keep-in-sync obligations

1. `docker/nginx-console.conf` ↔ `demo/nginx-console.demo.conf`: any change to
   the source conf beyond the `proxy_pass` host must be mirrored by hand.
2. `DEMO_T0` is a four-way contract: `demo/demo.env`, the seeder's rebase default and
   the flows' fixture default all READ it; the `widgets` shot's trending caption does
   NOT. That caption comes from an absolute window (`gain_start`/`gain_end`) hardcoded
   inside the seeded widget's `data_json` — an opaque serialized string the offset
   rebasing cannot reach — which `app.py` paints in its `st.caption`. Moving `DEMO_T0`
   moves every other timestamp but not that one, so the widget advertises a stale
   growth window while everything around it looks current. Keep it in step BY HAND;
   deriving it means the seeder rewriting inside an opaque JSON string, which is worse
   than the coupling and cuts against the bundle contract.
3. Seeded content the flows locate by accessible text must match the bundles
   exactly: `shots.ts` SEED + `console-poc.json` (session titles, `npm test`,
   `src/middleware/auth.ts`); AND the wiki/search flows' inline strings +
   `wiki-helpers.ts`/`search-helpers.ts` constants ↔ `wiki-poc.json` /
   `search-poc.json` (project slugs, page ids, the `demo-project-64` job, the QA
   `answer` id, workspace ids, run ids, result-card + trace-lane text). The
   `widgets` shot's seed↔flow contract is narrower but just as brittle: the session
   title (`SEED.session.widgetTitle` ↔ `console-poc.json`) AND the seeded widget's
   `app.py`, which must keep rendering one native `st.progress` bar per card so
   `widgets.spec.ts` can wait on exactly 6 `[data-testid="stProgress"]` nodes (its
   content-anchor + staleness signal) — change the seeded repo count and change
   that `toHaveCount(6)` too. The Settings + plan-approval shots add
   `SEED.apiKeyLabels` ↔ `console-poc.json` `api_keys[].label`,
   `SEED.llmApiBase` ↔ `demo/configs/app.json` `llm.api_base`, and
   `SEED.session.planTitle` ↔ the `demo-scoped-api-keys-plan` session title + its
   `PlanProposedEvent`/`PlanDecisionEvent` revisions (a decision must fold onto an
   earlier proposal of the same revision, enforced at bundle load). The ask-user
   shot adds `SEED.session.askUserTitle` + `SEED.questionHeaders` +
   `SEED.questionNotesPlaceholder` ↔ the `demo-ingest-queue-cutover` session's
   `UserQuestionEvent` groups (an answered event must fold onto an earlier group of
   the same `call_id`, enforced the same way).
4. Playwright image pin ↔ `apps/mewbo_console/package-lock.json`.
5. The demo Mongo db name has THREE readers that must agree: `demo/demo.env`
   (`DEMO_MONGODB_DATABASE`), the `MEWBO_MONGODB_DATABASE` env on the api+seed
   services, AND `demo/configs/app.json` (`storage.mongodb.database`, read by the
   config-driven wiki/scg/search stores). Change one, change all.

## Scope boundaries

Local web screenshots only, via `make demo-*`, covering console + wiki + search +
settings (21 committed `docs/assets/img` artifacts). Android capture (redroid +
Maestro), video rendering (Playwright `recordVideo` + ffmpeg) and CI wiring are
out of scope.

**Settings + plan-approval shots.** `settingsModels` reads `demo/configs/app.json`'s
fictional `llm.example.com` base + is-set secret state. `settingsSecurity` is seeded
via the `SeedApiKey` bundle kind with FIXED ids + T0-relative `created_at`, because
`KeyStore.create_key` mints a uuid off the wall clock and breaks byte-stability; the
stored hash is salted, so a seeded key can never authenticate. `planApproval` is backed
by the two-revision `demo-scoped-api-keys-plan` session — rev 1 decided, rev 2 pending
is the awaiting-approval card. ⚠️ That session is `awaiting_approval` and
default-visible, so it is a top-ish row of `front`/`tasks`; any status a seeded session
can hold needs a matching `StatusBadge` arm, or it falls through to the "Archived"
catch-all and mislabels a live session in both the list and its own header.

Four non-console captures (Nextcloud Talk, Gmail, Home Assistant Assist ×2) have no
service in this stack and need staged captures with invented identity data.

⚠️ **Two CONSOLE hand-captures sit inside the console numbering where they read as
pipeline output**: `mewbo-console-05-plugins.png` and `mewbo-console-06-projects.png`.
No spec writes either — `shots.ts` declares 21 filenames while 23 files match the
`mewbo-{console,wiki,search,settings}-*` convention, and those two are the difference.
Both render a DELETED top NavBar and the deleted standalone `/plugins` and `/projects`
routes, so they document UI a user cannot reach. Bringing them in needs seams that do
not exist: no managed-project seed kind (`virtual_projects` is empty, so the Workspace
facet renders blank), and the Plugins facet needs installed-plugin state plus a
route-stubbed marketplace, a marketplace listing being a network fetch on a deliberately
offline net. **Seeding managed projects also moves `ProjectLabel` on session rows, so it
perturbs `01-front`/`02-tasks`** — the "3 shots move, not 1" hazard. Do it as its own
change with its own byte-diff review, never folded into a re-render.
