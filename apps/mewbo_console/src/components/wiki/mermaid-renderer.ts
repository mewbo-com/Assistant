/**
 * Shared Mermaid renderer — single source for lazy-loading mermaid, theme
 * config, init coordination, and the rendered-SVG cache.
 *
 * Both `MermaidBlock` (inline body) and `DiagramZoom` (modal) consume this
 * module so they share the cache and never re-render the same source twice
 * per theme. Keeping it here also kills the `mermaid.initialize()` race
 * that happens when many blocks each call init in their own effects.
 */

type MermaidLib = typeof import("mermaid")["default"];

let mermaidLibPromise: Promise<MermaidLib> | null = null;

/** Lazy-load the mermaid bundle. Subsequent callers reuse the same promise. */
export async function loadMermaid(): Promise<MermaidLib> {
  if (!mermaidLibPromise) {
    mermaidLibPromise = import("mermaid").then((m) => m.default);
  }
  return mermaidLibPromise;
}

export type Theme = "light" | "dark";

export function currentTheme(): Theme {
  return typeof document !== "undefined" &&
    document.documentElement.classList.contains("light")
    ? "light"
    : "dark";
}

/**
 * Mermaid needs an actual resolved font stack, not a live `var(--font-sans)`
 * reference: it feeds this string into Canvas 2D text measurement
 * (`ctx.font = …`) to lay out diagram boxes, and a CSS custom property
 * doesn't resolve there the way it does in a stylesheet — only in the SVG's
 * own inline styles, which is half the story. Reading the computed value
 * mirrors `resolveColor` below and keeps this from ever hardcoding (and
 * outliving) a specific font name again.
 */
function resolveFontFamily(): string {
  if (typeof window === "undefined") return "system-ui, sans-serif";
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue("--font-sans")
    .trim();
  return raw || "system-ui, sans-serif";
}

/**
 * Composite a token onto an opaque base and read it back as a flat `#rrggbb`.
 *
 * `cssVarColor` is right for the WebGL graph but wrong here, for two reasons
 * that both end in a diagram drawn from mermaid's stock palette instead of
 * ours — silently, with nothing in the console to point at why.
 *
 * First, several of our tokens embed alpha (`--border-strong` is
 * `55 9% 74% / 0.35`), so `cssVarColor` hands back `hsl(H S% L% / A)`.
 * Mermaid does not consume that string as CSS: it parses colours with its own
 * library to derive the rest of the palette, and a value that library cannot
 * read is discarded in favour of a built-in default.
 *
 * Second, even parsed, a translucent stroke is the wrong INPUT. Mermaid bakes
 * the value into the SVG as a flat stroke rather than compositing it over the
 * diagram's surface, so a hairline tuned to sit at 35% over a card renders as
 * a washed-out line.
 *
 * Painting onto a canvas over the surface colour settles both: the browser
 * does the parsing, and the alpha is resolved against the background it was
 * designed against, yielding the colour a reader would actually have seen.
 */
function resolveColor(name: string, base: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  if (!raw) return fallback;
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    const ctx = canvas.getContext("2d");
    if (!ctx) return fallback;
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, 1, 1);
    // The `--diagram-*` tokens are authored as literal hex (they are design
    // values a person picked against a contrast target, not palette maths),
    // while the console's older tokens are bare `H S% L%` triples that need
    // wrapping. Accept both rather than forcing one spelling on the family.
    ctx.fillStyle = raw.startsWith("#") ? raw : `hsl(${raw})`;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
    return `#${[r, g, b].map((c) => c.toString(16).padStart(2, "0")).join("")}`;
  } catch {
    return fallback;
  }
}

