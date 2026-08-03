> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [data](app/src/main/java/com/mewbo/aura/data/CLAUDE.md) · [ui](app/src/main/java/com/mewbo/aura/ui/CLAUDE.md) · [voice](app/src/main/java/com/mewbo/aura/voice/CLAUDE.md) · [notify](app/src/main/java/com/mewbo/aura/notify/CLAUDE.md) · [di](app/src/main/java/com/mewbo/aura/di/CLAUDE.md) · [mock](app/src/main/java/com/mewbo/aura/mock/CLAUDE.md) · [debug](app/src/debug/java/com/mewbo/aura/CLAUDE.md) · [test](app/src/test/java/com/mewbo/aura/CLAUDE.md) · [redroid](tools/redroid/CLAUDE.md)

# Mewbo Aura — Android Assistant Client

Scope: `apps/mewbo_aura/` — native Kotlin + Jetpack Compose assistant app (assist overlay via the
system assistant role + sessions/chat UI) over the Mewbo session REST+SSE API. The backend owns all
logic; Aura is a thin frontend.

**Design system: [`DESIGN.md`](DESIGN.md) is canonical for visual laws, token values, provenance, and
the regression registry ("never again").** Read it before ANY visual/UI change; when a scoped
CLAUDE.md and DESIGN.md disagree, DESIGN.md wins and the CLAUDE.md is stale. in these
files are DESIGN.md's provenance shorthand (user-directive rounds, in supersession order).

## CLAUDE.md tree (this app)

Read the deepest file that applies before editing; a new substantive package gets one.

| Scope | File |
|---|---|
| **Design system: laws, tokens, provenance, regression registry** | [`DESIGN.md`](DESIGN.md) |
| App root: build, device matrix, layering, atomic-class rules, iconography | `CLAUDE.md` (this file) |
| **Data — hub**: layering + the advertise/answer seam | `app/src/main/java/com/mewbo/aura/data/CLAUDE.md` |
| Data · REST (`AuraApi`, DTOs, `Response<>`-wrapped-vs-plain throw) | `app/src/main/java/com/mewbo/aura/data/api/CLAUDE.md` |
| Data · SSE (`SessionStreamClient`, backlog replay, `trySend` hazard) | `app/src/main/java/com/mewbo/aura/data/sse/CLAUDE.md` |
| Data · model (`SessionEvent`, `TranscriptReducer`, POISON ANCHOR, `PromotedTools`) | `app/src/main/java/com/mewbo/aura/data/model/CLAUDE.md` |
| Data · repos (`RunRepository` `@Singleton`, fork/retry, `RunNotifications`) | `app/src/main/java/com/mewbo/aura/data/repo/CLAUDE.md` |
| Data · device tools (catalog/executor, two-layer gate, launch gate) | `app/src/main/java/com/mewbo/aura/data/device/CLAUDE.md` |
| Data · settings (`SettingsStore` keys, `KeystoreCipher`) | `app/src/main/java/com/mewbo/aura/data/settings/CLAUDE.md` |
| **UI — hub**: thin router, Compose stability, one-chat-tree, a11y | `app/src/main/java/com/mewbo/aura/ui/CLAUDE.md` |
| UI · theme (tokens, discipline, `AssistantExtras`, reduced-motion) | `app/src/main/java/com/mewbo/aura/ui/theme/CLAUDE.md` |
| UI · orb + shared shader primitives (`GlslNoise`/`ClayFlowerSdf`) | `app/src/main/java/com/mewbo/aura/ui/orb/CLAUDE.md` |
| UI · shader family (`AuroraEdgeGlow`/`AuroraWashTop`) | `app/src/main/java/com/mewbo/aura/ui/aurora/CLAUDE.md` |
| UI · chat surface (`ChatScreen`/`Transcript`, message actions, scope row) | `app/src/main/java/com/mewbo/aura/ui/chat/CLAUDE.md` |
| UI · promoted-tool action cards (`PromotedTools` allowlist trap) | `app/src/main/java/com/mewbo/aura/ui/chat/toolcards/CLAUDE.md` |
| UI · Streamlit widget card (WebView ready-signal) | `app/src/main/java/com/mewbo/aura/ui/chat/widget/CLAUDE.md` |
| UI · composer (`AuraComposer`, `RmsWaveform`, scope-row anchor) | `app/src/main/java/com/mewbo/aura/ui/composer/CLAUDE.md` |
| UI · assist overlay render (`AssistUiState`) | `app/src/main/java/com/mewbo/aura/ui/overlay/CLAUDE.md` |
| UI · shared vocabulary (`ActionSheet`, `MarkdownMessage`, `ErrorCard`…) | `app/src/main/java/com/mewbo/aura/ui/common/CLAUDE.md` |
| UI · drawer + routes + `SessionActionsSheet` | `app/src/main/java/com/mewbo/aura/ui/navigation/CLAUDE.md` |
| UI · recents view-state + rail helpers | `app/src/main/java/com/mewbo/aura/ui/sessions/CLAUDE.md` |
| UI · chat search (client-side, title-only) | `app/src/main/java/com/mewbo/aura/ui/search/CLAUDE.md` |
| UI · settings screen (7 sections) | `app/src/main/java/com/mewbo/aura/ui/settings/CLAUDE.md` |
| **Turn-completion notifications** (FGS follow-a-run) | `app/src/main/java/com/mewbo/aura/notify/CLAUDE.md` |
| **DI** — Hilt modules + the seam law | `app/src/main/java/com/mewbo/aura/di/CLAUDE.md` |
| **Mock backend** (debug scripted backend) | `app/src/main/java/com/mewbo/aura/mock/CLAUDE.md` |
| Voice: `AssistTurnMachine`, `VoiceInteractionSession`, speak-along | `app/src/main/java/com/mewbo/aura/voice/CLAUDE.md` |
| Debug-variant tooling (preview hosts, fakes, mock) | `app/src/debug/java/com/mewbo/aura/CLAUDE.md` |
| JVM test idioms: coroutine-scope house rules, scripted-fake traps | `app/src/test/java/com/mewbo/aura/CLAUDE.md` |
| redroid dev container ops | `tools/redroid/CLAUDE.md` |

