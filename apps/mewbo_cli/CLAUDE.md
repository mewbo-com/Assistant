> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo CLI — terminal UI guidance

Scope: `apps/mewbo_cli/` only — the Textual TUI, the plain fallback, and how CLI
output is produced.

## Two surfaces, one engine

- **`MewboApp(textual.App)`** (`tui/app.py`) — the interactive surface. ONE event
  loop owns the alternate buffer; every interaction is a widget. Layout:
  `Vertical(Header, Horizontal(Transcript, Sidebar), Input, footerbar(StatusLine, Footer))`,
  driven by a `ScreenState` enum (onboarding|landing|chat); the sidebar hides via
  CSS below a width breakpoint. The turn loop runs on a thread worker.
- **Plain fallback** — `cli_master._run_plain_repl` (and the `--query` one-shot)
  for no-TTY / pipes / `MEWBO_DISABLE_TEXTUAL=1` / `cli.disable_textual`. A
  prompt_toolkit REPL that renders Rich panels after each run. It never reaches
  `_run_app`, which is why several App-only capabilities simply do not exist
  there (ask-user questions, modals, live streaming).

The CLI is a **strictly-local engine**: core + tools only, never imports
`mewbo_api`. It runs `SessionRuntime`→`ToolUseLoop` in-process and the local JSONL
transcript is authoritative.

**The composer (`#input`) is NOT docked.** It flows above the bottom bar with a
margin; the `StatusLine` and the keybinding `Footer` share ONE bottom-docked
`#footerbar` so they stack flush. Two competing `dock: bottom` widgets overlap.

**Do not run Rich rendering and Textual concurrently.** Textual runs a blocking
app loop; mixing it with live Rich rendering or spinners deadlocks or corrupts
terminal state. This is why every interactive surface belongs in the App.

## Extension seams — add an installer, never edit `app.py`

Four stable seams (`tui/seams.py`), exposed as `app.messages` / `app.sidebar_slots`
/ `app.permission` / `app.input`:

| Seam | Owns |
|---|---|
| `MessageRendererRegistry` | keyed message/tool renderers + generic fallback |
| `SidebarSlotRegistry` | ordered sidebar slot factories |
| `PermissionGateway` | the `approval_callback` threaded into `run_sync` |
| `InputGateway` | input completion source |

Children mount at the seams via a post-mount `installers` hook —
`MewboApp(..., installers=[...])` runs each `AppInstaller` at the tail of
`on_mount`, each in a try/except so a failing installer degrades to a dim notice
rather than breaking the App. `cli_master._build_installers` assembles them, which
is what keeps `app.py` closed to modification.

## Transcript design system (the rendering contract)

Hold to this when touching `transcript_render.py` / `widgets/transcript.py` /
`widgets/fleet_panel.py`:

- **Color = identity + state · muted text = hierarchy · spinner = liveness.**
  Status is a glyph+color, never a background alone. Always go through the
  injected `Palette` / `ICONS` — never a raw hex or an inline glyph.
- **Role identity via a colored left-rail.** USER turns carry a thick `$primary`
  rail (`.t-user`); ASSISTANT turns are plain default-fg markdown, no rail. One
  blank line between blocks, extra top margin isolating each user turn. Never
  double-space.
- **Tools = inline-vs-block + collapse-by-default.** READS are a COMPACT one-liner
  (`<glyph> <label> <path> · N lines`, no body — the highest-frequency tool must
  not bloat the viewport). SHELL execs are a terminal block: `$` prompt + bold
  command + output on a `$success` rail (`.t-bash`), detected via
  `is_shell_tool()` (name-hint based, so vendored ids like `aider_shell_tool`
  match). EDIT/WRITE render `DiffView`. GENERIC/MCP unwrap the result envelope
  (shell→stdout, file→text, dir→entries) or pretty-print JSON, truncated
  `… +N lines`.
- **Plan/todo is dual-surfaced**: inline in the transcript AND a pinned sidebar
  dock with an `N/M` count and tri-state per item (✓ done · → in-progress, exactly
  one · • pending).
- **A proposed plan is ONE decision surface, never raw JSON.** A halted plan-mode
  run renders `plan.md` through the `plan_proposal` renderer (bordered card,
  `$accent` `.t-plan_proposal` rail, full `render_markdown`, capped preview) and
  the raw planner `spawn_agent`/`check_agents` steps are suppressed.
