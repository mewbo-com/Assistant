/**
 * IndexingProgress — atomic class describing one job's progress.
 *
 * The landing page card polls the snapshot endpoint (``IndexingJob``)
 * and the indexing page consumes the SSE stream (folded
 * ``IndexingStreamState``). Computing progress independently in each surface
 * invites exactly the drift a single-owner class prevents — e.g. reading
 * ``(scanned/total)*96`` alone (ignoring ``phase``) pegs at 96 for the entire
 * graph/plan/pages window, where a phase-weighted computation with
 * per-phase sub-progress does not.
 *
 * This class is the single source of truth. ``fromJob`` and ``fromStream``
 * both feed the same private ``_compute`` core, so the two views can
 * never drift apart again. ETA is a MEASURED-RATE-OR-NOTHING estimate of
 * time left in the CURRENT phase only — see "ETA" below for why a fixed
 * per-phase budget and a trailing-phase guess were both removed.
 *
 * Convention: an atomic class — frozen state in the instance, behaviour
 * on the prototype, static helpers off the class.
 */
import type { IndexingJob, IndexingPhase } from "./api/types";

/** Indexing-screen reducer state (a subset — only the fields we read here). */
export interface IndexingStreamSnapshot {
  job: IndexingJob | null;
  phase: IndexingPhase | null;
  totalPages: number | null;
  pagesSubmitted: number;
}

// Phase weights — calibrated to real run shape.
// Each phase has a (start, end) percent range; sub-progress fills the
// range linearly. The bar never finishes a phase at 100% until the NEXT
// phase event arrives (sub is clamped to 0.98 inside ``_compute``).
const PHASE_RANGE: Record<IndexingPhase, [number, number]> = {
  clone: [0, 5],
  scan: [5, 20],
  graph: [20, 32],
  enrich: [32, 40],
  plan: [40, 45],
  pages: [45, 95],
  finalize: [95, 100],
};

const PHASE_LABEL: Record<IndexingPhase, string> = {
  clone: "Cloning repository",
  scan: "Scanning files",
  graph: "Building knowledge graph",
  enrich: "Extracting entities",
  plan: "Planning wiki structure",
  pages: "Writing wiki pages",
  finalize: "Finalizing",
};

export const PHASE_ORDER: readonly IndexingPhase[] = [
  "clone",
  "scan",
  "graph",
  "enrich",
  "plan",
  "pages",
  "finalize",
];

/** Default unit label when the BE hasn't started sending one yet
 *  (`phaseProgressUnit` absent) — generic enough to read as honest. */
const DEFAULT_PROGRESS_UNIT = "units";

export interface ProgressView {
  /** Whole-number percent for the bar (0-100). */
  pct: number;
  /** Phase used for rendering. Falls back to ``"clone"`` on unknown. */
  phase: IndexingPhase;
  /** Heading line — "Cloning repository", "Writing wiki pages", … */
  label: string;
  /** Sub-line — "12 of 30 files", "Page 4 of 25", "9 of 12 nodes", or empty. */
  statusLine: string;
  /**
   * Seconds remaining estimate, scoped to the CURRENT phase only — never a
   * whole-job guess (see "ETA" below). ``null`` when there's no measured
   * rate to extrapolate from: the run hasn't committed a unit in this phase
   * yet, the phase carries no progress signal at all, or the run is already
   * complete.
   */
  etaSeconds: number | null;
}

interface ComputeInput {
  phase: IndexingPhase | null;
  status: IndexingJob["status"] | undefined;
  scannedCount: number;
  totalCount: number;
  pagesSubmitted: number;
  totalPages: number | null;
  phaseStartedAt: string | null | undefined;
  /**
   * Generic per-phase progress — the ONE mechanism for every phase beyond
   * scan/pages (today: graph/enrich; works for a future phase with zero
   * changes here). The BE resets all three of these to ``null`` on every
   * ``emit_phase`` transition, which is the invariant this class leans on:
   * a non-null ``phaseProgressCurrent`` always belongs to the phase named by
   * ``phase``, never a stale value from a phase that already ended (the
   * class of bug ``scannedCount``/``currentFile`` had — frozen leftovers
   * from a phase that finished, read as if they described the current one).
   */
  phaseProgressCurrent: number | null;
  /** Paired with {@link phaseProgressCurrent}. ``null`` means "a running
   *  count with no known total yet" — a real status line, but no honest
   *  fraction to paint (pct stays at the phase floor, no ETA). */
  phaseProgressTotal: number | null;
  /** "files" | "nodes" | "entities" | … — defaults to a generic label when
   *  the BE hasn't started sending one. */
  phaseProgressUnit: string | null;
}

