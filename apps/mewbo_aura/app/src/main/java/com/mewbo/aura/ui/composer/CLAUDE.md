> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Composer — ui/composer/

Scope: `ui/composer/` — `AuraComposer` (one component, five `ComposerState`s, consumed by both the
docked in-app chat and the assist overlay), `ComposerState`/`ComposerStyle`, `RmsWaveform`,
`ComposerOptionsSheet`.

## Two pill widths/heights/type scales/circle sizes, never share one token

`ComposerStyle.Docked` (in-app chat) and `ComposerStyle.FloatingOverlay` (assist overlay) render
the SAME five `ComposerState`s and anatomy, but every geometry axis that distinguishes them is a
DIFFERENT, independently measured/directed token — never one token consumed with a
style-conditional multiplier:

- `AuraSpacing.Composer.horizontalMargin` = **16dp** — docked, in-app.
- `AuraSpacing.Composer.overlayHorizontalMargin` = **38dp** — floating overlay pill, pixel-sampled
  from real-device captures (a 16dp estimate for the overlay was wrong).
- `AuraSpacing.Composer.height` (docked) vs. `AuraSpacing.Composer.overlayHeight` (overlay)
  — values in DESIGN.md; the overlay pill reads as a full conversational surface.
- `AuraSpacing.Composer.actionCircleSize` (docked) vs. `AuraSpacing.Composer.overlayActionCircleSize`
  (overlay) — values in DESIGN.md; the primary voice action dominates the overlay
  pill's trailing side; threaded via `ComposerTrailingCluster`'s `size` param to
  `ComposerOverlayMicCircle`/`ComposerActionCircle`/`ComposerVoiceModeTile`/`ComposerStopTile`, all
  four of which default to the docked size so every OTHER caller stays byte-identical.
- `AuraType.bodyMessage` (docked) vs. `AuraType.composerOverlay` (overlay) — size/
  line-height in DESIGN.md; the field/invitation type scale, so the invitation reads comfortably
  title-scale in the overlay pill.

These are deliberately SEPARATE `AuraSpacing`/`AuraType` fields per axis, not shared tokens
consumed with a style-conditional multiplier — a future in-app change on any of these must never
accidentally drag the overlay pill's geometry along with it, or vice versa. `ComposerStyle.pillColor`
(Docked → `surfaceInput`, FloatingOverlay → `surfaceOverlayPill`) was the original precedent for
"same component, two independently-tokened style axes"; `pillHeight`/`actionCircleSize`/
`fieldTextStyle` (private `ComposerStyle` extensions next to `pillColor` in `AuraComposer.kt`)
follow the identical shape.

## Docked scope-row alignment anchor

The docked composer's pre-session scope row (rendered by `ChatSurface`, [`ui/chat/CLAUDE.md`](../chat/CLAUDE.md)
owns its content + faceted counts) aligns its leading glyph to the pill's straight-edge start via the
new token `AuraSpacing.Composer.scopeRowStartInset = horizontalMargin + height/2` (16 + 32 = 48dp).
`radiusPill` is `CircleShape` — a 50% stadium — so the corner-radius end sits exactly `height/2` in
from the pill edge: that's the "where the curve becomes the straight line" anchor, reusable for ANY
composer-content alignment (values → DESIGN.md). Do not eyeball a scope-row indent against
`horizontalMargin` alone — it would sit under the stadium curve, not the straight edge.

## Repeating section headers: top-heavy asymmetric rhythm

`ComposerOptionsSheet`'s `ScopeSectionHeader` (the provenance dividers layered over the per-server tool
groups) pads **top-heavy and asymmetric** — full `AuraSpacing.DrawerRow.sectionHeaderTopPad` above, NO
explicit gap below — so a boundary clings to the content that FOLLOWS it (~2:1, the same "headings cling
to what follows" shape as `AuraSpacing.Markdown.headingTopGap`, matching `RecentsHeader` /
`SettingsScreen`). The **symmetric `sectionHeaderTopPad / 2` top-AND-bottom form is ONLY for a single,
non-repeating header** (`ActionSheet.SheetHeader`, `ModelPickerSheet`): in a REPEATING list it gave
every provenance boundary the same visual weight as an ordinary intra-section row gap, so the boundaries
read as unclear. `ServerGroupHeader`/`ToolRow` are bare, zero-gap fixed-height rows, so this
header's own top/bottom asymmetry is what carries the entire boundary signal — don't restore symmetry here.

## `DictationCenterContent`'s `partialText` null-vs-blank contract