- **Never leak raw logs or a usage footer into the transcript** — vitals live in
  the footer status line + sidebar gauge.

Markdown goes through the ONE renderer (`aider_ui.render_markdown`). Thinking is a
3-mode `ThinkingCollapser` (collapsed / tail-window / expanded); tool output
truncates via `ToolOutputCollapser` with a per-edit diffstat.

**Streamed text goes through `StreamingMarkdown`** — a stable-prefix cache that
re-renders only the trailing partial up to the last complete markdown block
boundary, memoized per `(prefix, width, theme_version)`; `append_stream` composes
cached `stable` + fresh `partial` into the live `Static` (test seam:
`_full_render_count`). Re-rendering the whole buffer per delta visibly flickers.

`register_transcript_renderers(registry, *, palette)` must be called **before
`app.run()`** so the renderers win the App's `has`-guarded defaults.
`register_orchestration_cards` installs ONE *wrapping* `"tool"` renderer AFTER it,
drawing cards for `spawn_agent`/`spawn_agents` (agent-launch), `check_agents`
(fleet table) and `tool_search` (discovered-tool list), and delegating every other
tool to the base renderer through a private staging registry — additive, never a
change to the render dispatch.

## `AgentTranscriptHub` — the order-preserving keystone

`tui/agent_transcript_hub.py` is the single source of truth for what happened. It
subscribes ONCE to the shared `SessionEventBus` (wired in `cli_master._run_app`) —
every agent's events flow through it, each payload carrying `agent_id`/`depth`/
`parent_id` — and demuxes into `dict[agent_id -> AgentTranscript]`, each a
**strictly-ordered append log** (text · tool · spawn IN ARRIVAL ORDER, never
grouped by type):

- `agent_message_delta` coalesces into one streamed `TextSpan` per step, closed by
  the step's `agent_message` or by any tool/spawn.
- A tool is ONE mutable `ToolCall` keyed by `call_key` (`tool_id`+input) — created
  `running` on `pre_tool_use` (the `FleetBridge` hook seam is the ONLY tool-start
  signal; the bus has none) and mutated in place to `done`/`error` + elapsed when
  its `tool_result` lands.
- `sub_agent` appends a `Spawn` marker to the PARENT's log.
- `update_todos` calls are suppressed as transcript cards (`_SUPPRESSED_CARD_TOOLS`)
  — the dock is their surface, not card spam.

The root (`depth == 0`) drives the live `TranscriptView` via an injected `RootSink`
(`cli_master._TranscriptHubSink`, marshalling with `call_from_thread`); the hub
itself is pure state. `TurnEngine(live=True)` SUPPRESSES the legacy batch
tool+assistant dump — it would duplicate and reorder what already streamed; the
plain path keeps `live=False` and the dump. `build_tool_payload` (`turn_engine.py`)
is the ONE payload extractor shared by both.

Tool cards carry three weights: `running` = accent glyph, full weight;
`done`/`error` = settled and muted (`.t-settled` lowers `text-opacity`) with
`✓`/`✗` + elapsed. The foot spinner shows the live step label and on settle
collapses to `✓ done · {N}s`.

**A new event kind needs a `_dispatch` arm — the renderer fallback will NOT save
you.** `MessageRendererRegistry.render` falls back to `_default_message_renderer`
for an unknown item **kind**, which makes the transcript look forward-compatible.
It is not: a `TranscriptItem` only ever exists because the hub's `_dispatch`
if/elif chain minted one, so an event type with no arm produces no item and the
fallback is never reached. Registering a renderer without the arm is dead code.
`generative_ui` is the worked example — `UiPanel` + `_on_generative_ui` + an
`items_for` arm + one shared `_ui_panel_item` projection, so the live sink and the
drill-in view render it identically. It renders `alt_text` (the prose rendering of
the panel), never the component tree, which a terminal cannot draw. The stored-
transcript modal (`screens/transcript_screen.py`) is a SECOND, independent render
path over raw events and needs its own arm; `_TEXT_KINDS` does not cover this one
because the prose lives on `alt_text`, not `text`.

