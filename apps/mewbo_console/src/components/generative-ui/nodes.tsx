/**
 * The generative-UI component vocabulary — one thin adapter per allowlisted
 * component name.
 *
 * ## The trust model, because it is the whole point of this file
 *
 * The vendored renderer spreads a node's `props` DIRECTLY onto the resolved
 * component (`@assistant-ui/core` → `GenerativeUI.js`, `createElement(Resolved,
 * {...props})`). Those props are model-authored JSON. So:
 *
 * - **No adapter may spread its props onto a DOM element.** Each one reads the
 *   named props it understands and drops the rest on the floor. That is what
 *   stops a smuggled `onClick`, `style`, `srcDoc` or `dangerouslySetInnerHTML`
 *   from ever reaching the DOM, and it is why every adapter destructures
 *   explicitly instead of taking a rest parameter.
 * - **Every prop is coerced, never trusted.** The backend validates the same
 *   vocabulary at emit time, but validation there is not a reason to skip it
 *   here: a replayed transcript, an older emitter, or a hand-built event all
 *   reach this file having bypassed that check. A wrong type degrades to a
 *   default; it never throws into the transcript.
 * - **`dangerouslySetInnerHTML` appears nowhere.** Model output renders as
 *   text, always.
 *
 * ## The design contract
 *
 * These are ADAPTERS, not new UI. Each one is a thin wrapper over a primitive
 * that already exists in `components/ui/` or an established console idiom
 * (`cardSurface`, the `Badge` chip, the `--code-*` surface family). Adding a
 * twelfth component is a product decision, not an implementation detail.
 *
 * Only `Card` and `Stack` take children; every other name is a leaf, and a
 * leaf carrying children silently ignores them.
 */
import { Fragment, type ReactNode } from "react";

import { Badge } from "../agents";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { cardSurface } from "../ui/card-surface";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../ui/table";
import { cn } from "../../lib/utils";

/** Props exactly as they arrive from the wire: JSON, model-authored, unvalidated. */
type RawProps = Record<string, unknown>;

/** Props for the two container components, whose children the renderer supplies. */
type ContainerProps = RawProps & { children?: ReactNode };

// Length caps mirror the emit-side limits. They are re-applied here for the
// same reason the type coercion is: an event that reached the console did not
// necessarily pass through the validating emitter.
const MAX_PROSE = 2_000;
const MAX_CODE = 20_000;
const MAX_LABEL = 200;
const MAX_CELL = 500;
const MAX_ITEMS = 20;
const MAX_COLUMNS = 8;
const MAX_ROWS = 50;

/** Coerce an untrusted prop to a bounded display string; anything else is "". */
function str(value: unknown, cap: number = MAX_PROSE): string {
  return typeof value === "string" ? value.slice(0, cap) : "";
}

/**
 * Pick `value` when it names one of `allowed`, else fall back to `allowed[0]`.
 * The FIRST entry is the default by convention, so each call site orders the
 * union with the wire contract's default in front.
 */
function oneOf<T extends string>(value: unknown, allowed: readonly [T, ...T[]]): T {
  return (allowed as readonly unknown[]).includes(value) ? (value as T) : allowed[0];
}

const SAFE_SCHEMES = new Set(["http:", "https:", "mailto:"]);

/**
 * Resolve an untrusted href, or `null` when it is not safe to put in an
 * `href` attribute.
 *
 * This duplicates the emit-side check on purpose — a URL is the one prop that
 * becomes executable if it is wrong, so it is re-checked at the boundary that
 * actually hands it to the browser. Returns the PARSED href rather than the
 * input: parsing normalizes away the tab/newline splitting that lets
 * `java\nscript:` read as a relative path to a naive prefix check while the
 * browser still runs it as a scheme.
 */
function safeHref(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const raw = value.trim();
  // Scheme-relative ("//host/path") inherits the page's scheme, so it would
  // survive a protocol check while still pointing off-origin.
  if (!raw || raw.startsWith("//")) return null;
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    // A relative path can only ever address the console itself, which is never
    // what a generated link means. Refuse rather than resolve it.
    return null;
  }
  return SAFE_SCHEMES.has(parsed.protocol) ? parsed.href : null;
}

