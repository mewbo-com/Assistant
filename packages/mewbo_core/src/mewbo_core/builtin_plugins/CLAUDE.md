> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `builtin_plugins/` — the first-party suites that ship in the core wheel

Scope: `widget_builder/`, `generative_ui/` and `harness/`. All are ordinary plugins — same
SessionTool contract as a user-provided plugin, same `filter_specs()` tool-scope
rules, nothing special-cased by the loader. Discovery is a filesystem scan of each
suite's `.claude-plugin/plugin.json`, so adding a suite = drop a directory here
with a `plugin.json`.

`harness/` is the one suite that ships **no** SessionTool — it contributes only the
`mewbo-harness` skill, and it is the only built-in that declares **no**
`requires-capabilities`. Both properties are load-bearing and neither is cosmetic:
`activate_skill` is bound only when the visible catalogue is non-empty, so an
all-gated catalogue removes the tool itself on any surface advertising neither
`stlite` nor `apps`. An ungated suite keeps the mechanism reachable everywhere.
Gate it, and skills silently disappear from those surfaces again.

**Only ZERO-APP-IMPORT suites belong here.** A plugin whose tools wrap a heavier
substrate ships with that substrate, so it imports the engine down instead of up
into an app; `mewbo_graph.plugins.{wiki,scg}` is the worked example, and its
subsystem docs live there.

**`submit_widget` is TERMINAL-FREE (like `update_todos`) — keep it that way.** The
widget renders off the `widget_ready` event, not off run termination. Forcing
termination crashes every submit *after* the widget is already built (86% of calls
wasted on identical rebuilds in one measured trace) and robs the closing message.
**A tool whose effect is an event should not terminate at all.**

`widget_ready`'s payload is typed by `WidgetReadyPayload` (`submit_widget.py`,
`extra="forbid"`) **in the plugin module itself**, not as a new arm on
`mewbo_core.types.EventPayload` — that union stays generic and the event rides its
`dict[str, JsonValue]` catch-all on the wire. The snake_case wire shape is FROZEN:
a console TS type and an Android Kotlin type both mirror it.
