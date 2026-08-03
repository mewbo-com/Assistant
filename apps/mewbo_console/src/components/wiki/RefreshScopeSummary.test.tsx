/**
 * RefreshScopeSummary — renders nothing until there is something honest to
 * say, then renders the right half of the union (full-rebuild reason vs.
 * scoped touched-counts). `FULL_REASON_COPY`'s totality over
 * `RefreshFullReason` is enforced by `tsc` via its `Record<...>` type (the
 * `progress.ts` `PHASE_LABEL` idiom this file follows) — a missing reason is
 * a compile error, so no runtime totality test is needed for that half; this
 * suite instead checks the reasons that actually render distinct copy.
 *
 * vitest runs WITHOUT globals → explicit cleanup (console convention).
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { RefreshScopeSummary } from "./RefreshScopeSummary";
import type { RefreshDecision, ScopePreview } from "./api/types";

afterEach(cleanup);

const SCOPE_PREVIEW: ScopePreview = {
  filesAdded: 2,
  filesModified: 5,
  filesDeleted: 1,
  earlyCutoffFiles: 3,
  affectedEntities: 7,
  memoryKept: 40,
  memoryInvalidated: 2,
  memoryRevalidated: 2,
  pagesKeep: 10,
  pagesEdit: 3,
  pagesRegenerate: 1,
  newPages: 1,
  llmCalls: 4,
};

const SCOPED: RefreshDecision = { path: "scoped", mismatches: [] };
const FULL_REQUESTED: RefreshDecision = { path: "full", reason: "requested", mismatches: [] };
const FULL_MISMATCH: RefreshDecision = {
  path: "full",
  reason: "fingerprint_mismatch",
  mismatches: [
    { field: "embedding_model", expected: "openai/text-embedding-3-small", actual: "openai/text-embedding-3-large" },
    { field: "graph_schema_version", expected: "1", actual: "2" },
  ],
};

describe("RefreshScopeSummary", () => {
  it("renders nothing on a first index (no refreshDecision at all)", () => {
    const { container } = render(<RefreshScopeSummary />);
    expect(container).toBeEmptyDOMElement();
  });

  it("names the scoped path and its consequence before the delta pass reports back", () => {
    // This used to render NOTHING until the counts landed. Which path a run
    // took is decided up front and is the most useful thing to say about it —
    // without it a user watches a progress bar identical to a full index's with
    // nothing on screen telling them this run will not touch a page.
    const { container } = render(<RefreshScopeSummary refreshDecision={SCOPED} />);
    expect(screen.getByText("Scoped refresh.")).toBeInTheDocument();
    expect(container.textContent).toContain(
      "Documentation pages are not regenerated on this path",
    );
  });

  it("renders the reason sentence for a full rebuild, before any scope preview exists", () => {
    // `getByText` with a regex would ambiguously match both the `<p>` and its
    // icon-only wrapper (an SVG contributes no text, so their textContent is
    // identical) — assert on the rendered text as a whole instead.
    const { container } = render(<RefreshScopeSummary refreshDecision={FULL_REQUESTED} />);
    expect(screen.getByText("Full rebuild.")).toBeInTheDocument();
    expect(container.textContent).toContain("A full rebuild was requested for this refresh.");
  });

  it("appends the mismatching field names for fingerprint_mismatch", () => {
    const { container } = render(<RefreshScopeSummary refreshDecision={FULL_MISMATCH} />);
    expect(container.textContent).toContain(
      "Changed: embedding model, graph schema version.",
    );
  });

  it("renders the scoped touched-counts grid once the scope preview lands", () => {
    render(<RefreshScopeSummary refreshDecision={SCOPED} scopePreview={SCOPE_PREVIEW} />);
    // Headline groups: files changed (2+5+1=8), pages flagged stale (3+1+1=5).
    expect(screen.getByText("8")).toBeInTheDocument();
    expect(screen.getByText("files changed")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText("pages flagged stale")).toBeInTheDocument();
    expect(screen.getByText("entities affected")).toBeInTheDocument();
    // Footnote, not a headline number.
    expect(screen.getByText(/3 files needed no further work\./)).toBeInTheDocument();
    expect(screen.getByText(/4 model calls to reconcile memory/)).toBeInTheDocument();
  });

  it("does not claim the flagged pages were updated", () => {
    // Nothing on the scoped path writes a page, so the old "pages to update"
    // label and its past-tense "3 edited · 1 regenerated" sub-label both stated
    // work that was never attempted.
    const { container } = render(
      <RefreshScopeSummary refreshDecision={SCOPED} scopePreview={SCOPE_PREVIEW} />,
    );
    expect(screen.queryByText("pages to update")).toBeNull();
    expect(container.textContent).not.toContain("3 edited");
    expect(container.textContent).toContain("need edits");
  });

  it("qualifies zero model calls so it cannot read as free documentation", () => {
    // `llmCalls` counts memory reconciliation and never included documentation.
    // Reported bare beside "10 pages need rewriting" it read as the price of
    // regenerating them — i.e. as free work rather than as work never done.
    const { container } = render(
      <RefreshScopeSummary
        refreshDecision={SCOPED}
        scopePreview={{ ...SCOPE_PREVIEW, llmCalls: 0 }}
      />,
    );
    // Two sibling text nodes share the footnote `<p>` (the early-cutoff
    // clause + this one), so match on the combined text rather than an
    // exact single-node string.
    expect(container.textContent).toContain("none were spent on documentation");
    expect(container.textContent).not.toContain("No LLM calls needed.");
  });
});
