/**
 * `reduceIndexing` — the `scope_preview` fold.
 *
 * `scope_preview` is the newest event on the indexing stream: emitted once,
 * at the end of a SCOPED refresh's delta pass, carrying the same counts as
 * `IndexingJob.scopePreview` (one write on the BE, two transports). This
 * pins the reducer's half of that contract — the event lands on
 * `state.job.scopePreview` verbatim and leaves every other folded field
 * alone.
 */
import { describe, expect, it } from "vitest";

import { reduceIndexing, type IndexingStreamState } from "./streamHooks";
import type { IndexingEvent, ScopePreview } from "./types";

const SEED: IndexingStreamState = {
  job: null,
  history: [],
  phase: null,
  totalPages: null,
  pagesSubmitted: 0,
  logs: [],
  error: null,
};

const PREVIEW: ScopePreview = {
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

const QUEUED: IndexingEvent = {
  type: "queued",
  jobId: "job-1",
  slug: "git.example.com/acme/beacon",
  totalCount: 10,
};

describe("reduceIndexing — scope_preview", () => {
  it("folds the event's counts onto the job snapshot verbatim", () => {
    const afterQueued = reduceIndexing(SEED, QUEUED);
    const state = reduceIndexing(afterQueued, { type: "scope_preview", ...PREVIEW });
    expect(state.job?.scopePreview).toEqual(PREVIEW);
  });

  it("leaves fields folded by earlier events untouched", () => {
    let state = reduceIndexing(SEED, QUEUED);
    state = reduceIndexing(state, { type: "scanning", file: "a.py", index: 0, totalCount: 10 });
    state = reduceIndexing(state, { type: "scope_preview", ...PREVIEW });
    expect(state.job?.status).toBe("scanning");
    expect(state.job?.currentFile).toBe("a.py");
    expect(state.job?.scopePreview).toEqual(PREVIEW);
  });

  it("is a no-op (never throws) if it somehow arrives before queued", () => {
    const state = reduceIndexing(SEED, { type: "scope_preview", ...PREVIEW });
    expect(state.job).toBeNull();
  });
});
