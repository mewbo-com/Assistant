/**
 * RefreshScopeSummary — the small, honest readout of what a wiki refresh
 * actually did (or is about to do): a scoped run's touched counts, or a full
 * rebuild's reason in one sentence. Shared by `RefreshThisWiki` (the moment
 * a refresh is requested — the POST response already carries the decision)
 * and `IndexingScreen` (the live run, once the scoped delta pass reports
 * back) so the two surfaces never spell this out twice.
 *
 * Renders nothing until there is something honest to say:
 *   - no `refreshDecision` at all → a first index, not a refresh. A third
 *     state, never treated as "full".
 *   - `refreshDecision.path === "full"` → the reason renders immediately;
 *     it is known the moment the decision is made, before any work runs.
 *   - `refreshDecision.path === "scoped"` but no `scopePreview` yet → the
 *     delta pass hasn't reported back. Nothing to show, not a placeholder.
 *   - `scopePreview` present → the touched-counts grid.
 *
 * Mirrors `mewbo_graph.wiki.types.RefreshDecision` / `ScopePreview` — see
 * those docstrings for why the shape is flat and why `mismatches` only ever
 * populates for `fingerprint_mismatch`.
 */
import { Info } from "lucide-react";

import { cn } from "@/lib/utils";

import type {
  FingerprintField,
  FingerprintMismatch,
  RefreshDecision,
  RefreshFullReason,
  ScopePreview,
} from "./api/types";

interface RefreshScopeSummaryProps {
  refreshDecision?: RefreshDecision;
  scopePreview?: ScopePreview;
  className?: string;
}

export function RefreshScopeSummary({
  refreshDecision,
  scopePreview,
  className,
}: RefreshScopeSummaryProps) {
  if (!refreshDecision) return null;

  if (refreshDecision.path === "full") {
    return (
      <FullRebuildNote
        reason={refreshDecision.reason}
        mismatches={refreshDecision.mismatches}
        className={className}
      />
    );
  }

  // The scoped note renders as soon as the PATH is known, without waiting for
  // the delta pass to report counts. Which path a run took is decided up front
  // and is the single most useful thing to say about it — a reader watching a
  // progress bar identical to a full index's otherwise has no way to learn that
  // this run will not touch a page. The counts join underneath when they land.
  return (
    <div className={className}>
      <ScopedPathNote />
      {scopePreview && <ScopedTouchedGrid preview={scopePreview} className="mt-2.5" />}
    </div>
  );
}

// ── Full rebuild — one human sentence, total over the reason union ────────

/**
 * `Record<RefreshFullReason, string>` rather than a switch: `tsc` fails the
 * build if a reason is ever added to the backend literal and this mapping
 * isn't updated in lockstep — the same exhaustiveness idiom `progress.ts`
 * uses for `PHASE_LABEL`. No separate runtime test needed for totality; the
 * compiler already is one.
 */
const FULL_REASON_COPY: Record<RefreshFullReason, string> = {
  requested: "A full rebuild was requested for this refresh.",
  graph_only:
    "This project is graph only, so a refresh always rebuilds it from scratch.",
  no_prior_index: "No prior index exists yet, so this is a first full build.",
  fingerprint_unknown:
    "The prior index didn't record what built it, so this refresh rebuilds fully rather than assuming it can be reused.",
  fingerprint_mismatch:
    "The tools that build this index have changed since the last one, so this refresh rebuilds fully.",
};

const FINGERPRINT_FIELD_LABEL: Record<FingerprintField, string> = {
  embedding_model: "embedding model",
  graph_schema_version: "graph schema version",
  grammar_pack_version: "grammar pack version",
  resolver_available: "symbol resolver availability",
};

function FullRebuildNote({
  reason,
  mismatches,
  className,
}: {
  reason: RefreshFullReason;
  mismatches: FingerprintMismatch[];
  className?: string;
}) {
  const changed =
    reason === "fingerprint_mismatch" && mismatches.length > 0
      ? mismatches.map((m) => FINGERPRINT_FIELD_LABEL[m.field]).join(", ")
      : null;
  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-md bg-[hsl(var(--muted))]/40 px-3 py-2",
        className,
      )}
    >
      <Info className="h-3.5 w-3.5 mt-0.5 shrink-0 text-[hsl(var(--muted-foreground))]" aria-hidden />
      <p className="text-xs leading-snug text-[hsl(var(--muted-foreground))]">
        <span className="font-medium text-[hsl(var(--foreground))]">Full rebuild.</span>{" "}
        {FULL_REASON_COPY[reason]}
        {changed && ` Changed: ${changed}.`}
      </p>
    </div>
  );
}