`ComposerState.Dictation.partialText: String?` is the center content's ONLY signal for "show the RMS
waveform bars" (`null`) vs. "show the finalizing transcript text" (non-null). **Every state-producing
call site MUST pass `listening?.partial?.takeIf { it.isNotBlank }`, never the raw
`listening?.partial`** — `AssistUiState.Listening.partial` (and `DictationState.Listening.partial`)
defaults to `""` the INSTANT dictation starts, not `null`. The raw empty string makes `partialText !=
null` permanently true from the first frame, so the RMS bars branch never renders at all: the C3
waveform is dead the moment dictation starts, with no visible error. This regressed twice, at the
assist overlay's `FloatingComposerBar` and the in-app composer's own binding — grep both before
touching either, and apply the same `takeIf` to any THIRD caller.

## C3 fluid voice bars (`RmsWaveform`,)

The 28-bar RMS meter is a per-bar simulation, **not one envelope scaling a fixed taper shape** — that
form reads as "a triangle moving." `RmsWaveformMath.barTarget(index, rms, timeSeconds)` computes a
FLOWING pseudo-spectrum target per bar (only RMS exists, no FFT: a `valueNoise1d` field sampled at
`index * PER_BAR_PHASE + timeSeconds * FLOW_HZ`, tapered by the `shape` center-weight), and `stepBars`
drives each bar toward its own target with a critically-damped spring (`SPRING_HZ`) plus neighbor
coupling (`COUPLING_PER_SECOND`, a discrete-Laplacian diffusion term) so energy visibly travels across
the row frame to frame. `heightFraction`/`envelopeSpec` and `AuraMotion.rmsDecayMs` are DELETED — the
spring replaces the tween-based envelope entirely; grep before reviving either token.

Rendering is ONE `Canvas`, stepped by a `withFrameNanos` loop that mutates plain
`heights`/`velocities` `FloatArray`s and bumps a `frameTick` read ONLY inside the `Canvas` draw lambda
— zero per-frame recomposition (house draw-phase law, [`ui/CLAUDE.md`](../CLAUDE.md)). All constants
(`FLOW_HZ`, `PER_BAR_PHASE`, `SPRING_HZ`, `COUPLING_PER_SECOND`, `NOISE_FLOOR`, `MAX_STEP_SECONDS`,
`SUB_STEP_SECONDS`) are behavioral tuning constants in `RmsWaveformMath`, per the
`EdgeGlowUniformMath` provenance convention — never design tokens (geometry stays in
`AuraSpacing.Composer`).

**Numerical-stability trap — found via on-device capture, not the unit suite.** The critically-damped
spring is numerically stiff at `SPRING_HZ`: a single semi-implicit-Euler step at the
`MAX_STEP_SECONDS` clamp overshoots and clamps every bar to the ceiling (from rest toward target
`0.5`, one step at `dt = 1/20` reaches `h = 1.59` before the `coerceIn`), so any slow frame — a
software renderer, a dropped frame, a GC pause — reproduces the "triangle" symptom as a uniformly
maxed-out row. **The `dtSeconds` clamp alone only bounds how much simulated time one call advances; it
does not make the integration stable.** `stepBars` internally sub-steps at `SUB_STEP_SECONDS` (1/60s)
regardless of the caller's already-clamped `dtSeconds`, looping the per-bar spring+coupling update
until the clamped duration is consumed (same input post-fix: `h = 0.36`, no overshoot). Verified
on-device: the peak bar index genuinely shifts between captures ~150ms apart.

## C-state morphs (Morphs)

`ComposerState` resolution priority, encoded in `ComposerState.resolve` (pure, unit-testable without
Compose): dictation wins over everything (an explicit, exclusive user action) → an in-flight run
wins over plain typing (`Streaming` must render even with an empty draft, to show the stop
affordance) → draft text alone decides `Typing` vs. `Idle`. Every state transition animates via the
SAME `ComposerMorphTransform` (`AuraMotion.composerMorphSpring`, 200ms crossfade) — "one cluster,
not per-state composables that pop." `trailingAccessory`, when supplied, replaces the ENTIRE trailing
cluster (not just the action circle) — the assist overlay needs the whole slot for its own
mic-circle/docked-orb/bare-glyph three-way (`ui/overlay/CLAUDE.md`); `AuraComposer` itself stays
agnostic to what a caller puts there.

## Mic circle tokens (`ComposerOverlayMicCircle`, overlay style only)

`AuraColors.accentOverlayMic` = **`#709FF8`** (measured; a `#A9C7FF` estimate was notably
lighter/whiter than reality). Glyph tint `onAccentOverlayMic` = `surfaceCanvas` (pure black) — both
fully opaque, confirmed strong contrast against `surfaceOverlayPill`'s `#020B2B` fill. **This pairing
was independently ruled out as a source of the "mic occasionally invisible" bug** — the actual root
cause was stale composition state ([`ui/overlay/CLAUDE.md`](../overlay/CLAUDE.md)'s draft-clear-on-Idle
law), not a rendering/contrast defect.

The Docked-style mic entry point is a bare glyph (`ComposerBareIconButton`), not a filled circle — the
filled-circle treatment is overlay-only, matching the "overlay pill gets its own C5 anatomy" ruling.