// Colours stay live token reads — `getComputedStyle` picks up whichever of
// `:root` / `.light` is active on `<html>` at call time, so the two themes
// share one mapping and never drift out of sync with `index.css`. Only
// mermaid's own built-in theme name still branches on `theme` below.
function buildThemeConfig(theme: Theme) {
  const fontFamily = resolveFontFamily();
  const dark = theme === "dark";
  // Alpha-bearing tokens are composited over the surface the diagram is
  // actually drawn on, so a hairline reads as the colour it was tuned to be.
  const surface = resolveColor("--diagram-surface", "#000000", dark ? "#121110" : "#f2f0ea");
  return {
    startOnLoad: false as const,
    securityLevel: "loose" as const,
    fontFamily,
    // `base` is the ONLY theme mermaid lets `themeVariables` modify. Under
    // `default`/`dark` it computes its own palette and DISCARDS the overrides
    // below, which is why these diagrams rendered in mermaid's stock lavender
    // however carefully the tokens were resolved — the tokens were reaching
    // mermaid all along and being thrown away. `darkMode` is what tells
    // `base` which direction to derive its remaining shades in.
    theme: "base" as const,
    themeVariables: {
      darkMode: dark,
      background: surface,
      primaryColor: resolveColor("--diagram-node-bg", surface, dark ? "#26241f" : "#ffffff"),
      primaryTextColor: resolveColor("--diagram-label", surface, dark ? "#f8f8f6" : "#0a0a0a"),
      primaryBorderColor: resolveColor("--diagram-node-border", surface, dark ? "#767269" : "#8c8880"),
      lineColor: resolveColor("--diagram-line", surface, dark ? "#8d8a80" : "#6f6c64"),
      // Left unstated, `base` derives these by rotating the primary hue, so
      // subgraphs and clusters arrive in colours that appear nowhere else in
      // the console. Point them at real surface tokens instead.
      secondaryColor: resolveColor("--diagram-cluster", surface, dark ? "#1c1b18" : "#e7e4db"),
      tertiaryColor: surface,
      secondaryBorderColor: resolveColor("--diagram-node-border", surface, dark ? "#767269" : "#8c8880"),
      tertiaryBorderColor: resolveColor("--diagram-node-border", surface, dark ? "#767269" : "#8c8880"),
      secondaryTextColor: resolveColor("--diagram-label", surface, dark ? "#f8f8f6" : "#0a0a0a"),
      tertiaryTextColor: resolveColor("--diagram-label", surface, dark ? "#f8f8f6" : "#0a0a0a"),
      nodeTextColor: resolveColor("--diagram-label", surface, dark ? "#f8f8f6" : "#0a0a0a"),
      mainBkg: resolveColor("--diagram-node-bg", surface, dark ? "#26241f" : "#ffffff"),
      nodeBorder: resolveColor("--diagram-node-border", surface, dark ? "#767269" : "#8c8880"),
      clusterBkg: resolveColor("--diagram-cluster", surface, dark ? "#1c1b18" : "#e7e4db"),
      clusterBorder: resolveColor("--diagram-node-border", surface, dark ? "#767269" : "#8c8880"),
      titleColor: resolveColor("--diagram-label", surface, dark ? "#f8f8f6" : "#0a0a0a"),
      // Edge labels (a flowchart branch's "yes"/"no") are drawn as a filled
      // chip, and mermaid derives that fill from its OWN palette rather than
      // from `background` — so overriding the surface alone left the chip at
      // mermaid's light default, grey-on-grey against our dark diagram. Both
      // halves have to be stated: the chip fill and the text sitting on it.
      edgeLabelBackground: surface,
      textColor: resolveColor("--diagram-label", surface, dark ? "#f8f8f6" : "#0a0a0a"),
      fontFamily,
    },
    flowchart: {
      // HTML labels render into a foreignObject, which is the only mode where
      // wrapped label text can be styled from CSS at all — with them off a
      // label is <text>/<tspan> and its line spacing is untouchable.
      htmlLabels: true,
      // Wider labels wrap onto fewer lines, so a node grows sideways rather
      // than downwards. Rank direction (TD vs LR) lives in the diagram
      // source and nothing here can override it, so this is the one honest
      // lever a renderer has over how tall a diagram ends up.
      wrappingWidth: 260,
      nodeSpacing: 45,
      rankSpacing: 55,
      padding: 12,
      curve: "basis" as const,
      useMaxWidth: true,
    },
    sequence: { useMaxWidth: true, wrap: true },
  };
}

// One initialise() call per theme. A second mount on the same theme reuses
// the same promise and never re-races `mermaid.initialize()`.
let initialisedTheme: Theme | null = null;
async function ensureInitialised(theme: Theme): Promise<MermaidLib> {
  const mermaid = await loadMermaid();
  if (initialisedTheme !== theme) {
    mermaid.initialize(buildThemeConfig(theme));
    initialisedTheme = theme;
  }
  return mermaid;
}

// Render cache keyed by `theme|source`. Identical content + theme returns
// the same SVG immediately and `mermaid.render()` is not called again.
const svgCache = new Map<string, string>();

export function getCachedSvg(theme: Theme, source: string): string | undefined {
  return svgCache.get(`${theme}|${source}`);
}

/**
 * Render `source` to an SVG string. Cached per `(theme, source)` — repeat
 * calls bypass mermaid entirely. `domId` controls the temporary id mermaid
 * uses internally for its render container.
 */
export async function renderToSvg(
  theme: Theme,
  source: string,
  domId: string
): Promise<string> {
  const cacheKey = `${theme}|${source}`;
  const hit = svgCache.get(cacheKey);
  if (hit) return hit;
  const mermaid = await ensureInitialised(theme);
  const { svg } = await mermaid.render(domId, source);
  svgCache.set(cacheKey, svg);
  return svg;
}

/**
 * Source registry by diagram id, populated by the inline `MermaidBlock`
 * mounts. The zoom modal reads from here to render the same diagram at a
 * larger scale without re-walking the markdown tree.
 */
export const diagramRegistry: Record<string, string> = {};