/**
 * The scoped path stated in plain language, as the full arm already was.
 *
 * The consequence matters more than the counts and is easy to miss: a scoped
 * run is sessionless and has no pages phase, so a page it scores stale stays
 * exactly as it was. Saying only "10 pages to regenerate" alongside "0 LLM
 * calls" reads as work that was free, when it is work that was never attempted.
 */
function ScopedPathNote({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-md bg-[hsl(var(--muted))]/40 px-3 py-2",
        className,
      )}
    >
      <Info className="h-3.5 w-3.5 mt-0.5 shrink-0 text-[hsl(var(--muted-foreground))]" aria-hidden />
      <p className="text-xs leading-snug text-[hsl(var(--muted-foreground))]">
        <span className="font-medium text-[hsl(var(--foreground))]">Scoped refresh.</span>{" "}
        Only the files that changed are re-parsed, so the code graph and search
        stay current. Documentation pages are not regenerated on this path — a
        full rebuild is what rewrites prose.
      </p>
    </div>
  );
}

// ── Scoped refresh — grouped counts, headline first, footnote last ────────

function ScopedTouchedGrid({
  preview,
  className,
}: {
  preview: ScopePreview;
  className?: string;
}) {
  const filesChanged = preview.filesAdded + preview.filesModified + preview.filesDeleted;
  const pagesToUpdate = preview.pagesEdit + preview.pagesRegenerate + preview.newPages;
  const memoryReconciled = preview.memoryInvalidated + preview.memoryRevalidated;

  return (
    <div className={className}>
      <div className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
        Scope of this refresh
      </div>
      <div className="mt-1.5 grid grid-cols-2 sm:grid-cols-4 gap-2">
        <ScopeTile
          value={filesChanged}
          label="files changed"
          sub={`${preview.filesAdded} added · ${preview.filesModified} modified · ${preview.filesDeleted} deleted`}
        />
        <ScopeTile value={preview.affectedEntities} label="entities affected" />
        {/* "flagged stale", NOT "to update" — nothing on this path updates
            them, and the past-tense "edited / regenerated" sub-label claimed
            outright that it had. */}
        <ScopeTile
          value={pagesToUpdate}
          label="pages flagged stale"
          sub={`${preview.newPages} uncovered · ${preview.pagesEdit} need edits · ${preview.pagesRegenerate} need rewriting`}
        />
        <ScopeTile
          value={memoryReconciled}
          label="memory reconciled"
          sub={`${preview.memoryKept} kept unchanged`}
        />
      </div>
      {/* Honest footnote, not a headline number. `llmCalls` counts the memory
          reconciliation this pass actually performed — it has never included
          documentation, because none is generated here. Left unqualified it
          read as the price of the pages counted above, i.e. as "regenerating
          them was free" rather than "they were never regenerated". */}
      <p className="mt-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
        {preview.earlyCutoffFiles > 0 &&
          `${preview.earlyCutoffFiles} file${preview.earlyCutoffFiles === 1 ? "" : "s"} needed no further work. `}
        {preview.llmCalls === 0
          ? "No model calls were needed to reconcile memory, and none were spent on documentation."
          : `${preview.llmCalls} model call${preview.llmCalls === 1 ? "" : "s"} to reconcile memory; none spent on documentation.`}
      </p>
    </div>
  );
}

function ScopeTile({ value, label, sub }: { value: number; label: string; sub?: string }) {
  return (
    <div className="rounded-md bg-[hsl(var(--muted))]/30 px-2.5 py-2">
      <div className="text-sm font-medium tabular-nums text-[hsl(var(--foreground))]">{value}</div>
      <div className="text-2xs text-[hsl(var(--muted-foreground))]">{label}</div>
      {sub && <div className="mt-0.5 text-2xs text-[hsl(var(--muted-foreground))]">{sub}</div>}
    </div>
  );
}