export class IndexingProgress {
  /** Compute from a snapshot ``IndexingJob`` (landing card path). */
  static fromJob(job: IndexingJob | null | undefined): ProgressView {
    if (!job) {
      return { pct: 0, phase: "clone", label: PHASE_LABEL.clone, statusLine: "", etaSeconds: null };
    }
    return IndexingProgress._compute({
      phase: (job.phase ?? null) as IndexingPhase | null,
      status: job.status,
      scannedCount: job.scannedCount ?? 0,
      totalCount: job.totalCount ?? 0,
      pagesSubmitted: job.pagesSubmitted ?? 0,
      totalPages: job.totalPages ?? null,
      phaseStartedAt: job.phaseStartedAt ?? null,
      phaseProgressCurrent: job.phaseProgressCurrent ?? null,
      phaseProgressTotal: job.phaseProgressTotal ?? null,
      phaseProgressUnit: job.phaseProgressUnit ?? null,
    });
  }

  /** Compute from a folded SSE state (indexing page path). */
  static fromStream(state: IndexingStreamSnapshot): ProgressView {
    return IndexingProgress._compute({
      phase: state.phase,
      status: state.job?.status,
      scannedCount: state.job?.scannedCount ?? 0,
      totalCount: state.job?.totalCount ?? 0,
      pagesSubmitted: state.pagesSubmitted,
      totalPages: state.totalPages,
      // SSE state doesn't carry phaseStartedAt (or the generic per-phase
      // progress fields) — the snapshot path does. ETA on the indexing page
      // falls back to the snapshot through ``fromJob`` when the caller has
      // it (most pages render both).
      phaseStartedAt: state.job?.phaseStartedAt ?? null,
      phaseProgressCurrent: state.job?.phaseProgressCurrent ?? null,
      phaseProgressTotal: state.job?.phaseProgressTotal ?? null,
      phaseProgressUnit: state.job?.phaseProgressUnit ?? null,
    });
  }

  /** Human label for *phase*, exposed for the phase strip. */
  static label(phase: IndexingPhase): string {
    return PHASE_LABEL[phase];
  }

  /** Format *seconds* as "~3 min left" / "~45 s left"; ``""`` if null. */
  static formatEta(seconds: number | null): string {
    if (seconds == null || seconds <= 0 || !Number.isFinite(seconds)) return "";
    if (seconds < 60) return `~${Math.round(seconds)}s left`;
    const m = Math.round(seconds / 60);
    if (m < 60) return `~${m} min left`;
    const h = Math.floor(m / 60);
    const rem = m % 60;
    return rem ? `~${h}h ${rem}m left` : `~${h}h left`;
  }

  // ── Internal ────────────────────────────────────────────────────────

