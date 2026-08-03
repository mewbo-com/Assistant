/**
 * App — create-and-run routes on session creation, not on query acceptance.
 *
 * `handleCreateAndRun` used to await `postQuery` before calling
 * `goToSession`, so the operator sat on the landing page for a round trip that
 * routing never needed: the session id is known the moment `create` resolves,
 * and the session page can already paint its shell from the transcript.
 *
 * Moving navigation earlier moves the error surface with it, which is the other
 * half of the contract here: `actionError` renders an inline Alert on the
 * landing page, so a failure raised AFTER the hop has to reach the operator as
 * a toast instead of being set into an unmounted surface.
 *
 * Drives the real `<App/>` (per `appNavigation.test.tsx`) because the wiring
 * between the composer, the create mutation and the router is the subject —
 * asserting the handler in isolation would not catch a session that routes to a
 * page which cannot resolve it.
 */
import { cleanup, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { App } from "../App";
import * as client from "../api/client";
import * as sessionStreamApi from "../api/sessionStream";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
  Toaster: () => null,
}));
import { toast } from "sonner";

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
  // Landing composer.
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
// The session page's own panes are exercised by their own suites; this file is
// about which page the operator lands on and what it tells them.
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
const createSession = vi.mocked(client.createSession);
const postQuery = vi.mocked(client.postQuery);
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

function renderApp() {
  const { hook, history } = memoryLocation({ path: "/", record: true });
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = rtlRender(
    <QueryClientProvider client={qc}>
      <Router hook={hook}>
        <App />
      </Router>
    </QueryClientProvider>,
  );
  return { ...result, history };
}

/** A promise that never settles — a request that stays in flight. */
function inFlight<T>(): Promise<T> {
  return new Promise(() => undefined);
}

/** Type a prompt into the landing composer and send it. */
async function sendFromLanding(text: string) {
  const user = userEvent.setup();
  const bars = await screen.findAllByTestId("inputbar-home");
  const bar = bars[bars.length - 1];
  await user.type(within(bar).getByLabelText("Task description"), text);
  await user.click(within(bar).getByLabelText("Send query"));
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  listSessions.mockResolvedValue([]);
  listNotifications.mockResolvedValue([]);
  listModels.mockResolvedValue({ models: [], default: "" });
  listTools.mockResolvedValue([]);
  listSkills.mockResolvedValue([]);
  listProjects.mockResolvedValue([]);
  fetchCommands.mockResolvedValue([]);
  fetchProjectFiles.mockResolvedValue({ files: [], attachments: [] });
  getConfig.mockResolvedValue({ config: {}, secrets: {} });
  getConfigSchema.mockResolvedValue({ type: "object", properties: {} });
  // A running session's stream stays open — no `stream_end`.
  streamSession.mockImplementation(() =>
    frameStream([sessionStateFrame({ running: true })], true),
  );
  createSession.mockResolvedValue("sess-new");
});

describe("create-and-run navigates on creation", () => {
  test("lands on the session route while /query is still in flight", async () => {
    // Never resolves: if routing waited on acceptance, the assertion below is
    // what fails, rather than something subtler further downstream.
    postQuery.mockImplementation(inFlight);

    const { history } = renderApp();
    await sendFromLanding("do the thing");

    await waitFor(() => expect(history.at(-1)).toBe("/s/sess-new"));
    expect(postQuery).toHaveBeenCalled();
  });

  test("the session page resolves the new session and paints its starting state", async () => {
    // The runtime hides a session with no visible event and no run in flight
    // from `list_sessions`, so the list refetch cannot supply this row — the
    // create mutation seeds it. Without that seed the operator lands on
    // "Session not found." instead of a session shell.
    streamSession.mockReturnValue(
      frameStream(
        [
          {
            ts: "2026-07-01T10:00:00Z",
            type: "run_accepted",
            payload: { session_id: "sess-new", run_id: "sess-new:r1" },
          },
          sessionStateFrame({ running: true }),
        ],
        true,
      ),
    );

    renderApp();
    await sendFromLanding("do the thing");

    expect(await screen.findByTestId("inputbar-detail")).toBeInTheDocument();
    expect(await screen.findByText("Starting…")).toBeInTheDocument();
    expect(screen.queryByText("Session not found.")).not.toBeInTheDocument();
  });

  test("a query failure after the hop surfaces as a toast, not a swallowed error", async () => {
    postQuery.mockRejectedValue(new Error("query refused"));

    const { history } = renderApp();
    await sendFromLanding("do the thing");

    await waitFor(() => expect(history.at(-1)).toBe("/s/sess-new"));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("query refused")),
    );
  });

  test("a create failure keeps the operator on the landing page with the inline error", async () => {
    createSession.mockRejectedValue(new Error("create refused"));

    const { history } = renderApp();
    await sendFromLanding("do the thing");

    expect(await screen.findByText(/create refused/)).toBeInTheDocument();
    expect(history.at(-1)).toBe("/");
    expect(postQuery).not.toHaveBeenCalled();
  });
});