// --- Leaves ---------------------------------------------------------------

export function TextNode({ value, tone }: RawProps) {
  const body = str(value);
  if (!body) return null;
  const muted = oneOf(tone, ["default", "muted"]) === "muted";
  return (
    <p
      className={cn(
        "text-sm leading-relaxed whitespace-pre-wrap break-words",
        muted ? "text-[hsl(var(--muted-foreground))]" : "text-[hsl(var(--foreground))]",
      )}
    >
      {body}
    </p>
  );
}

// A generated card is nested inside a transcript that already owns the page's
// top-level headings, so level 1 starts at <h2> — the document outline stays
// correct no matter how many cards a session emits.
const HEADING_LEVELS = {
  1: { Tag: "h2", cls: "text-lg font-semibold" },
  2: { Tag: "h3", cls: "text-base font-medium" },
  3: { Tag: "h4", cls: "text-sm font-medium" },
} as const;

export function HeadingNode({ value, level }: RawProps) {
  const body = str(value, MAX_LABEL);
  if (!body) return null;
  const { Tag, cls } = HEADING_LEVELS[level === 1 || level === 3 ? level : 2];
  return <Tag className={cn(cls, "text-[hsl(var(--foreground))]")}>{body}</Tag>;
}

const BADGE_STATUS_COLOR = {
  neutral: "muted",
  success: "emerald",
  warning: "amber",
  info: "blue",
  danger: "red",
} as const;

export function BadgeNode({ label, status }: RawProps) {
  const text = str(label, MAX_LABEL);
  if (!text) return null;
  const tone = oneOf(status, ["neutral", "success", "warning", "info", "danger"]);
  return <Badge color={BADGE_STATUS_COLOR[tone]}>{text}</Badge>;
}

