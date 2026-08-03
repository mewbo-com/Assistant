> ↑ [chat/CLAUDE.md](../CLAUDE.md) · [ui/CLAUDE.md](../../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../../CLAUDE.md)

# Aura Streamlit Widget Card — ui/chat/widget/

Scope: `ui/chat/widget/` — `WidgetCard`, the `ChatItem.Widget` renderer that hosts a model-built
Streamlit app. A bounded `WebView` (via `AndroidView`) serving the console-built offline stlite bundle
through `WebViewAssetLoader` from `appassets.androidplatform.net/assets/widget-host/`. The
advertise-half (the `stlite` capability header + the reducer's `foldWidget`) lives elsewhere, both
gated on `streamlitWidgetsEnabled` — [`di/CLAUDE.md`](../../../di/CLAUDE.md),
[`data/model/CLAUDE.md`](../../../data/model/CLAUDE.md).

## The ready-signal contract — NEVER `onPageFinished`

Post the `{app.py, data.json}` payload **on the host's `mewbo-widget-host-ready` window message, NOT
`onPageFinished`.** The ready signal exists so an embedder never depends on the host's internal load
order; a lazy-loaded listener silently drops an `onPageFinished`-timed post. A document-start bridge
script forwards the host's window-level ready message onto a `WebViewCompat.addWebMessageListener`
channel; only then does Android post, **EXACTLY ONCE per load** — a re-post reboots the Pyodide
kernel. `onPageFinished` is a fallback ONLY on a WebView too old for
`WEB_MESSAGE_LISTENER`/`DOCUMENT_START_SCRIPT`.

## Page-side traps — fix the PAGE, never Kotlin

- **WebView viewport units read 0.** `vh`/`dvh`/`%`-on-root all evaluate to `0` inside a `WebView`, so
  a widget that sizes off them collapses. The fix belongs in the console bundle.
- **`.mjs` MIME.** Served over a proxy whose `mime.types` lacks `mjs`, Pyodide is handed
  `application/octet-stream` and the kernel dies. A serving-side fix, not a client one.

## Docked vs overlay

`ChatItemRow` picks the renderer by the `allowRichWidgets` flag threaded from `ChatTranscript` (`true`
docked / `false` overlay). The overlay renders `WidgetSummaryCard`, a compact tap-to-open card whose
tap reuses the overlay's expand/handoff — booting Pyodide in a small floating overlay is wrong.
Extend that parameter surface for any further docked-vs-overlay difference; never fork the composable.

`WidgetCard` is `isChipFamily` for SPACING only (it is a full-width payload card, not a 36dp glance
row), and `widget_id`-keyed so re-reduction never rebuilds a visible one. `ChatIcons.Fullscreen` opens
it in a full-screen dialog.

**Accepted limitation:** a `WebView` in a `LazyColumn` item reboots Pyodide on far scroll-off — there
is no Compose portal equivalent to hoist it out of the list.

Verified end-to-end on redroid via the mock `widget` scenario (offline bundle booted, no network).
Streamlit's full paint is a Pixel-tier check — Pyodide boot on SwiftShader is too slow to demonstrate.
