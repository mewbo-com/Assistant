> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Shared UI Vocabulary — ui/common/

Scope: `ui/common/` — the small composables shared across surfaces. Each has ONE key invariant/trap.

- **`ActionSheet`** — the shared long-press-sheet CHROME (`SheetHeader`/`SheetActionRow(enabled)`/
  `SheetErrorCaption`), consumed by both `MessageActionsSheet` and `SessionActionsSheet` (which is where
  they originated). The CONTAINER is `AuraBottomSheet` (below), not `ActionSheet` — the two sheets
  differ in structure (pane machine vs flat rows), so chrome stays shared while structure stays each
  sheet's own. **Do not treat the container as "only a lambda pass-through" and leave it unshared:**
  bounding, scrolling, and insets are load-bearing shared behavior, and their absence independently
  produced the same overflow defect in every sheet (DESIGN.md). `SheetErrorCaption` keeps the
  sheet OPEN on failure.
- **`ProjectRowKind`** — what a project row IS (`Saved`/`Temporary`/`Auto`), carrying its glyph and
  tint as MEMBERS. Three readers: the two project pickers (`ui/settings/ProjectPickerSheet`, the
  app-wide default, and `ui/composer/ComposerOptionsSheet`'s project pane, the per-chat one) render
  structurally different lists over the same three kinds, and `ChatTopBar`'s project line is the third
  — the glyph-and-tint `when` lives here so it is not three copies in three packages edited for
  different reasons. `Auto` wears the PROJECT tint, not `Temporary`'s muted one: an auto session ends up in a real
  project, it just is not in one yet. `AutoRowLabel`/`AutoRowCaption` are here for the same reason —
  two spellings of one mode read as two modes.
- **`AuraBottomSheet`** — the ONE shared sheet CONTAINER: viewport-capped height, internal scrolling,
  system-bar insets, the top-corners-only shape, and the container color, owned in one place instead of
  re-derived per sheet. Two entry points share the one private frame: `AuraBottomSheet` (a scrollable
  `Column`, for bounded content — short content still wrap-content sizes, a 2-row action sheet stays
  small) and `AuraListBottomSheet` (a `LazyColumn`, for unbounded, data-driven content like the model
  catalog or the project list). `ModalBottomSheet` is never called directly anywhere else — enforced by
  `SheetContainerContractTest`.
- **`MarkdownMessage`** — the ONE assistant-text render path (streaming AND finalized; `WordFadeText`
  retired). Two mandatory anti-flicker mechanisms (DESIGN.md): `rememberMarkdownState(retainState =
  true, immediate = true)` (the default resets to a blank `State.Loading` per content change — that
  blank-flash WAS the flicker); and `rememberStreamedText(text, isStreaming)` throttling the reparse to
  `AuraMotion.markdownStreamReparseMs` (~20Hz) while streaming and passing finalized text through
  byte-identically. The caller MUST run `MarkdownBuffer.sanitize` first. Custom layout emits type-aware
  Spacers only BETWEEN blocks (never before the first — `topGapAfterBubble` owns the space above); h4–h6
  reuse `bodyMessage` (heading by placement only).
- **`MarkdownBuffer.sanitize`** — a pure defensive closer (NOT a parser) at the streaming→finalized swap:
  closes half-open ``` fences, `**` bold, `*` italics so a truncated buffer can't swallow the rest of the
  transcript into one code block. A fence only closes when it starts its own line; italic counting
  excludes CommonMark list-marker `*` bullets.
- **`ErrorCard`** — a quiet inline error (warning glyph + reason + optional Retry text-button), NO red
  fills. `onRetry == null` renders without the button — the composable does not know WHY (the caller's
  `ChatUiState.sessionEnded` decides; a terminated session 410s every retry). There is no dedicated
  `error()` `SessionEvent` type, so callers always arrive with an already-resolved human reason.
- **`NoticeHost`** — the one-line-at-a-time notice; `show()` REPLACES (no queue); `lastText` is held
  separately so the exit fade plays against the last message instead of snapping blank. **Needs explicit
  `.imePadding()`** or it renders behind the keyboard.
- **`TypingIndicator`** — 3 phase-offset pulsing dots (reads as a wave). The `Row` sets
  `verticalAlignment = CenterVertically` EXPLICITLY (DESIGN.md: shared row composables must, or the
  trailing label misaligns). Dots inherit `LocalContentColor` — color is the caller's, not themed here.
- **`AttachmentTile`** — metadata-only (`filename`, `mimeType`); NEVER a real thumbnail (no Coil/AsyncImage
  anywhere in the app). `AttachmentGlyphs` is the ONE home of the `image/`-mime check, shared with the
  composer's pre-send chip.
- **`ChatOverflowMenu`** — the one overflow-menu style (`surfaceSelected`, 20dp radius); v1 ships only
  working items, no disabled stubs.
