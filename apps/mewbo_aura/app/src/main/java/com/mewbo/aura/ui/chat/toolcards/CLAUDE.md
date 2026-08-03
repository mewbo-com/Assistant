> ↑ [chat/CLAUDE.md](../CLAUDE.md) · [ui/CLAUDE.md](../../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../../CLAUDE.md)

# Aura Promoted-Tool Action Cards — ui/chat/toolcards/

Scope: `ui/chat/toolcards/` — the filled action cards a hand-picked tool renders instead of folding
into the collapsed `ToolCallGroupCard`. `ToolActionCard` (base) · `ToolCardRegistry` (dispatch +
`GenericToolCard` fallback) · `AlarmToolCard`/`TimerToolCard`/`SmsToolCard`/`SwitchProjectToolCard`.
This is the sanctioned exception to "never a filled card" — read DESIGN.md for the law and why it
does NOT repeal the fold's rule.

## THE ALLOWLIST TRAP — a registry branch is dead code until the id is in `PromotedTools`

Promotion is TWO halves that evolve independently: `data/model/PromotedTools`
([`data/model/CLAUDE.md`](../../../data/model/CLAUDE.md)) decides WHICH `tool_result`s become their own
`ChatItem.ToolCard` item (transcript structure); `ToolCardRegistry` here decides what that item LOOKS
like. **Adding a `when` branch to `ToolCardRegistry` renders NOTHING until the tool id is also added to
`PromotedTools`** — the reducer never creates the `ChatItem.ToolCard` without it. Conversely, an id in
`PromotedTools` with no registry branch is SAFE: it lands on `GenericToolCard` (header + the backend's
one-line `summary`), never a crash or blank row.

## Laws

- **`ToolActionCard` is the ONE base** — an entity header (glyph + quiet `chipLabel`/`textSecondary`
  label: *which tool is this*) over a swappable content slot. Anatomy is Google's own (GMS `uiautomator`
  capture); every VALUE is an Aura token, nothing measured off their pixels. It wears
  `surfaceSelected` NOT `surfaceInput` (which fills the composer/sheets/chips — a card in it dissolves
  into the chrome) and `radiusBubble` NOT `radiusCard` (radiusCard's 20dp was measured for the SMALL
  overlay card; this is a full-width bubble-scale surface). Picking a token by NAME rather than the
  surface it was measured against is how both first-round bugs happened — read the KDoc, not the name.
- **Adding the next card = three touches:** one id in `PromotedTools`, one `when` branch in
  `ToolCardRegistry`, one composable over `ToolActionCard`. If a change needs more, the base is wrong —
  fix the base, don't fork it.
- **A card may read the tool's RESULT — but only where the result is a NAMED SHAPE, never prose.**
  `SwitchProjectToolCard` is the first card to read `ToolCall.detail`, and it can because
  `switch_project` returns JSON with fixed keys (`project`/`name`/`cwd`/`repo`/`branch`/
  `previous_project`/`previous_cwd`/…), mirrored field-for-field by a `@Serializable`
  `SwitchProjectResult`. Every key is always present — `null` when unknown rather than omitted — so
  the card branches on VALUES, never on key existence. **Rule: if a card wants a fact from a result,
  the result grows a key — a renderer never parses a sentence.** Matching line prefixes off a prose
  result breaks silently the day someone improves the tool's copy, which is why the tool's prose moved
  into a `summary` FIELD and everything else got named.
  - The tool's `summary` is written for the MODEL and is deliberately NOT echoed: the card renders
  structure, the turn's own narration underneath is the user's prose.
  - `cwd`, not `path` — `path` is `list_projects`' word for a catalogue ENTRY (a registered
  repository with no checkout legitimately has none); `cwd` is where the agent now IS.
  - Read the KEYS from the producer, not from a description of it: `description` and `name` arrive
  as `""` rather than `null` when the catalogue has none, so blank-is-absent, not null-is-absent.
- **A refused call must never render as a successful one — and the card owns a guard of its own.**
  Promotion already requires `success`, which the loop's error-envelope reclassification drives. The
  card ALSO refuses any payload carrying an `error()` key, because the first guard depends on a
  reclassifier recognising one exact spelling (today a Python repr, `str(dict)`, NOT `json.dumps` —
  `ast.literal_eval` silently declines JSON). Two independent guards, because the failure mode is a
  card confidently naming a project the session never moved into.
- **An unreadable result degrades the WHOLE card, not just the rows it fed.** Now that the result is
  the contract, a truncated/mistyped/absent one means the card does not know what happened, so
  `parse` returns null ⇒ `GenericToolCard` rather than half-rendering from the args. Args are a
  fallback for one field (the key), never a second source of truth.
- **Args are model output — a black box → parse TOTALLY.** `AlarmArgs.parse` returns null on EVERY
  malformed shape (including a nested object where a number belongs — the `jsonPrimitive` accessor
  THROWS there, so use `as? JsonPrimitive`); the card degrades to `GenericToolCard` rather than
  rendering half-empty.
- **Format the payload BY HAND, not `DateTimeFormatter`/`String.format`** — a device locale would flip
  an alarm to a 24-hour clock or Eastern-Arabic digits, and the time the model set is a wall-clock fact
  that must read identically everywhere.
- **Promotion requires `success`** — a failed allowlisted call stays in the fold (a confident singled-out
  card for a tool that failed is the UI lying), so `GenericToolCard` never needs failure copy.
- **The card does NOT suppress the turn's prose** — the wire sends exactly one `assistant` event per
  turn and the reducer never drops it, so a promoted turn renders `[card] → [prose]` (card above
  narration). The reference app replaces its prose with the card; we diverge BY DECISION (a turn's
  narration can carry more than the tool's result). Do not write card copy that assumes the card is the
  whole answer.
- **A `ToolCard` is `isChipFamily` for SPACING only** (not a 36dp glance row — it's a payload surface
  with its own `AuraSpacing.ToolCard` geometry, applying the 24dp gutter inset itself). Omitting the
  predicate reproduces the "chips sit 0dp under the bubble" bug — a promoted tool is very often the first
  thing there. `ToolCardRegistry.humanize` is sentence case, not Title Case (a calm quiet label).
