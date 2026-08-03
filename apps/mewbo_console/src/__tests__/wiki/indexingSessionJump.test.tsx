/**
 * IndexingScreen — "Watch the indexing session" jump.
 *
 * The loading screen offers a direct hop into the Mewbo session running the
 * (re)index, sourced from the `sessionId` the job snapshot stamps from the
 * job→session binding. Three properties worth locking:
 *   - present + navigates to `/s/<id>` while the index is live,
 *   - ABSENT when the snapshot carries no session (a graph-only index is
 *     sessionless — the affordance is never a disabled dead end),
 *   - still present on a stopped run, where the transcript matters most.
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
  // The SSE stream yields nothing: `sessionId` rides the snapshot poll only,
  // so the jump must not depend on a single stream frame having arrived.
  // eslint-disable-next-line require-yield
  subscribeToIndexing: vi.fn(async function* () {
    return;
  }),
  getIndexingJob: vi.fn(),
  cancelIndexingJob: vi.fn(),
  resumeIndexingJob: vi.fn(),
}));

const getIndexingJob = vi.mocked(client.getIndexingJob);

const LIVE_JOB: IndexingJob = {
  jobId: "j1",
  slug: "git.example.com/acme/widgets",
  status: "scanning",
  scannedCount: 12,
  totalCount: 40,
  currentFile: "src/main.py",
  phase: "scan",
  sessionId: "sess-indexer-1",
};

function renderScreen() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const loc = memoryLocation({ path: "/wiki/indexing?jobId=j1", record: true });
  render(
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>
        <IndexingScreen jobId="j1" slug={LIVE_JOB.slug} />
      </Router>
    </QueryClientProvider>,
  );
  return loc;
}

const JUMP = { name: /watch the indexing session/i };

afterEach(cleanup);
beforeEach(() => {
  getIndexingJob.mockReset();
});

describe("IndexingScreen — session jump", () => {
  it("navigates to the backing session page", async () => {
    const user = userEvent.setup();
    getIndexingJob.mockResolvedValue(LIVE_JOB);
    const loc = renderScreen();

    // Wait for the snapshot to land BEFORE searching for the element — the
    // same guard the sessionless case below already carries, and it is needed
    // here for a different reason. This is the FIRST test in the file, so it
    // pays this module graph's cold Vite transform; under a parallel full-suite
    // run that alone can outlast `findByRole`'s 1000ms default, and the whole
    // 1000ms gets spent before the query has even resolved. Waiting on the
    // query first means the element search starts from a settled state.
    await waitFor(() => expect(getIndexingJob).toHaveBeenCalled());
    await user.click(await screen.findByRole("button", JUMP));
    expect(loc.history.at(-1)).toBe("/s/sess-indexer-1");
  });

  it("renders nothing when the job has no backing session", async () => {
    getIndexingJob.mockResolvedValue({ ...LIVE_JOB, sessionId: undefined });
    renderScreen();

    // Wait for the snapshot to land AND render (the phase label only reaches
    // the screen through it — the stream yields nothing) before asserting the
    // affordance stayed away; otherwise this passes on the pre-fetch render
    // and proves nothing.
    await waitFor(() => expect(getIndexingJob).toHaveBeenCalled());
    await screen.findByText("Scanning files");
    expect(screen.queryByRole("button", JUMP)).toBeNull();
  });

  it("explains the absence in TEXT when the run took the scoped path", async () => {
    // The button's absence is a CORRECT signal ("nothing to watch") that the
    // user had no way to read as one — 18 consecutive runs had shown it, so the
    // 19th reads as a broken button rather than a different kind of run. It
    // stays text: a disabled control says "this should work and doesn't".
    getIndexingJob.mockResolvedValue({
      ...LIVE_JOB,
      sessionId: undefined,
      refreshDecision: { path: "scoped", mismatches: [] },
    });
    renderScreen();

    await waitFor(() => expect(getIndexingJob).toHaveBeenCalled());
    expect(
      await screen.findByText(/runs without an agent session/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", JUMP)).toBeNull();
  });

  it("stays silent for a sessionless run that is not a scoped refresh", async () => {
    // A graph-only index is sessionless too, but for an unrelated reason — this
    // copy would be wrong there, so it is gated on the decision, not on absence.
    getIndexingJob.mockResolvedValue({ ...LIVE_JOB, sessionId: undefined });
    renderScreen();

    await waitFor(() => expect(getIndexingJob).toHaveBeenCalled());
    await screen.findByText("Scanning files");
    expect(screen.queryByText(/runs without an agent session/i)).toBeNull();
  });

  it("stays available on a stopped run, beside Resume", async () => {
    getIndexingJob.mockResolvedValue({
      ...LIVE_JOB,
      status: "interrupted",
      error: { code: "internal", message: "worker restarted" },
    });
    renderScreen();

    expect(await screen.findByRole("button", JUMP)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /resume indexing/i })).toBeInTheDocument();
  });
});
