/**
 * IndexingScreen — cancelability is the server's call, not a status guess.
 *
 * `canCancel` used to be derived from the same local status-literal set that
 * picks the recovery panel (`failed`/`interrupted`/`cancelled` read as
 * "incomplete, hide Cancel") — exactly backwards for an `interrupted` job
 * whose backing session never resolved: the run reads as incomplete AND the
 * server still considers it active, but the local rule hid the one control
 * that could clear it, leaving the job permanently stuck. The fix reads
 * `IndexingJob.isActive` — the server's own verdict — instead of re-deriving
 * terminality from `status` locally.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IndexingScreen } from "@/components/wiki/IndexingScreen";
import * as client from "@/components/wiki/api/client";
import type { IndexingJob } from "@/components/wiki/api/types";

vi.mock("@/components/wiki/api/client", () => ({
  // The snapshot poll is the authoritative source for `isActive`; the SSE
  // stream never carries it (each event sets only a handful of narrow
  // fields, never the full wire object), so it yields nothing here on
  // purpose — matches `indexingRecovery.test.tsx`'s convention.
  // eslint-disable-next-line require-yield
  subscribeToIndexing: vi.fn(async function* () {
    return;
  }),
  getIndexingJob: vi.fn(),
  cancelIndexingJob: vi.fn(),
  resumeIndexingJob: vi.fn(),
}));

const getIndexingJob = vi.mocked(client.getIndexingJob);
const cancelIndexingJob = vi.mocked(client.cancelIndexingJob);

/** The job at the center of the bug: stopped, but the server still counts it
 *  as active because its backing session was never cleanly resolved. */
const STUCK_JOB: IndexingJob = {
  jobId: "j1",
  slug: "git.example.com/acme/widgets",
  status: "interrupted",
  scannedCount: 30,
  totalCount: 30,
  currentFile: null,
  phase: "pages",
  pagesSubmitted: 4,
  totalPages: 10,
  isActive: true,
  error: { code: "internal", message: "worker restarted" },
};

/** Mirrors a snapshot from before the server started stamping `isActive`. */
const PRE_ROLLOUT_JOB: IndexingJob = {
  jobId: "j1",
  slug: "git.example.com/acme/widgets",
  status: "interrupted",
  scannedCount: 30,
  totalCount: 30,
  currentFile: null,
  phase: "pages",
  pagesSubmitted: 4,
  totalPages: 10,
  error: { code: "internal", message: "worker restarted" },
};

function renderScreen(slug = STUCK_JOB.slug) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const loc = memoryLocation({ path: "/wiki/indexing?jobId=j1", record: true });
  render(
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>
        <IndexingScreen jobId="j1" slug={slug} />
      </Router>
    </QueryClientProvider>,
  );
  return loc;
}

afterEach(cleanup);
beforeEach(() => {
  getIndexingJob.mockReset();
  cancelIndexingJob.mockReset();
  cancelIndexingJob.mockResolvedValue(STUCK_JOB);
});

describe("IndexingScreen — canCancel reads isActive, not a local status set", () => {
  it("an active-but-stalled job renders Cancel ALONGSIDE the recovery panel", async () => {
    getIndexingJob.mockResolvedValue(STUCK_JOB);
    renderScreen();

    // The recovery panel is unaffected — that read stays presentational.
    expect(await screen.findByText("Indexing was interrupted")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /resume indexing/i })).toBeInTheDocument();
    // The stuck job's one previous dead end: Cancel is now reachable.
    expect(screen.getByRole("button", { name: /cancel indexing/i })).toBeInTheDocument();
  });

  it("clicking Cancel calls the DELETE endpoint and returns to the wiki list", async () => {
    getIndexingJob.mockResolvedValue(STUCK_JOB);
    const user = userEvent.setup();
    const loc = renderScreen();

    await user.click(await screen.findByRole("button", { name: /cancel indexing/i }));
    await waitFor(() => expect(cancelIndexingJob).toHaveBeenCalledWith("j1"));
    await waitFor(() => expect(loc.history.at(-1)).toBe("/wiki"));
  });

  it("a genuinely terminal job (isActive: false) hides Cancel, even though it's also 'interrupted'", async () => {
    getIndexingJob.mockResolvedValue({ ...STUCK_JOB, isActive: false });
    renderScreen();

    expect(await screen.findByText("Indexing was interrupted")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cancel indexing/i })).toBeNull();
  });

  it("a job the server hasn't confirmed active (isActive absent) fails closed", async () => {
    getIndexingJob.mockResolvedValue(PRE_ROLLOUT_JOB);
    renderScreen();

    expect(await screen.findByText("Indexing was interrupted")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cancel indexing/i })).toBeNull();
  });

  it("a live, non-terminal job (isActive: true) renders Cancel with no recovery panel", async () => {
    getIndexingJob.mockResolvedValue({
      ...STUCK_JOB,
      status: "scanning",
      error: undefined,
      isActive: true,
    });
    renderScreen();

    expect(await screen.findByRole("button", { name: /cancel indexing/i })).toBeInTheDocument();
    expect(screen.queryByText("Indexing was interrupted")).toBeNull();
  });
});