## Standalone Gradle project inside a Python monorepo

This directory is its own Gradle root (`settings.gradle.kts` lives HERE, not at the repo root).
Nothing Android leaks upward: no root-level gradle files, caches, or SDK paths.

- Build entry point is ONLY `./gradlew` from this directory. No system Gradle.
- `ANDROID_HOME` is not exported in CI/agent shells — prefix commands:
  `ANDROID_HOME=$HOME/android-sdk ./gradlew :app:assembleEnterpriseDebug`.
- `local.properties` (gitignored) carries `sdk.dir` for IDE use.
- Repo-wide Python tooling (ruff/mypy/pytest) ignores this subtree; Kotlin quality gates run
  through Gradle only.

## Build → install → verify loop (mandatory after UI/data changes)

```bash
ANDROID_HOME=$HOME/android-sdk ./gradlew :app:assembleEnterpriseDebug
adb -s localhost:5555 install -r app/build/outputs/apk/enterprise/debug/app-enterprise-debug.apk
adb -s localhost:5555 shell am start -n com.mewbo.aura/.MainActivity
adb -s localhost:5555 exec-out screencap -p > /tmp/aura.png   # then LOOK at it
adb -s localhost:5555 logcat -d --pid=$(adb -s localhost:5555 shell pidof com.mewbo.aura) | grep -E "FATAL|AndroidRuntime"
```

**Every adb/gradle invocation passes `-s <serial>`** — never default device selection; redroid and
the Pixel can be connected simultaneously.