**Deferred-sink law: never invoke the sink under `_lock`.** The sink marshals to
the UI thread and that callback re-enters the hub's read API (`root_activity_label`
takes `_lock`), so calling it under the lock self-deadlocks cross-thread — `_lock`
is an `RLock` and per-thread reentrancy does not save you. Ingest COLLECTS sink
actions into a FIFO under `_lock` and FLUSHES after release (`_enqueue_sink` /
`_flush_sink`), single-drainer so order survives concurrent `observe`. `_sink_*`
helpers snapshot their payload at enqueue time — a `ToolCall` mutates
running→settled in place. Guards: the foreign-thread lock probe and the two-thread
ordering test in `tests/test_tui_agent_transcript_hub.py`.

## Decisions: permissions, plan approval, ask-user

All three bridge a worker thread to the App loop with the SAME blocking pattern —
`app.call_from_thread(app.push_screen_wait, modal)`. Do not invent a second one.

- **Permissions** (`tui/permission_service.py`, `widgets/permission_modal.py`).
  `PermissionService.decide(step)` is a layered chain that never raises (errors
  deny): skip-predicate → mode auto-accept-edits (READ/WRITE tiers) → persisted
  **deny** rule → persisted **allow** rule → in-memory session grant → modal.
  Rules persist as `fnmatch` globs (`tool`/`action`) in `~/.mewbo/permissions.json`
  (or `$MEWBO_HOME`) via `PermissionRuleStore`; session grants stay in memory.
  `RiskTier` classifies READ/WRITE/EXEC (EXEC for bash/`execute`;
  `_WRITE_TOOL_PATTERNS` elevates destructive-named tools to WRITE even on a `get`)
  and drives badge colour + default focus. The modal embeds `DiffView` for edits or
  the full command for bash; `a` allow-once · `s` allow-session · `d`/**esc** deny
  (esc = safe default). LLM-sourced fields are `rich.markup.escape`'d. Permission
  modes (`normal → auto-accept-edits → plan`) cycle on **shift+tab**.
  `_confirm_rich_panel` survives ONLY as the plain fallback — do not extend the
  `console.input` path; add to `PermissionService`.
- **Plan approval** (`widgets/plan_modal.py`, via the `TurnEngine`
  `plan_approval_resolver`): `a` = approve & execute (`runtime.approve_plan` +
  mode plan→act + an act-mode follow-up run), `k`/`esc` = keep planning, `r` =
  reject. **There are no plan slash commands.** The plain fallback approves via
  `/continue`.
- **Ask-user questions** (`tui/question_dispatcher.py`, `widgets/ask_user_modal.py`).
  Core dispatches through the process-wide `mewbo_core.ask_user.QuestionDispatcher`
  seam; `_run_app` registers ONE `TuiQuestionDispatcher` and binds
  `AskUserQuestionTool(session_id)` per run via the `TurnEngine`
  `extra_session_tools_factory`. **Interactive-TTY only** — the plain paths
  register nothing and the tool does not exist there, so nothing ever blocks on an
  absent human. Registration is cleared in a `finally` after `app.run()`.

**The ask-user seam is a FACTORY, and it must reach BOTH run-driving paths.** A
factory because the tool binds the session id at construction while the id moves
under the engine (`/new`, `/resume`, fork) — a captured list dispatches the answer
against a stale session. The query turn resolves it (`turn_engine.run_query` +
`_approve_pending_plan`) and the COMMAND path inherits it through
`CommandContext.extra_session_tools_factory` / `.extra_session_tools()`
(`cli_context.py`), the same optional-collaborator idiom as `approval_callback` /
`hook_factory`. `/continue`, `/retry`, `/edit` and `_approve_pending_plan` all pass
it into their `run_sync`; without that, a RECOVERED session silently loses the tool
for the whole run — on the very path most likely to need a human decision.
`/summarize` and `/compact` are compaction, not agent turns, and correctly bind
nothing. Guard: `tests/test_tui_turn_engine.py` drives the real command registry and
asserts the factory's own list reaches `run_sync`.

**`AskUserModal`:**

- 1-4 questions (`OptionList` radio single-select, `SelectionList` multi-select,
  plus an ever-present "Other" `Input`); digits `1-4` pick in the focused question,
  `ctrl+s`/Enter submits, **esc = decline**.
- `collect_answers` (pure, unit-tested): free text WINS over a selection, and ANY
  unanswered question returns `None`, keeping the modal open rather than dismissing
  a half-answer. `collect_notes` blanks to `None`, never `""`.
- **The timeout must live INSIDE the modal.** A `wait_for` raced against
  `push_screen_wait` cannot implement it: that call returns only once the modal
  calls `dismiss()`, and cancelling the awaiting coroutine does not reach the
  executor thread blocked inside it — the modal stays parked and the thread wedged
  for the life of the process. `on_mount` arms
  `set_timer(timeout_seconds, self._expire)`.
- `_expire` settles with the `ASK_USER_EXPIRED` sentinel, **not `None`** — `None`
  means declined. `_settle` is the single funnel all three exits dismiss through: it
  cancels the timer and guards a double dismiss when a keybinding and the timer land
  in the same tick. `action_submit` stamps `self.submitted` BEFORE settling, so if
  `_expire` wins that race the dispatcher still reads the answer off the live modal
  and reports `answered` — a genuine answer outranks a withdraw that raced it.
- The budget is one static hint line, never a live countdown. `notes_placeholder`
  renders one group-level `Input` below the questions, outside the per-question XOR
  (a user can pick an option AND add a caveat), riding the modal's `notes` attribute
  rather than widening the dismiss type.
- **Decline and timeout are TERMINAL — the next message is the late answer.** A
  dismissed modal leaves nothing mounted listening for that `call_id`, and the
  `timed_out` tool result already tells the model to expect a late answer as text.
  Do not re-mount a dismissed modal to imitate the browser console's persistent
  card; that adds a second answer channel and a claim on the composer.

## Input and completion

`tui/input/` + `widgets/input_area.py` — ONE sigil-dispatched field:
`@`→files (caret-anchored OptionList overlay; tiered `FileCatalog` ranking:
exact-stem > basename-prefix > path-segment > substring; shift-arrow inserts
without dismissing), `/`→commands+skills+custom+MCP-prompts (fuzzy,
match-highlighted, arg-hints), `!`→bash. `CompletionEngine` is I/O-free ranking and
never raises (degrades to no suggestions). `CustomCommandLoader` reads
`.claude/commands/*.md` + `~/.mewbo/commands/*.md` (frontmatter `description` /
`argument-hint` / `allowed-tools`, `$ARGUMENTS`, subdir namespacing
`frontend:component`, project-overrides-user). `PromptHistory` lives at
`~/.mewbo/cli_history` with `ctrl+r` reverse search. The Textual `CommandPalette`
is fed by `MewboCommandProvider` using an **instance-level `COMMANDS`**, never the
class global.

**Queued-while-busy:** `Enter` during a run queues the message, `esc` pulls the
latest back; `_run_turn` flips `set_busy()` and drains `drain_next()` after the turn.

Plain fallback only: `cli_completer.MewboCompleter` drives `PromptSession`
completion (`complete_while_typing=True`) over the shared `FileCatalog` and the
command + skill lists. Never raises out of `get_completions`.

## Sidebar, fleet and status

**The hub is the single fleet source.** `fleet_rows()` returns per-agent `FleetRow`
rollups (model / tool-count / tokens / status / timestamps — root tokens from
`llm_call_end` cumulatives, elapsed from `started_at`/`stopped_at` stamped in-hub)
and `items_for(agent_id)` the ordered items for drill-in. `FleetBridge`
(`tui/fleet_bridge.py`) is the per-run `HookManager` (`on_agent_start/stop` +
`pre/post_tool_use`); it holds no state of its own and only marshals a UI refresh
onto the UI thread on each run event.

`FleetPanel` (`widgets/fleet_panel.py`) is an `OptionList` of one-glance summaries —
`<glyph> <label> · <model> · <N tools> · <elapsed> · <in→out>`, glyph `●`/`✓`/`✗`,
**NO tool-name dump** (count only). Selecting a row calls the injected `on_select`
→ `FleetDrillController.open`, which **swaps `#transcript` for a `FleetDrillView`
in place** (breadcrumb + per-agent footer + the agent's transcript through the SAME
renderers, reusing `.t-settled`); `esc` restores the live transcript. The panel is
the persistent navigator, so root↔child↔child navigation all re-targets in place.

`make_sidebar_installer` registers three ordered `SidebarSection`s
(`widgets/sidebar.py`): **Fleet** (10) · **Plan** (20) · **Context** (30).

**No facet is shown twice between sidebar and footer.** `StatusBar` (sidebar slot)
is ONLY the context/cost gauge — `󰓅 ctx %` (>80% warn, `~` = estimate) + cost;
model/tokens on its `StatusState` feed the meter but are not rendered. The IDE-style
`StatusLine` (`widgets/status_line.py`, docked in `#footerbar`) owns identity and
volume: `󰀄 user@host · model · 󰉋 cwd · 󰘬 branch 󰏗 stash · ↑in ↓out` (session
tokens, never a subscription quota). Both run off ONE refresh tick
(`make_statusline_installer`, offloaded via `asyncio.to_thread`). Glyphs live in
`cli_icons.py` — never inline one.

**Todos are authoritative, not inferred.** The Plan dock renders core's `todos`
event (an agent calls `update_todos`); the hub ingests it in `_on_todos` (mapping
the event's `completed` → the panel's `done`) and exposes `root_todos()`, wired as
the `TodoPanel` `todo_provider`. Each event fully REPLACES the dock.

**`ThroughputMeter`** (`tui/status/throughput_meter.py`, mirroring `ContextMeter`) —
one atomic class per agent: a `Phase` enum (idle/uploading/reasoning/streaming/
running_tool/stalled), EWMA output tok/s (live `len/4`, reconciled to the
authoritative `llm_call_end` output), TTFT, and a monotonic stall timer. Fed from
the hub's ingest points, with the hub stamping arrival timestamps from its own
clock, so it is deterministic under a fake clock. The `reasoning`
phase (a reasoning model legitimately emits no deltas while thinking) is inferred
via core's `model_supports_reasoning_effort` and is EXEMPT from the stall alarm.
Rendered twice: the root meter drives the foot activity label via
`TranscriptView.set_activity_label_provider`, pulled on the existing **0.1s spinner
tick** so a hung agent visibly flips to `stalled` with no new event
(`streaming ↓82 tok/s` · `running bash… (12s)` · `⠿ stalled 30s`); and per-agent as
a terse `FleetRow.throughput` facet, chiefly for the sub-agent-hang case.

## Session screens and keybindings

`tui/screens/`, `tui/session/`, `tui/keybindings.py`:

- `ResumeScreen` (`ctrl+s` / `/resume`) over `list_sessions()`.
- `TranscriptScreen` (`ctrl+o`), reusing `DiffView`.
- `RewindCheckpointer` / `/rewind` reverts code AND conversation together: a
  pre-restore safety snapshot of tracked **and untracked** files, then
  `git restore` + `git clean` — fully recoverable, no-op outside git.
- `AutoTitler` — idempotent, first-message, reuses `title_generator`.
- `DialogFactory` pickers mirrored as `ModalScreen`s (`screens/dialogs.py`).
- Declarative `KeybindingConfig` (`~/.mewbo/keybindings.json` overrides, a bad file
  is skipped) attached on the App so the `Footer` shows them.

Global bindings: `ctrl+o` transcript · `ctrl+s` session switcher · `ctrl+l`
clear+redraw (corruption escape hatch) · `ctrl+p` palette · `ctrl+r` history search
· `shift+tab` permission-mode cycle. **`shift+tab`, `ctrl+p` and `ctrl+r` are owned
elsewhere — do not re-bind them in a new screen.**

## Theme and DiffView

Never reach for a raw color or bespoke diff rendering.

- **`cli_theme.py`** — semantic `Palette` (frozen dataclass; the binding 15-role
  contract in `ROLE_NAMES`: `primary, secondary, accent, fg_base, bg_base, muted,
  border, error, warning, success, user, assistant, diff_add, diff_del, diff_eq`).
  `build_theme(palette, *, name, dark)` → `textual.theme.Theme`: native semantic
  roles map to `Theme` fields (Textual generates shades); the rest — plus
  `fg_base`/`bg_base` — go in `variables`, so **all 15 roles surface verbatim as CSS
  vars** (`$diff_add`, `$fg_base`, …). Built-ins `DEFAULT_PALETTE` (dark),
  `HYPER_PALETTE`, `LIGHT_PALETTE`. `ThemeManager` (atomic class, injected — never a
  global) loads JSON from `~/.mewbo/themes/` (bad files skipped, never crashes) and
  owns `register_all(app)` / `list_themes` / `switch` / `apply_command`.
  `detect_terminal_is_dark()` / `auto_palette()` for auto light/dark.
- **`cli_icons.py`** — `ICONS` (read-only): `check`, `cross`, `error`,
  `tool_pending`, `spinner` (braille frames), `agent_state` for the 6 lifecycle
  states, and the status-line glyphs (`branch`/`stash`/`gauge`/`user`/`folder`).
- **`cli_diffview.py`** — ONE reusable `DiffView(Widget)` for BOTH the approval
  modal and the transcript. Colors come from the injected `Palette`; difflib hunks →
  per-side Rich `Syntax` + add/del tint; split above `split_min_width` cols else
  unified, `t` toggles; highlight hash-cached by `(content, syntax_theme, palette)`;
  honest truncation (`e`/`enter` expands, `←/→` h-scrolls).
- **`cli_gradient.py`** — `gradient_text(text, colors)`; splash + dialog titles only.

## Local-first, opt-in remote (`cli_remote.py`)

ONE CLI-only config block `cli.remote = {base_url, token}` (`extra="forbid"`,
`token` is `x-secret`; `enabled` ⇔ non-empty `base_url`). All three remote
behaviours reuse existing seams — no bespoke launcher, no engine relocation:

- **Transcript sync** — `RemoteTranscriptSync` registers ONCE on the shared
  `SessionEventBus`. It filters to the live active session (a `session_id_provider`
  reading `state.session_id`, so it follows switches), enqueues non-blocking on the
  append hot path, and a daemon worker fire-and-forget POSTs batched events to
  `<base_url>/api/sessions/<id>/events` with `X-API-Key`. **A failed POST is logged
  and dropped — local JSONL stays truth.** Tests drive `flush()` with
  `autostart_worker=False` and an injected poster.
- **Product tools** — `mewbo_mcp_server_config(base, token)` synthesizes the Mewbo
  MCP entry (`transport: streamable_http`, `url=<base>/mcp`, `Authorization:
  Bearer`) and feeds it to `load_registry(extra_mcp_servers={"mewbo": …})`, the
  registry seam plugins use. The CLI passes its registry explicitly to `run_sync`,
  bypassing the orchestrator's rebuild so the root sees the tools. Only pass the
  kwarg when a server exists.
- **Provenance** — `surface=cli` is threaded per-run via
  `run_sync(source_platform="cli")`. The local-vs-synced facet rides the existing
  `TraceProvenance` context seam: a synced session writes ONE
  `{"transcript_sink": "synced"}` context event, which `derive` promotes to a
  `transcript_sink:` chip. Absence == local. **Do NOT write a `source_platform`
  context event** — `SessionOrigin.classify` reads it as CHANNEL.
- `HeaderContext.transcript_sink` (`local-only` / `remote: <base_url>`) renders one
  `sink` row in the wide/normal header.

## Console logging vs the alt-screen

The App owns the alternate buffer, so **no loguru sink may write to the terminal
while it runs** — a live stderr sink interleaves with Textual's rendering and
corrupts the composer/footer (acute under `-vv`). `_run_app` calls
`_redirect_app_logs_to_file(args)` FIRST: absent an explicit `--log-file` it routes
all detail to `~/.mewbo/cli.log` (overwrite per launch) via
`set_cli_log_file(..., quiet_console=True)`, dropping the console sink. `-vv`
therefore raises *captured* detail off-screen. The plain paths never reach
`_run_app` and keep console logging; per-run detail is captured per-session either
way by core's `session_log_context` file sink.

## Commands (keep in sync)

Registered in `cli_commands.py`: `/help` · `/exit`|`/quit` · `/new` · `/session` ·
`/summary` · `/summarize`|`/compact` · `/status` · `/terminate` · `/tag [NAME]` ·
`/fork [TAG]` · `/plan on|off` · `/skills [name]` · `/plugins` · `/mcp
[select|init]` · `/config init` · `/init` · `/models` · `/automatic` · `/retry` ·
`/continue` · `/edit` · `/mode plan|act` · `/tokens` · `/budget`.

Registered in `cli_master.py` (App-side handlers): `/context` · `/resume` ·
`/rewind [N]` · `/keybindings`.

`/tag`, `/fork`, `/models` and `/mcp select` open a dialog when their argument is
omitted (TTY only). In the plain fallback, `/continue` **approves** a pending plan
proposal instead of resuming.

## Config knobs (UI-relevant)

| Knob | Effect |
|---|---|
| `llm.api_base` | printed in the ready panel |
| `llm.default_model` / `llm.action_plan_model` | used when `--model` is unset |
| `cli.disable_textual` | force the plain fallback |
| `cli.remote.base_url` / `cli.remote.token` | opt-in remote sync + MCP product tools |
| `runtime.cli_log_style` | default log styling for CLI runs |
| `configs/mcp.json` | MCP server config used for discovery |

⚠️ **`cli.statusline.script` / `cli.statusline.interval_seconds` are documented by
`StatusLineRunner` but are NOT reachable from `configs/app.json`.** The runner reads
them with `get_config_value("cli", "statusline", …)`, but `CLIConfig` declares no
`statusline` field, so Pydantic drops the whole subtree at validation and the lookup
always returns `None`. Today the script is only settable through the
`StatusLineRunner(script=…, interval=…)` constructor. Declaring the field on
`CLIConfig` is the fix; do not chase it in the runner.

Run flags parsed in `run_cli`: `--fallback-models` (comma-split, singular alias
`--fallback-model`) and `--no-fallback`, which sets the chain to `()` — an explicit
empty tuple that OVERRIDES the `llm.fallback.*` config default, distinct from `None`
which uses it.

## Post-run notices (both surfaces)

`cli_notices.py` is shared by the App and the plain fallback. Core emits
`llm_retry` / `llm_fallback` / `recovery` to the **transcript**, not via hooks, so
the live surface never sees them; `_print_resilience_events` replays them after each
run as dim lines, scoped to events after the last `user` event so multi-turn
sessions do not re-print prior turns:

- `llm_retry` → `↻ Retrying {model} after {error_type} ({attempt}/{max}, {delay}s)`.
- `llm_fallback` → `⤳ Falling back: {from} → {to} ({reason})`, appending
  ` [pinned for run]` when the payload carries `sticky: true`.
- `recovery` halt → `⊘ Halted: repeated '{tool}' with no progress — /retry or
  /continue to recover`.

It returns `bool` (whether a halt line printed) so `_maybe_print_recovery_hint` can
suppress the generic hint when the halt line already names those commands. The hint
prints only when the run ended in a recoverable terminal state (`error`,
`max_steps_reached`, `halted_no_progress`, `canceled`) AND a prior user turn exists.

## Interaction design — converge, don't invent

Before adding a command, mode, or widget for any user-facing interaction (approval,
confirm, pick-one, diff, progress), do both, *before writing the spec*:

1. **Reuse the existing in-repo affordance** — `PermissionModal` /
   `DialogFactory.select_one` / `ModalScreen` for decisions, `DiffView` for edits,
   the plan/todo dock for progress. A new surface duplicating one is a DRY violation.
2. **Match the convergent reference UX.** Where leading terminal coding agents have
   solved the same interaction (plan approval, permission prompts, diffs, todos),
   check how and converge. Re-derive the pattern, never copy names.

**Decisions are selectable prompts/modals, NEVER new slash commands** — an
`/approve` + `/reject` pair for a decision the modal already handles is a
regression, not a feature.

Prefer toolkit defaults; avoid bespoke widgets and heavy layouting; keep new UI
logic near existing UI code. Show tool activity before the final response, and never
print raw tool output as the answer — the core synthesizes it. Tests drive a real
CLI flow with fake tools/LLM output rather than over-mocking, and keep permission
prompts deterministic.

## Core files

| File | Role |
|---|---|
| `src/mewbo_cli/cli_master.py` | entry (`run_cli`), `_run_app`, `_run_plain_repl`, installers, Rich output sections |
| `src/mewbo_cli/tui/` | the App: `app.py`, `turn_engine.py`, `seams.py`, `agent_transcript_hub.py`, `widgets/`, `screens/` |
| `src/mewbo_cli/cli_notices.py` | resilience/recovery/usage replay, shared by both surfaces |
| `src/mewbo_cli/cli_commands.py` | command registry, model wizard, MCP listing |
| `src/mewbo_cli/cli_context.py` | `CommandContext` — state + optional collaborators shared across commands |
| `src/mewbo_cli/cli_dialogs.py` | `DialogFactory` (`select_one`/`select_many`/`prompt_text`/`confirm`), inline so scrollback survives; auto-falls back to a plain prompt with no TTY |
| `packages/mewbo_core/.../session_runtime.py` | shared runtime: `enqueue_message` (steering), `interrupt_step` |

⚠️ **`cli_agent_display.py` is unreferenced.** `AgentDisplayManager` (the Rich
`Live` agent-tree renderer) is imported by nothing — neither surface renders it.
Do not wire new work against it; the live surface is the hub + `TranscriptView`.

Keep this file updated when section layouts, dialog behaviours, seams, or
UI-related config/env change. Link to code instead of duplicating it.
