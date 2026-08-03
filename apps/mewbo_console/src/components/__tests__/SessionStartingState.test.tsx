/**
 * SessionDetailView + ConversationTimeline — the accepted-run shell.
 *
 * The engine emits `run_accepted` the instant it takes a run, expressly so a
 * client can paint the session shell during the orchestrator's synchronous
 * cold start. Nothing consumed it: a transcript holding only that marker
 * produces no timeline rows, no open turn, and therefore an empty conversation
 * pane — the operator watched a blank page for the whole window.
 *
 * These tests drive the REAL `ConversationTimeline` (not the stub the sibling
 * `SessionDetailView.test.tsx` uses) because the defect was precisely a
 * derivation with no renderer behind it: asserting the prop would have passed
 * while the page still painted nothing. Everything else in the view is stubbed,
 * per that file's convention.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionDetailView } from "../SessionDetailView";
import type { EventRecord, SessionSummary } from "../../types";
import * as sessionStreamApi from "../../api/sessionStream";

vi.mock("../../api/client", () => ({
  // eslint-disable-next-line @typescript-eslint/no-empty-function
  fetchUsage: vi.fn(() => new Promise(() => {})), // never resolves — untested facet
  postQuery: vi.fn(),
  sendMessage: vi.fn(),
  uploadAttachments: vi.fn(),
  interruptStep: vi.fn(),
  approvePlan: vi.fn().mockResolvedValue(undefined),
  answerQuestion: vi.fn().mockResolvedValue({ ok: true }),
  recoverSession: vi.fn(),
  forkSession: vi.fn(),
  getConfig: vi.fn().mockResolvedValue({ config: {}, secrets: {} }),
  listProjects: vi.fn().mockResolvedValue([]),
}));
// `streamSession` is the only export replaced — `isSessionStateFrame` /
// `isStreamEndFrame` stay real so `useSessionEvents`'s own frame-folding logic
// (which imports them directly) keeps working against the scripted frames
// below.
vi.mock("../../api/sessionStream", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/sessionStream")>();
  return {
    ...actual,
    streamSession: vi.fn(),
  };
});
vi.mock("../wiki/api/client", () => ({
  getWikiSessionLink: vi.fn(),
  listProjects: vi.fn().mockResolvedValue([]),
}));
vi.mock("../WorkspacePanel", () => ({
  WorkspacePanel: () => <div data-testid="workspace-panel" />,
}));
vi.mock("../triggers/SessionTriggersSection", () => ({
  SessionTriggersSection: () => <div data-testid="triggers-section" />,
}));
vi.mock("../InputBar", () => ({
  InputBar: () => <div data-testid="input-bar" />,
}));

// jsdom lacks IntersectionObserver — TurnScroller observes rows on mount.
class IntersectionObserverStub {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
  takeRecords = vi.fn(() => []);
}
globalThis.IntersectionObserver =
  globalThis.IntersectionObserver ??
  (IntersectionObserverStub as unknown as typeof IntersectionObserver);

/**
 * Scripts the SSE transport for one `streamSession` call: replays `frames`
 * in order, then either ends (mirroring the server's `stream_end`) or hangs
 * forever (mirroring a still-open, still-running connection) depending on
 * `pending`.
 */
function frameStream(
  frames: sessionStreamApi.SessionStreamFrame[],
  pending = false,
): AsyncGenerator<sessionStreamApi.SessionStreamFrame> {
  async function* generator() {
    for (const frame of frames) {
      yield frame;
    }
    if (pending) {
      // A still-running stream never returns: the server holds the connection
      // open for the life of the run, so the generator must not complete or
      // the hook would treat it as a healthy close and re-subscribe.
      await new Promise<never>(() => undefined);
    }
  }
  return generator();
}

function sessionStateFrame(
  overrides: Partial<Omit<sessionStreamApi.SessionStateFrame, "type">> = {},
): sessionStreamApi.SessionStateFrame {
  return {
    type: "session_state",
    running: false,
    status: "",
    done_reason: "",
    title: "",
    recoverable: false,
    terminated: false,
    terminated_at: null,
    ...overrides,
  };
}

const streamSession = vi.mocked(sessionStreamApi.streamSession);

const session: SessionSummary = {
  session_id: "s1",
  title: "A session",
  status: "running",
};

const ACCEPTED: EventRecord = {
  ts: "2026-07-01T10:00:00Z",
  type: "run_accepted",
  payload: { session_id: "s1", run_id: "s1:r1" },
};

function renderDetail() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const { hook } = memoryLocation({ path: "/s/s1", record: true });
  return render(
    <QueryClientProvider client={qc}>
      <Router hook={hook}>
        <SessionDetailView session={session} onBack={vi.fn()} />
      </Router>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
});

describe("SessionDetailView — a run that has been accepted but not started", () => {
  it("renders the starting beat for a transcript holding only run_accepted", async () => {
    streamSession.mockReturnValue(
      frameStream(
        [ACCEPTED, sessionStateFrame({ running: true, status: "running", done_reason: "" })],
        true,
      ),
    );
    renderDetail();

    const beat = await screen.findByText("Starting…");
    expect(beat).toBeInTheDocument();
    // An honest affordance, not a fabricated assistant message: the beat is a
    // polite live region and carries no prose beyond its own label.
    expect(beat.closest("[role='status']")).toBeInTheDocument();
  });

  it("hands the readout over to the pending beat once the turn opens", async () => {
    streamSession.mockReturnValue(
      frameStream(
        [
          ACCEPTED,
          { ts: "2026-07-01T10:00:03Z", type: "user", payload: { text: "do the thing" } },
          sessionStateFrame({ running: true, status: "running", done_reason: "" }),
        ],
        true,
      ),
    );
    renderDetail();

    // The prompt is on screen with its in-flight beat, and the starting beat is
    // gone — one run, one readout. Asserted inside ONE `waitFor`: an open turn
    // auto-opens the trace panel, which re-parents the conversation, so a
    // `findByText` awaited on its own can resolve a node that the re-parent then
    // detaches before the assertion reads it.
    await waitFor(() => {
      expect(screen.getByText("do the thing")).toBeInTheDocument();
      expect(screen.getByText("Working")).toBeInTheDocument();
      expect(screen.queryByText("Starting…")).not.toBeInTheDocument();
    });
  });

  it("does not claim a run is starting when the server reports none running", async () => {
    // An acceptance marker abandoned by a killed process (before the API's
    // sweep settles the run) must not leave the page reading "Starting…".
    streamSession.mockReturnValue(
      frameStream([
        ACCEPTED,
        sessionStateFrame({ running: false, status: "incomplete", done_reason: "" }),
        { type: "stream_end" },
      ]),
    );
    renderDetail();

    await waitFor(() => expect(streamSession).toHaveBeenCalled());
    expect(screen.queryByText("Starting…")).not.toBeInTheDocument();
  });
});