**Distribution flavors (`public` · `enterprise`).** `enterpriseDebug` is the dev/enterprise variant —
it bakes a private, deployment-supplied root CA as a Network Security Config trust anchor and permits
cleartext for LAN backends, so the loop above builds `assembleEnterpriseDebug`. `public*` carries
platform trust only (system CAs) and is the ONLY flavor CI builds + ships publicly. The umbrella
`assembleDebug`/`assembleRelease` build BOTH flavors — always name the flavor. The CA cert is
gitignored and auto-seeded from `~/temp_folder/enterprise-ca.crt` by the `seedEnterpriseCa` task
(override with `-Pmewbo.enterpriseCaSource=` / `AURA_ENTERPRISE_CA_SRC` / `AURA_ENTERPRISE_CA_B64`);
an enterprise build fails loudly if it can't find the cert. **Never again:** `public*` APKs must not
contain the CA — verify with `unzip -l <apk> | grep enterprise_ca` (public = empty, enterprise =
`res/raw/enterprise_ca.crt`). Prereleases (`0.0.XX-debug`) ship `enterpriseDebug`, built locally and
attached to the release by hand.

**Before any `assemble*Release`: build the console first** (`npm run build` in `apps/mewbo_console`,
which also emits `dist/widget-host/`). A RELEASE build FAILS HARD when that bundle is absent
(`syncWidgetHostAssets<Variant>` — a release must never silently ship the widget feature
advertised-but-broken); debug builds only warn. Aura releases are built LOCALLY, so this is a
one-command prerequisite, not CI wiring.

## Device matrix

| Tier | Device | Use for | Hard limits |
|---|---|---|---|
| 1 | redroid container (`tools/redroid/`, `localhost:5555`) | UI, chat, streaming, reducer work | AOSP: **no `SpeechRecognizer`, no TTS engine** → always `FakeTranscriber`/`FakeSynthesizer`; software GPU → FPS numbers meaningless; assistant role/gesture not representative |
| 2 | physical Pixel over Wi-Fi adb | assistant role, gesture overlay, real STT/TTS, haptics, final acceptance | needs a human in the loop |

Container ops (binder module load, subnet pin, `data/` wipe, backend line-of-sight, GMS variant) live
in [`tools/redroid/CLAUDE.md`](tools/redroid/CLAUDE.md) — read it before touching the container.

## Hard-won build facts (don't re-derive)

- AGP 9.2.1 has **built-in Kotlin** — applying `org.jetbrains.kotlin.android` hard-errors. Its
  internal Kotlin is pinned to 2.4.0 via `buildscript classpath` in the root build file to match the
  compose/serialization plugins; jvmTarget lives in top-level `kotlin { compilerOptions {} }`.
- compileSdk 37 is forced by AAR floors (lifecycle 2.11, hilt-nav-compose 1.4, mikepenz markdown
  0.43) — don't "fix" it back to 36.
- KSP 2.3.9: Hilt modules/`@Provides` must never be `internal` (upstream codegen break).
- dagger 2.60's POM omits `error_prone_annotations` but its ViewModel-component codegen references
  it — the catalog carries the dep explicitly; removing it breaks the first `@HiltViewModel`.
- material3 1.4.0's `MaterialExpressiveTheme`/`MotionScheme` are `internal` (unreachable, and `javap`
  misleadingly shows them public — Kotlin visibility is frontend-enforced). Plain `MaterialTheme` +
  `AuraMotion` tokens is the settled approach, not a stopgap.
- Gradle 9.6 removed `project.exec` — build-script subprocess calls use `ProcessBuilder` (see the
  release-signing keytool bootstrap).
- `android.net.Uri` can't be hand-doubled in plain-JVM unit tests (package-private ctor, real methods
  throw in the stub) — the catalog carries `mockito-core` solely for tests needing a behaving `Uri`.
  Usage + other JVM-test-only traps: `app/src/test/java/com/mewbo/aura/CLAUDE.md`.
- `androidx.compose.material:material-icons-extended` is BOM-managed by the compose BOM → 1.7.8. R8
  strips the unused glyphs in `release`; `debug` carries the full aar. Its glyphs DEPEND on
  `material-icons-core`, so a jar grep for a CORE glyph (`Build`, `Settings`) inside the extended aar
  returns 0 — expected, not a missing dependency.

## Recurring-401 rule + credential seeding

A 401 from the app on the dev device almost always means **the app's stored API key is gone**
(container recreated, `data/` wiped, or a fresh install) — NOT a backend/auth bug. Don't
re-diagnose; re-seed deterministically (debug builds only):

