/**
 * App — opening a session does not wait on the sessions listing.
 *
 * `GET /api/sessions` summarises EVERY session; on a large deployment it is
 * measured in seconds. The detail route used to resolve its subject out of that
 * listing and render "Loading session…"/"Session not found." until it arrived,
 * so opening ONE session paid for a summary of all of them. The per-session
 * `/events` response carries the same authoritative facts for this one id
 * (title, status, running, recoverable, terminated), so the page now mounts from
 * the route's id and fills in from whichever source answers first.
 *
 * These tests hold the listing in flight (never resolving) — the state the fix
 * exists for — and drive the real `<App/>`, because the defect lived in the
 * route's gating rather than in any component's own rendering.
 */
import { cleanup, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { App } from "../App";
import * as client from "../api/client";
import * as sessionStreamApi from "../api/sessionStream";
import type { SessionSummary } from "../types";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
  Toaster: () => null,
}));

vi.mock("../api/client", () => ({
  listSessions: vi.fn(),
  createSession: vi.fn(),
  postQuery: vi.fn(),
  sendMessage: vi.fn(),
  uploadAttachments: vi.fn(),
  // eslint-disable-next-line @typescript-eslint/no-empty-function
  fetchUsage: vi.fn(() => new Promise(() => {})), // never resolves — untested facet
  archiveSession: vi.fn(),
  unarchiveSession: vi.fn(),
  updateSessionTitle: vi.fn(),
  regenerateTitle: vi.fn(),
  listNotifications: vi.fn(),
  dismissNotification: vi.fn(),
  clearNotifications: vi.fn(),
  createShare: vi.fn(),
  exportSession: vi.fn(),
  interruptStep: vi.fn(),
  approvePlan: vi.fn(),
  answerQuestion: vi.fn(),
  recoverSession: vi.fn(),
  forkSession: vi.fn(),
  listModels: vi.fn(),
  listTools: vi.fn(),
  listSkills: vi.fn(),
  listProjects: vi.fn(),
  fetchCommands: vi.fn(),
  fetchProjectFiles: vi.fn(),
  executeCommand: vi.fn(),
  getConfig: vi.fn(),
  getConfigSchema: vi.fn(),
  patchConfig: vi.fn(),
}));
// `streamSession` is the only export replaced — `isSessionStateFrame` /
// `isStreamEndFrame` stay real so `useSessionEvents`'s own frame-folding logic
// (which imports them directly) keeps working against the scripted frames
// below.
vi.mock("../api/sessionStream", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/sessionStream")>();
  return {
    ...actual,
    streamSession: vi.fn(),
  };
});
vi.mock("../components/wiki/api/client", () => ({
  getWikiSessionLink: vi.fn(),
  listProjects: vi.fn().mockResolvedValue([]),
}));
vi.mock("../components/WorkspacePanel", () => ({
  WorkspacePanel: () => <div data-testid="workspace-panel" />,
}));
vi.mock("../components/triggers/SessionTriggersSection", () => ({
  SessionTriggersSection: () => <div data-testid="triggers-section" />,
}));

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

const listSessions = vi.mocked(client.listSessions);
const streamSession = vi.mocked(sessionStreamApi.streamSession);
const listModels = vi.mocked(client.listModels);
const listTools = vi.mocked(client.listTools);
const listSkills = vi.mocked(client.listSkills);
const listProjects = vi.mocked(client.listProjects);
const listNotifications = vi.mocked(client.listNotifications);
const fetchCommands = vi.mocked(client.fetchCommands);
const fetchProjectFiles = vi.mocked(client.fetchProjectFiles);
const getConfig = vi.mocked(client.getConfig);
const getConfigSchema = vi.mocked(client.getConfigSchema);

/** A promise that never settles — a request that stays in flight. */
function inFlight<T>(): Promise<T> {
  return new Promise(() => undefined);
}

