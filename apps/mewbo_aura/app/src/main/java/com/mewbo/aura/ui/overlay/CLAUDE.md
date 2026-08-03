> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Assist Overlay — ui/overlay/

Scope: `ui/overlay/` — `AssistOverlayScreen` (the `VoiceInteractionSession` content view) and its
debug host `AssistOverlayPreviewActivity` (`app/src/debug/.../ui/overlay/`). Voice-first,
first-turn-in-overlay, second-turn-onward hands off — [`voice/CLAUDE.md`](../../voice/CLAUDE.md) owns
the state machine, this file owns the render of `AssistUiState`. "Never fork chat rendering"
([`ui/CLAUDE.md`](../CLAUDE.md)) is binding here.

## Response card contract

The `Streaming` state's first-turn response renders as a floating `ResponseCard` with measured
geometry:

- Side margin **14dp** (`AuraSpacing.ResponseCard.sideMargin`) — tighter than the composer pill's own
  overlay margin (38dp, [`ui/composer/CLAUDE.md`](../composer/CLAUDE.md)); the card and pill are
  visually distinct widths by design, not a shared token.
- Corner radius **20dp** (`AuraShape.radiusCard`) — distinct from the 28dp bubble radius.
- Gap to the composer pill above it: **11dp** (`AuraSpacing.ResponseCard.gapToPill`).
- Drag handle **27×1.5dp**, 5dp ink offset from the card's top edge; a dedicated 32dp-tall touch strip
  pinned to `Alignment.TopCenter` (NOT `minimumInteractiveComponentSize`'s symmetric expansion, which
  would bleed the hit area above the card's rounded top corner) — swipe-up past a 24dp threshold fires
  `onExpand`.
- Height cap **65%** of the overlay's measured height (`ResponseCard.maxHeightFraction`), with
  internal scroll beyond that via the shared `ChatTranscript`.
- **The card yields to the IME** — it carries `Modifier.weight(1f, fill = false)` in the bottom
  `Column` (which is `Arrangement.Bottom`-packed because a weighted child expands the Column to full
  height). Measurement-order law: pill/error/chip measure FIRST, the card gets exactly the leftover —
  so when the keyboard opens (`imePadding()` shrinks the column's inner max), the CARD shrinks while the
  composer stays pinned above the keyboard. The 65% `heightIn` cap alone is computed against the FULL
  window height, which never changes with the IME (static session-window frame), so relying on it
  alone overflowed the stack. Same law as the composer-options sheet squeeze: weighted content yields,
  fixed chrome never gets coerced.
- **Shrink-wraps short replies** via `ChatTranscript(..., fillParent = false,...)` — the ONE additive
  parameter that lets the card's `LazyColumn` size to its own laid-out content within the `heightIn`
  cap instead of always forcing the full slab. `fillParent` defaults `true` (byte-identical for every
  OTHER caller); the overlay is the only `false` caller. **Never fork `ChatTranscript` to get a
  different sizing mode — extend the shared component's parameter surface instead.**
- Content is the SAME shared `ChatTranscript` every other surface renders — only the container chrome
  (drag handle, controls row) is local to this file. Thumbs/share controls are deliberately absent (no
  backend semantics for either yet — a documented deviation, not an oversight).
- Swipe-to-dismiss is scoped ABOVE the card's reserved region while one is showing
  (`1f - maxHeightFraction` of screen height) — a whole-screen catcher competes with the card's own
  `ChatTranscript` scroll. Don't reintroduce a whole-screen catcher.

## No orb in the overlay (deleted by decision)

The floating/docking overlay orb was DELETED outright: the reference overlay has no floating orb (four
real-device captures — pill + card only); the orb's window-coordinate anchor-chasing was the overlay's
top defect source (a stranded-orb IME bug, a 300–600ms empty dock-slot gap, a desaturated docked
render, a tap-swallowing docked slot); and ~120 lines of the most fragile code in the tree existed only
to animate it. The brand mark lives in the response card's clay-flower typing indicator and the app
surfaces (launcher icon, `AuraSpark`) instead. **Do NOT reintroduce a floating overlay element that
tracks another composable's `boundsInWindow()`** — if a future design wants a shared-element morph
here, place both endpoints in the SAME layout tree so the layout system owns the geometry.

## Draft-clear-on-Idle law (the mic-invisibility root cause)

The `VoiceInteractionSession` composition SURVIVES a hide→show cycle (AOSP-confirmed; see
[`voice/CLAUDE.md`](../../voice/CLAUDE.md)'s session-reuse lifecycle law). Type a partial query,
dismiss WITHOUT sending, re-invoke the assistant — the SAME composition and its `rememberSaveable var
draft` come back. Nothing in `AssistTurnMachine` touches that Compose-local `draft`, so a stale
non-blank draft resolved `ComposerState.Typing` and permanently replaced the mic circle with a ghost
send-arrow — read by users as "the mic is randomly missing."

Fix: `LaunchedEffect(state is AssistUiState.Idle) { if (state is AssistUiState.Idle) draft =
TextFieldValue }` — piggybacking on a state value that already exists rather than adding machine
surface. **Any new Compose-local state in this file must ask the same question**: does it need an
equivalent Idle-keyed reset, given the composition can outlive any single invocation?

## Trailing-accessory 2-way

`FloatingComposerBar`'s `trailingAccessory` override (active only while `ComposerState.Idle`) picks ONE
of two things, never a 3rd without extending this list:

1. **Bare mic glyph** (`BareMicButton`) — once `Streaming.done == true`: the post-turn voice-follow-up
  affordance, an OUTLINE glyph per reference-capture ground truth, NOT the filled circle (that's
  `Ready`'s own trailing slot only — confusing the two is a design regression).
2. **Filled mic circle** (`ComposerOverlayMicCircle`) — every other `Idle` case (fresh `Ready`;
  disabled during the brief pre-first-token `Sending` window, when the whole composer is disabled).

While a turn is actively generating (`Streaming && !done`), the composer resolves
`ComposerState.Streaming` and `AuraComposer`'s own default cluster renders the STOP tile (wired to
`onStopStreaming`). Both accessories wire to the SAME `AssistOverlayCallbacks.onStartListening` — the
caller (`AuraSession`) decides the `RECORD_AUDIO` grant question once, centrally; this screen never
branches on permission state.

## Pill pull-up → app handoff

Swiping the floating composer pill UP past `PILL_PULL_UP_THRESHOLD_DP` (48dp — local constant, flagged
for `AuraSpacing` tokenization) hands off into the full app from ANY overlay state.
`detectVerticalDragGestures` on the pill root drag-follows the finger via an `Animatable` offset;
releasing past threshold commits (settle haptic), below it springs back on
`AuraMotion.composerMorphSpring`.

Routing is a TESTABLE seam, `AssistTurnMachine.pullUpToApp(draft)`, never decided in the composable: a
sessionId from THIS invocation → the existing `EXTRA_HANDOFF_SESSION_ID` route-to-session path (the
same one `expand()` uses); no session → `EXTRA_HANDOFF_NEW_CHAT` (the app's default landing IS new
chat). A typed draft rides `EXTRA_HANDOFF_DRAFT` → chat destination `SavedStateHandle`
(`HANDOFF_DRAFT_KEY`, raw value — NOT a route arg, arbitrary text isn't URL-safe) →
`ChatViewModel.seedHandoffDraft` → one-shot apply in `ChatSurface` that deliberately does NOT tag the
pending send Voice (a masqueraded Voice tag would trigger speak-along on a typed draft's reply). The
drag offset carries an Idle-keyed `snapTo(0f)` reset per the draft-clear law.

## Companion chip slot + pill drag-handle hint

- **Drag-handle notch.** `AuraComposer`'s `showDragHandle: Boolean = false` (default off, so the docked
  in-app composer stays byte-identical) renders a small top-center notch when `showDragHandle &&
  !expanded` — hidden while the pill is expanded (chips/multiline draft), since a grown pill has no
  stable top-center resting spot. It's a visual AFFORDANCE HINT for the pull-up gesture, not a second
  gesture surface: the whole pill remains the drag target. It reuses
  `AuraSpacing.ResponseCard.dragHandleWidth`/`dragHandleHeight`/`dragHandleTopOffset` VERBATIM rather
  than minting a second handle size — one handle vocabulary in this file, not two.
- **`CompanionChipSlot`** (private) is the ONE named slot for contextual chips above the pill — today
  the inline `ErrorCard` (client-side `AssistUiState.Error`, retry-wired) and `ContinueLastSessionChip`
  (`AssistUiState.Ready` with a `lastSessionTitle`), nothing else (YAGNI). Both chips share one
  fade+rise entrance (`fadeIn(AuraMotion.composerMorphSpring) + slideInVertically { it / 2 }`). **Each
  chip's content lambda re-derives its own typed state** (`state as? AssistUiState.Error ?:
  return@AnimatedVisibility`) rather than trusting the outer `visible` condition —
  `AnimatedVisibility` keeps rendering its content during the EXIT transition off a snapshot, so a
  state flip mid-exit must never crash on a stale cast. Same defensive shape as
  `ComposerActionCircle`'s exhaustive-but-unreachable `Idle` branch.

**Repro note — two distinct failure surfaces, don't confuse them.** The client-side
`AssistUiState.Error` path (`ErrorCard`) is reachable on-device via `AssistOverlayPreviewActivity` by
pointing the base URL at an unreachable host (`-e seedBaseUrl http://127.0.0.1:1 -e mockBackend false`
on `.MainActivity`, then send any query) — this fails `AssistTurnMachine.beginTurn`'s
`createSession`/`sendQuery` directly, landing on `AssistUiState.Error` before any stream starts. The
mock backend's `"error"`-keyword scenario does NOT exercise this chip — it scripts a `completion`-level
`done_reason: "error"` that the reducer folds into an in-transcript error `ChatItem` rendered INSIDE
the `ResponseCard`, never transitioning `AssistUiState` to `Error` at all.

## Dismiss choreography: glow extinguishes WITH the pill + contained ✕

- **Shared exit-window constant.** `OVERLAY_GLOW_DISMISS_MS` (top-level private val, ≈169ms) is the ONE
  constant read by both `AuroraEdgeGlow(..., dismissFadeMs = …)` and the `tween()` duration for
  `FloatingComposerBar`'s pill-exit `Animatable`s. Before it, the glow used `AuroraEdgeGlow`'s quick
  default while the pill used a locally computed formula — two formulas that happened to read the same
  `AuraMotion` tokens and could drift apart the moment either changed. One shared number makes "glow
  and pill extinguish together" structural, not coincidental. It rides `AuroraEdgeGlow`'s existing
  hold-frame dismiss fade ([`ui/aurora/CLAUDE.md`](../aurora/CLAUDE.md) § run-end ease-off) — the same
  mechanism as `ChatScreen`'s longer ≥3s photosensitivity fade, just a shorter window — never a
  uniform snap (DESIGN.md).
- **Device-verified within this device's resolution.** redroid's screencap round-trip is ~1–1.2s, far
  slower than the 169ms window, so a frame-accurate burst capture isn't possible there. A genuine
  mid-dismiss frame WAS caught: the pill still on screen alongside a faint center-weighted blue
  bottom-edge tint (pixel-sampled), then a fully clean home screen in the next capture. No frame showed
  the glow alone after the pill vanished, or vice versa. Note `AssistOverlayPreviewActivity`'s
  `onDismiss = { machine.dismiss; finish }` calls `finish()` synchronously, so the whole exit
  choreography races Activity teardown on that host — a pre-existing host characteristic.
- **Contained dismiss ✕.** `OverlayChrome`'s `IconButton` was the ONLY floating element at the top with
  no container of its own AND the ONLY overlay icon with no explicit `tint` — it inherited
  `LocalContentColor` (`Color.Black` under `MaterialTheme`) over the dark scrim, rendering as fully
  invisible pixels in both hosts. Fix: the same `surfaceIconScrim`-disc treatment the chat top bar uses
  for icons over content (`Modifier.background(AuraColors.surfaceIconScrim, AuraShape.radiusPill)`),
  plus `tint = AuraColors.iconPrimary` on the `Icon` — never rely on the inherited default again.

## Outside-tap dismiss + the pointer-layering law

The overlay window is modal (`TYPE_VOICE_INTERACTION` — receives touch, no pass-through) and the scrim
(`OverlayScrim`) is DECORATIVE, consuming nothing. Dismiss is THREE affordances routed through the ONE
`callbacks.onDismiss` (→ `AuraSession.dismissSession()` → `machine.dismiss()` + `hide()`, so
draft-clear / TTS-stop / haptics AND the `state → Idle` exit choreography all ride the ONE proven
teardown path; reduced motion needs nothing extra, that exit already snaps):

1. the contained **✕** (`OverlayChrome`);
2. the scoped **swipe catcher**, which ALSO handles a plain tap — a second `detectTapGestures`
  alongside its drag detector (two `pointerInput`s on one node compose fine: the tap fires on a
  stationary press, the drag on movement past slop). Still scoped ABOVE the card's reserved region so
  its DRAG never competes with the card's `ChatTranscript` scroll;
3. a full-screen **gap layer** LOW in the z-stack (above the scrim, below all content) carrying
  `detectTapGestures { onDismiss }` plus the single `semantics { onClick("Dismiss assistant") }`.

**Pointer-layering law.** Compose hit-testing routes a pointer event ONLY to the topmost hit sibling
chain at that position — a lower overlapping sibling is NOT in the hit path, so consumption is
irrelevant and a single under-everything dismiss layer is insufficient. EVERY pointer-handling layer
over the scrim must itself route plain taps to `onDismiss` (this is why the swipe catcher gained a tap
detector — a tap in its region previously died in the drag detector, device-verified). The gap layer
only catches taps where no pointer-input sibling overlaps it: the card's side margins, and the
below-catcher / above-card band. Pure-draw layers (`AuroraEdgeGlow`) have no `pointerInput` and are
hit-test-transparent. Any new non-`Surface` overlay content over the scrim must ABSORB its own taps or
the outside-tap fires under it — `ResponseCard` (a bare `Column`) opts in via `detectTapGestures {}`,
DOWN-only, so the drag handle's swipe-up and the transcript's scroll are untouched.

The single `semantics onClick("Dismiss assistant")` lives on the gap layer and is TalkBack-reachable
independent of pointer z-order — an accessibility action dispatches through the SEMANTICS tree, not
pointer hit-testing. Label-only, no `contentDescription`, so it contributes a dismiss action without
shadowing the composer/card as a giant labelled region; the ✕ keeps its own independent label.

## IME: the platform pan is the ONLY keyboard mechanism — never add imePadding here

The WindowManager force-pans `TYPE_VOICE_INTERACTION` windows for the IME regardless of app-side
`softInputMode` (measured: `ADJUST_NOTHING` set both pre-attach and via live `attributes` reassignment
never changed dumpsys' `adjust=pan`, and the surface visibly panned). Pan alone parks the focused
composer directly above the keyboard — the correct, tight fit. Adding `imePadding()` on top made BOTH
mechanisms lift the content: the composer floated ~900px above the keyboard (measured via uiautomator +
`dumpsys input_method`). The bottom `Column` therefore has deliberately NO `imePadding()`.

**The in-app chat is the OPPOSITE**: an Activity window where `adjustResize` is inert under
edge-to-edge and `ChatSurface` NEEDS `imePadding()`. Don't cross-apply either rule.

## Screen-reader state announcer

`OverlayStateAnnouncer` (private) is the overlay's non-visual state channel — a zero-sized (`1.dp`)
`Box` with `semantics { liveRegion = LiveRegionMode.Polite; contentDescription = … }`, mounted as the
FIRST child of `OverlayChrome`'s `Box` so it's always present regardless of which other chrome shows.

**The string is keyed on state-KIND, never on a state's payload fields.** `Listening(partial, rmsDb)`
maps to one fixed string regardless of the live deltas that tick many times a second, and
`Streaming(items, done, speaking)` maps on `done` alone (constant through every delta-driven `items`
growth, flipping exactly once). Keying on payload would spam TalkBack on every waveform tick or
streamed token — the orb/glow/waveform are already the high-frequency visual channel; the announcer's
whole value is being a LOW-frequency non-visual one. `Idle` announces nothing (`null` short-circuits
before the `Box` is emitted) since the overlay is never on screen in that state.

**Dismissal is deliberately unannounced.** The window teardown races TalkBack's own announcement queue,
and focus returning to the host app IS the dismissal signal a screen-reader user gets for free from the
platform. A competing announcement would either lose the race silently or double-announce. Whether a
real screen reader speaks these correctly is a physical-Pixel human verification item — redroid and the
preview activity only confirm the states render and the announcer never crashes.

## Real-session-vs-preview geometry note

The real `VoiceInteractionSession` window's on-screen geometry differs from
`AssistOverlayPreviewActivity`'s — the composer has been measured sitting ~1000px lower in device
coordinates on the real session window. The leading explanation was that the two hosts had DIFFERENT
unconfigured platform soft-input defaults before `decorFitsSystemWindows(false)` was set explicitly
([`voice/CLAUDE.md`](../../voice/CLAUDE.md); the soft-input mode itself is left at the platform pan
default, never `setSoftInputMode`). Now that both request the same explicit regime, **re-verify this
gap on a real device before assuming it's closed.** When driving the real session via `adb input tap`,
always measure coordinates from an actual screenshot of the SESSION window, never reuse
preview-activity coordinates.