```bash
adb -s localhost:5555 shell am start -n com.mewbo.aura/.MainActivity \
  -e seedBaseUrl http://api:5125 \
  -e seedApiKey "$(grep '^MEWBO_MASTER_API_TOKEN' "$(git rev-parse --show-toplevel)/.env" | cut -d= -f2)"
```

Never hand-type the key through the Settings UI in automation — `adb input text` mangles long secrets.

## Parallel agents in one worktree

Multiple implementer agents share this working tree concurrently. Three mechanisms have destroyed or
misattributed work here:

- `git checkout -- <foreign-path>` reverts to the last COMMIT, destroying any OTHER agent's
  uncommitted edits to that path — not just your own.
- A tree-wide `git stash` (even "isolated verify, restore right after") opens a window where every
  other agent's `ls`/`Read` sees the pre-stash tree — reads as a false "work lost" alarm.
- A bare `git add <your files>` + bare `git commit` commits the WHOLE shared index, including any
  OTHER agent's already-staged, not-yet-committed files.

Rules: never run `checkout --`/`clean`/`reset`/`stash` on a path outside your own lane; commit with
an explicit pathspec (`git commit -m "..." -- <exact paths>`) or inspect `git diff --cached --stat`
first; commit your own coherent slice early rather than waiting for a whole-tree green. Verify tree
state with `git diff`/`git status`, never a bare `ls`/`grep`/`Read` — those can return stale reads
mid-churn. Gradle's `UP-TO-DATE` is untrustworthy here too — force `--rerun` on at least one
verification pass before trusting a result.

Run `:app:lintDebug` at every wave boundary, not just at the end — a missing `VIBRATE` permission
silently no-op'd every haptic because lint never ran until the final gate.

## Debug-variant tooling (src/debug)

Debug builds ship three preview hosts (`LivenessShowcaseActivity`, `ChatPreviewActivity`,
`AssistOverlayPreviewActivity`), the fake voice pipeline, and the **mock backend** — a debug-only
`OkHttpClient` interceptor giving every session HTTP/SSE call a canned scripted response, so
on-device overlay/gate testing costs zero LLM tokens even with auto-listen firing a query on every
invocation. Full inventory: [`debug/CLAUDE.md`](app/src/debug/java/com/mewbo/aura/CLAUDE.md); mock
mechanics: [`mock/CLAUDE.md`](app/src/main/java/com/mewbo/aura/mock/CLAUDE.md).

```bash
adb -s <serial> shell am start -n com.mewbo.aura/.ui.aurora.LivenessShowcaseActivity
adb -s <serial> shell am start -n com.mewbo.aura/.MainActivity -e mockBackend true
```

## Package layering — dependencies flow strictly down

```
ui/ (Compose)  →  viewmodels  →  data/ (repos → sse/api → model)
                       ↘  voice/ (Transcriber · Synthesizer seams)
```

- `data/` has **zero Compose/Android-UI imports**; `ui/` never touches OkHttp/Retrofit — all I/O
  goes through repositories.
- All audio/speech code lives in `voice/` behind the `Transcriber` and `Synthesizer` interfaces. No
  app-layer audio code outside `voice/`.
- **A capability advertised to the backend is serviced at the seam EVERY host shares, never at one
  host's ViewModel.** `RunRepository.live()` builds `DeviceToolDispatch` into the flow it hands out,
  so a new entry point can't silently re-break it. Advertising in `sendQuery` while wiring the answer
  into `ChatViewModel` cost 66.8s overlay turns (two 30s server timeouts) once. Mechanics:
  [`data/CLAUDE.md`](app/src/main/java/com/mewbo/aura/data/CLAUDE.md) § "Device tools".
- **One chat composable tree (`ChatTranscript`), reused as a COMPONENT by both hosts — never
  forked.** `MainActivity` nav is the ONE app-navigation host; the assist overlay ALSO renders chat
  (a `ResponseCard` for the first turn's streaming response, the same `ChatTranscript` `ChatSurface`
  uses), then hands off to `MainActivity` from the second interaction onward. Contract:
  [`ui/overlay/CLAUDE.md`](app/src/main/java/com/mewbo/aura/ui/overlay/CLAUDE.md) and
  [`voice/CLAUDE.md`](app/src/main/java/com/mewbo/aura/voice/CLAUDE.md).