function renderAtSession() {
  const { hook } = memoryLocation({ path: "/s/sess-1", record: true });
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return rtlRender(
    <QueryClientProvider client={qc}>
      <Router hook={hook}>
        <App />
      </Router>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  // The listing never answers — every test here is about what the page can do
  // without it.
  listSessions.mockImplementation(inFlight);
  listNotifications.mockResolvedValue([]);
  listModels.mockResolvedValue({ models: [], default: "" });
  listTools.mockResolvedValue([]);
  listSkills.mockResolvedValue([]);
  listProjects.mockResolvedValue([]);
  fetchCommands.mockResolvedValue([]);
  fetchProjectFiles.mockResolvedValue({ files: [], attachments: [] });
  getConfig.mockResolvedValue({ config: {}, secrets: {} });
  getConfigSchema.mockResolvedValue({ type: "object", properties: {} });
  streamSession.mockImplementation(() =>
    frameStream([sessionStateFrame({ running: false }), { type: "stream_end" }]),
  );
});

describe("session detail with the sessions listing still in flight", () => {
  test("renders the shell and names the session from its own poll", async () => {
    streamSession.mockReturnValue(
      frameStream(
        [
          sessionStateFrame({
            running: true,
            status: "running",
            title: "Refactor the billing pipeline",
          }),
        ],
        true,
      ),
    );

    renderAtSession();

    // The shell: back out, and a composer to steer with.
    const back = await screen.findByRole("button", { name: "Back" });
    expect(await screen.findByTestId("inputbar-detail")).toBeInTheDocument();
    // Never the list-derived dead end.
    expect(screen.queryByText("Session not found.")).not.toBeInTheDocument();
    expect(screen.queryByText("Loading session…")).not.toBeInTheDocument();
    // Title and status come from the per-session response. Scoped to the header
    // because a live run also labels itself in the composer's run strip.
    const header = back.closest("header") as HTMLElement;
    expect(await screen.findByText("Refactor the billing pipeline")).toBeInTheDocument();
    await waitFor(() => expect(within(header).getByText("Running")).toBeInTheDocument());
  });

  test("claims no status while neither source has one", async () => {
    // Both fetches in flight: the header must not stamp "Idle" on a session
    // whose state is merely unknown — that reads as wrong the instant a running
    // session's poll answers. No frames arrive at all — a still-open stream
    // that simply hasn't delivered anything yet, not a session_state frame.
    streamSession.mockReturnValue(frameStream([], true));

    renderAtSession();

    expect(await screen.findByTestId("inputbar-detail")).toBeInTheDocument();
    expect(screen.queryByText("Idle")).not.toBeInTheDocument();
  });

  test("a terminated session is read as terminated without the listing", async () => {
    // `session.status === "terminated"` used to be the only signal here and it
    // comes from the listing, so a cold listing would have left a live composer
    // on a session that can never run again.
    streamSession.mockReturnValue(
      frameStream([
        sessionStateFrame({ running: false, status: "terminated", terminated: true }),
        { type: "stream_end" },
      ]),
    );

    renderAtSession();

    expect(await screen.findByTestId("inputbar-terminated")).toBeInTheDocument();
    expect(screen.queryByTestId("inputbar-detail")).not.toBeInTheDocument();
  });
});

describe("session detail once the listing answers", () => {
  test("the listing's title outranks the server projection", async () => {
    // A rename patches the listing optimistically (`applyTitle`), so the
    // snapshot has to win — otherwise the just-typed title flickers back to the
    // server's older copy on the next poll.
    const listed: SessionSummary = {
      session_id: "sess-1",
      title: "Renamed by the operator",
      status: "completed",
    };
    listSessions.mockResolvedValue([listed]);
    streamSession.mockReturnValue(
      frameStream([
        sessionStateFrame({ running: false, status: "completed", title: "Stale server title" }),
        { type: "stream_end" },
      ]),
    );

    renderAtSession();

    await waitFor(() =>
      expect(screen.getByText("Renamed by the operator")).toBeInTheDocument(),
    );
    expect(screen.queryByText("Stale server title")).not.toBeInTheDocument();
  });
});