  private static _compute(input: ComputeInput): ProgressView {
    // Pick a phase — explicit if known, else infer from the `status`
    // field so a run with no `phase` reported still renders meaningfully.
    let phase: IndexingPhase = input.phase ?? "clone";
    if (!input.phase) {
      if (input.status === "scanning") phase = "scan";
      else if (input.status === "finalizing") phase = "pages";
      else if (input.status === "complete") phase = "finalize";
    }
    if (input.status === "complete") {
      return { pct: 100, phase: "finalize", label: PHASE_LABEL.finalize, statusLine: "Done", etaSeconds: 0 };
    }

    const [lo, hi] = PHASE_RANGE[phase];
    let sub = 0;
    let line = "";
    if (phase === "scan" && input.totalCount > 0) {
      sub = input.scannedCount / Math.max(1, input.totalCount);
      line = `${input.scannedCount} of ${input.totalCount} files`;
    } else if (phase === "pages" && (input.totalPages ?? 0) > 0) {
      sub = input.pagesSubmitted / Math.max(1, input.totalPages ?? 1);
      line = `Page ${input.pagesSubmitted} of ${input.totalPages}`;
    } else if (input.phaseProgressCurrent != null) {
      // Phase-agnostic ladder for every OTHER phase (today: graph/enrich —
      // works for a future phase with zero changes here, per the class doc
      // above). One branch: a positive total ⇒ a real fraction; otherwise
      // ``sub`` stays 0 (floor pct, no ETA) but the line still reports real
      // activity instead of staying empty for the phase's whole duration.
      const current = input.phaseProgressCurrent;
      const total = input.phaseProgressTotal;
      const unit = input.phaseProgressUnit ?? DEFAULT_PROGRESS_UNIT;
      if (total != null && total > 0) {
        sub = current / Math.max(1, total);
        line = `${current} of ${total} ${unit}`;
      } else {
        line = `${current} ${unit} processed`;
      }
    }
    sub = Math.max(0, Math.min(1, sub));

    // Headroom: don't paint 100% of the phase until the next phase event arrives.
    const reach = lo + (hi - lo) * Math.min(sub, 0.98);
    const pct = Math.round(reach);

    return {
      pct,
      phase,
      label: PHASE_LABEL[phase] ?? "Indexing repository",
      statusLine: line,
      etaSeconds: IndexingProgress._eta({ ...input, phase, sub }),
    };
  }

  /**
   * ETA — a measured-rate-or-nothing estimate of time left in the CURRENT
   * phase. Two things this deliberately does NOT do, both removed by this
   * fix:
   *
   *   - No fixed per-phase budget fallback. The old fallback (a
   *     ``PHASE_BUDGET_S`` lookup, sized "for ~30 files, 25 pages") was
   *     wrong by 15-20x on a real multi-thousand-file repo, and — because
   *     it never changed once picked — never counted down either: a run
   *     could sit at a frozen bar with a stale ETA for its entire real
   *     duration. An absent ETA is honest; a guess that doesn't scale with
   *     repo size is not.
   *   - No trailing-phase summation. Every phase after the current one
   *     hasn't started, so there is no measured number to add for it —
   *     summing a guessed budget for an unstarted phase onto an otherwise
   *     honest current-phase estimate just re-introduces the same
   *     dishonesty one phase early. The reported number is therefore
   *     "time left in this phase," not "time left in the whole job" — a
   *     real, shrinking number beats a compounded guess.
   */
  private static _eta(
    input: ComputeInput & { phase: IndexingPhase; sub: number },
  ): number | null {
    const elapsed = input.phaseStartedAt
      ? Math.max(0, Date.now() / 1000 - new Date(input.phaseStartedAt).getTime() / 1000)
      : null;

    let inPhase: number | null;
    if (input.phase === "pages" && (input.totalPages ?? 0) > 0) {
      const remaining = Math.max(0, (input.totalPages ?? 0) - input.pagesSubmitted);
      // Measured per-page rate once we have ≥1 page committed:
      // (elapsed / pagesSubmitted) extrapolated to remaining pages.
      inPhase = elapsed != null && input.pagesSubmitted > 0
        ? (elapsed / input.pagesSubmitted) * remaining
        : null;
    } else if (input.phase === "scan" && input.totalCount > 0) {
      const remaining = Math.max(0, input.totalCount - input.scannedCount);
      inPhase = elapsed != null && input.scannedCount > 0
        ? (elapsed / input.scannedCount) * remaining
        : null;
    } else if (elapsed != null && input.sub > 0) {
      // Generic linear extrapolation off the per-phase signal (graph/enrich,
      // once ``phaseProgressCurrent``/``phaseProgressTotal`` populate ``sub``
      // above): ``elapsed / sub`` is the phase's total-time estimate;
      // subtract elapsed for what's left.
      inPhase = elapsed / Math.max(0.01, input.sub) - elapsed;
    } else {
      // No measurable rate for this phase: clone/plan/finalize never carry
      // a progress signal, and graph/enrich read this way until
      // ``phaseProgressCurrent`` starts arriving (old job) or its total is
      // still unknown. Showing nothing here is the fix — see the class doc
      // above.
      inPhase = null;
    }

    if (inPhase == null || !Number.isFinite(inPhase) || inPhase < 0) return null;
    return inPhase;
  }
}