## Turn-completion notifications (`notify/`)

A `dataSync` foreground service follows a backgrounded run to its terminal event and posts a
completion notification (mechanics in
[`notify/CLAUDE.md`](app/src/main/java/com/mewbo/aura/notify/CLAUDE.md)). App-level consequence:
**`RunRepository` is `@Singleton`** — the watcher is a THIRD follower of `RunRepository.live()`
(after chat and the overlay) and must share the ONE multicast SSE connection, which only holds if
all three share the repo instance.

## Apps (Mewbo Apps sub-product) — `ui/apps/` + `data/repo/AppRepository`

Native gallery/detail/create screens for the Mewbo Apps sub-product (LLM-built, trigger-maintained
mini apps). The detail screen renders the app's stlite frontend through the SAME WebView seam widgets
use.

- **`AppWebView` reuses `buildStliteWebView` — never a second WebView.** The widget card's factory
  was already payload-agnostic (it takes a `messageJson: String`) and is reused verbatim. Its
  ready-signal-gated `postPayloadOnce()` still owns the FIRST payload delivery (never
  `onPageFinished` — the page posts `mewbo-widget-host-ready` when the kernel is live).
- **The refresh repost goes through an `AndroidView` `update` block, guarded by the WebView's
  `tag`.** `factory` runs ONCE, so a later `refresh()` (toolbar, or after a trigger pause/resume)
  that mints a new token + `messageJson` had nowhere to go. The `tag` doubles as "the payload
  currently loaded": it is stamped in `factory` too, so the FIRST `update` (which `AndroidView` fires
  immediately after `factory`) sees an equal tag and SKIPS — leaving the true first delivery to
  `postPayloadOnce()`, never racing it. A LATER recomposition where `messageJson` actually differs
  posts via `evaluateJavascript` + restamps the tag; an unchanged `messageJson` is a no-op (the host
  page's `render()` tears down and remounts the kernel on every post — a real cost, not free).
- **Theoretical ready-gate race (documented, unreachable in practice).** The `update`-block repost
  bypasses `postPayloadOnce()`'s ready-gate, so IN THEORY a pre-ready repost is dropped and the ready
  signal then delivers the stale factory payload. Unreachable in practice: the ready signal fires
  local-asset-fast while a refresh needs a human tap + a network round-trip. Don't add a second gate
  without a real repro.
- **Payload = `mewbo-app-payload`, mirroring the console `host.ts` contract EXACTLY**
  (`{type:"mewbo-app-payload", payload:{entrypoint, files, requirements, app_context:{token,
  api_base, app_id}}, theme}`). Verified field-for-field against
  `apps/mewbo_console/src/widget/host.ts` `parseAppMessage` + `types.ts` `AppContext` — a field
  rename here is a cross-surface break. `token` is the whole `token_id` (the bearer credential);
  `api_base` (NOT `base_url`).
- **`apps` capability advertised UNCONDITIONALLY.** `DataModule`'s `AuthInterceptor` sends
  `X-Mewbo-Capabilities` as a comma-joined list with `apps` always present (`stlite` stays gated on
  `streamlitWidgetsEnabled`) — comma-separated parsing verified against `backend.py`. Trigger
  pause/resume reuses the EXISTING global `PATCH /api/triggers/{id}`; if the API ever ships an
  app-scoped pause route it is a one-method swap in `AppRepository.setTriggerPaused`.
- **`AppRepository` is `@Singleton`**, cache-then-refresh like `SessionRepository`; mutations degrade
  to `null`/`false` on failure. The creation flow follows `RunRepository.live(sessionId)` for
  `SessionEvent.AppReady`, rendering a light "Building…" line, not the full `ChatTranscript`.
  `AppReady` is dropped by `TranscriptReducer` (never a chat item).
- Accepted gap: `app_updated` is not consumed — an open detail screen refreshes manually only
  (mirrors the console + API side).

## Project context — a session's directory MOVES mid-run

A session can run in auto workspace mode (`context.project == "auto"`), where the model picks the
project itself via `list_projects` / `switch_project` and may switch again later. Consequences for
this client (full detail in
[`data/model/CLAUDE.md`](app/src/main/java/com/mewbo/aura/data/model/CLAUDE.md)):

- **`lastContextProject` is live state, not a hydration-time read** — a `switch_project` call
  persists a fresh `context` event, so the project is folded per event rather than scanned once.
- **⚠️ Use `as? JsonPrimitive`, NEVER the `jsonPrimitive` accessor, on this path.** The accessor
  THROWS on a nested object or array; on the live per-event path that takes the run's collector down
  with it.
- **`isContextEvent` exists because a bare `String?` collapses two facts** — "a context event naming
  no project" (session on a temp directory) versus "not a context event at all" (nothing to adopt).
- **The switch is a promoted tool card**, so BOTH halves of the allowlist ship together —
  `PromotedTools.IDS` and the `ToolCardRegistry` branch
  ([`ui/chat/toolcards/CLAUDE.md`](app/src/main/java/com/mewbo/aura/ui/chat/toolcards/CLAUDE.md)).
- **The result is JSON with fixed keys, not prose** — parse it, never regex the sentence. A REFUSED
  switch arrives as the shared `{"error": {code, message}}` envelope and must not render as a
  successful move; anything unparseable degrades to `GenericToolCard`.

## Atomic class paradigm (Kotlin flavor)

Same rule as the rest of the monorepo: every behavior/feature/strategy is one atomic class — state as
constructor-injected attributes (Hilt), behavior as methods, pure helpers as `companion object`
functions. Sealed interfaces for closed unions (`SessionEvent`, `OrbState`, `TranscriberEvent`) so
`when` is exhaustive and the compiler flags drift. A free function that grows past two args or starts
carrying state across calls becomes a class. No util-file dumping grounds.

## Theme discipline

No `Color(`, dp radius, or `spring(` literals outside `ui/theme/`. All tokens come from `AuraTheme` /
`AssistantExtras` (see [`ui/theme/CLAUDE.md`](app/src/main/java/com/mewbo/aura/ui/theme/CLAUDE.md)).

## Iconography

Icon sourcing has ONE order of preference — never hand-roll an icon when an off-the-shelf one covers
the case:

1. **`material-icons-extended` first** for any NEW glyph. A semantically-correct extended glyph beats
  a hand-rolled path every time.
2. **`ui/chat/ChatIcons.kt`'s hand-rolled set is FROZEN LEGACY.** Existing reuses stay (a reused
  hand-rolled glyph is not a "new hand-roll" — `ChatIcons.Clock`/`Fullscreen`/the drawer's archive
  glyph are legitimate legacy reuses), but do NOT add a new hand-rolled path there. Precedent: the
  recents-header filter icon's hand-ported `filter_alt` path was DELETED in favor of
  `Icons.Default.FilterAlt`.