export function KeyValueNode({ items }: RawProps) {
  const raw = Array.isArray(items) ? items.slice(0, MAX_ITEMS) : [];
  const pairs: { label: string; value: string }[] = [];
  for (const entry of raw) {
    const o = (entry ?? {}) as RawProps;
    const label = str(o.label, MAX_LABEL).trim();
    if (!label) continue;
    pairs.push({ label, value: str(o.value, MAX_CELL) });
  }
  if (pairs.length === 0) return null;
  return (
    <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
      {pairs.map((pair, i) => (
        <Fragment key={`${pair.label}-${i}`}>
          <dt className="text-[hsl(var(--muted-foreground))]">{pair.label}</dt>
          <dd className="text-[hsl(var(--foreground))] break-words">{pair.value}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

export function TableNode({ columns, rows }: RawProps) {
  const cols = (Array.isArray(columns) ? columns.slice(0, MAX_COLUMNS) : []).map((c) =>
    str(c, MAX_LABEL),
  );
  if (cols.length === 0) return null;
  // A ragged row is padded to the header width rather than dropped. Emit-side
  // validation rejects ragged rows outright, so one arriving here means the
  // event came from somewhere that skipped it — and a short row still reads.
  const body = (Array.isArray(rows) ? rows.slice(0, MAX_ROWS) : []).map((row) =>
    cols.map((_, i) => str(Array.isArray(row) ? row[i] : undefined, MAX_CELL)),
  );
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {cols.map((col, i) => (
            <TableHead key={`${col}-${i}`}>{col}</TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {body.map((cells, r) => (
          <TableRow key={r}>
            {cells.map((cell, c) => (
              <TableCell key={c}>{cell}</TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function CodeBlockNode({ code, language }: RawProps) {
  const body = str(code, MAX_CODE);
  if (!body) return null;
  const lang = str(language, 40).trim();
  return (
    <div className="overflow-hidden rounded-md border border-[hsl(var(--code-border))] bg-[hsl(var(--code-body))]">
      {lang && (
        <div className="border-b border-[hsl(var(--code-border))] px-3 py-1 text-2xs text-[hsl(var(--code-fg-subtle))]">
          {lang}
        </div>
      )}
      <pre className="overflow-x-auto p-3 font-mono text-xs leading-relaxed text-[hsl(var(--code-fg))]">
        <code>{body}</code>
      </pre>
    </div>
  );
}

const ALERT_TONE = {
  info: "border-[hsl(var(--info)/0.3)] bg-[hsl(var(--info)/0.1)] text-[hsl(var(--info-text))]",
  success:
    "border-[hsl(var(--success)/0.3)] bg-[hsl(var(--success)/0.1)] text-[hsl(var(--success))]",
  warning:
    "border-[hsl(var(--warning)/0.3)] bg-[hsl(var(--warning)/0.1)] text-[hsl(var(--warning-text))]",
  danger:
    "border-[hsl(var(--destructive)/0.3)] bg-[hsl(var(--destructive)/0.1)] text-[hsl(var(--destructive-text))]",
} as const;

export function AlertNode({ body, title, variant }: RawProps) {
  const message = str(body);
  if (!message) return null;
  const tone = oneOf(variant, ["info", "success", "warning", "danger"]);
  const heading = str(title, MAX_LABEL);
  return (
    <Alert className={ALERT_TONE[tone]}>
      {heading && <AlertTitle>{heading}</AlertTitle>}
      <AlertDescription className="whitespace-pre-wrap break-words">{message}</AlertDescription>
    </Alert>
  );
}

export function DividerNode() {
  return <hr className="border-0 border-t border-[hsl(var(--border))]" />;
}

export function LinkNode({ href, label }: RawProps) {
  const safe = safeHref(href);
  const text = str(label, MAX_LABEL).trim() || safe || "";
  if (!text) return null;
  if (!safe) {
    // Degrade to inert text instead of dropping the node: the label still
    // carries the meaning, and a silently-vanishing link reads as a render
    // bug rather than as a refusal.
    return (
      <span
        className="text-sm text-[hsl(var(--muted-foreground))]"
        title="Link removed — unsupported URL scheme"
      >
        {text}
      </span>
    );
  }
  return (
    <a
      href={safe}
      target="_blank"
      rel="noopener noreferrer nofollow"
      className="break-words text-sm text-[hsl(var(--primary-text))] underline underline-offset-2 hover:opacity-80"
    >
      {text}
    </a>
  );
}

// --- Containers -----------------------------------------------------------

export function CardNode({ title, children }: ContainerProps) {
  const heading = str(title, MAX_LABEL);
  return (
    <div className={cn(cardSurface({ radius: "left" }), "space-y-2 p-3")}>
      {heading && (
        <div className="text-sm font-medium text-[hsl(var(--foreground))]">{heading}</div>
      )}
      {children}
    </div>
  );
}

const STACK_GAP = { md: "gap-3", sm: "gap-1.5", lg: "gap-5" } as const;

export function StackNode({ direction, gap, children }: ContainerProps) {
  const dir = oneOf(direction, ["vertical", "horizontal"]);
  const size = oneOf(gap, ["md", "sm", "lg"]);
  return (
    <div
      className={cn(
        "flex",
        dir === "horizontal" ? "flex-row flex-wrap items-center" : "flex-col",
        STACK_GAP[size],
      )}
    >
      {children}
    </div>
  );
}

// --- Degradation ----------------------------------------------------------

/**
 * Rendered in place of any component name the allowlist does not carry.
 *
 * Passing this to the renderer is not optional: without a `Fallback` the
 * vendored renderer THROWS `GenerativeUIRenderError` on an unknown name, and a
 * throw inside the transcript blanks the whole conversation. A model typo must
 * cost one placeholder row, not the page.
 */
export function GenerativeUIFallbackNode({ component }: { component: string; props?: unknown }) {
  return (
    <div
      className={cn(
        cardSurface({ radius: "right" }),
        "px-2.5 py-1.5 text-xs text-[hsl(var(--muted-foreground))]",
      )}
    >
      Unsupported element <span className="font-mono">{str(component, 80)}</span>
    </div>
  );
}