3. **Brand/provider LOGOS have no Material analog, so they ARE hand-converted** —
  `ModelProviderIcons` uses `res/drawable/ic_provider_*.xml` converted from MIT `@lobehub/icons`
  MONOCHROME SVGs (single `currentColor` path → tintable, never the colored variants, per theme
  discipline). Each drawable carries its upstream-source + license as an XML provenance comment.

Two standing rules: **one icon weight per surface** (the drawer/settings are all `Icons.Filled.*`, so
a new drawer glyph is Filled); and the ephemeral **Temporary project uses `Schedule` (a clock), NOT
an `AutoDelete`/trash glyph** — a delete-affordance glyph next to a selectable, tappable row misreads
as "tap to delete".

## Testing

`ANDROID_HOME=$HOME/android-sdk ./gradlew :app:testPublicDebugUnitTest` — test sources are
flavor-agnostic, but `testDebugUnitTest` does not exist under the distribution flavors, so pick a
flavor. The reducer (`TranscriptReducer`), `SentenceChunker`, and `AssistTurnMachine` suites are the
contract tests; any serialization/event change must keep them green. Tests exercise real code paths
(real JSON fixtures from the live API shape), stubbing only transport. Read
[`test/CLAUDE.md`](app/src/test/java/com/mewbo/aura/CLAUDE.md) for the JVM-test-only house idioms
before writing a new test double.
