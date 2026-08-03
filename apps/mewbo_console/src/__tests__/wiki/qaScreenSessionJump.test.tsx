/**
 * QAScreen → "Watch the answering session".
 *
 * The Q&A screen is the one wiki surface that had no way back to the run doing
 * its work, so a user waiting on an answer could not read the transcript the
 * indexing screen has always exposed. The binding reaches the screen by TWO
 * independent routes and both are pinned here, because they cover disjoint
 * halves of the feature: the live `meta` event covers a streaming turn (which
 * is exactly when someone wants to watch), and the persisted snapshot covers a
 * replayed `?answer=` load (where no stream is ever opened).
 *
 * The third case is the absence rule: an answer with no backing session must
 * render NO affordance — not a disabled one, not one pointing at `/s/`.
 */
import type { ReactNode } from "react";

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import { QAScreen } from "@/components/wiki/QAScreen";
import * as wikiClient from "@/components/wiki/api/client";
import type { QaAnswer, QaEvent } from "@/components/wiki/api/types";

vi.mock("@/components/wiki/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/wiki/api/client")>();
  return {
    ...actual,
    getAnswer: vi.fn(),
    streamAnswer: vi.fn(),
    getPage: vi.fn(),
    getSourceExcerpt: vi.fn(),
  };
});
// Keep the model picker inert (no /api/models fetch during the screen render).
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({
    models: [],
    defaultModel: "",
    capabilities: {},
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));

const getAnswer = vi.mocked(wikiClient.getAnswer);
const streamAnswer = vi.mocked(wikiClient.streamAnswer);
const getPage = vi.mocked(wikiClient.getPage);

const JUMP = "Watch the answering session";

function makeAnswer(overrides: Partial<QaAnswer> = {}): QaAnswer {
  return {
    answerId: "ans1",
    fromPageId: "core",
    summarySources: [],
    model: "anthropic/claude-sonnet-4-5",
    blocks: [],
    accessedSources: [],
    modelsUsed: [],
    ...overrides,
  };
}

/**
 * Holds a generator suspended after its first frame, so the turn stays
 * mid-stream for the whole test. The consumer's `AbortSignal` tears the
 * effect down on unmount; nothing here ever needs to settle.
 */
const MID_STREAM = new Promise<void>(() => undefined);

/** A stream that opens with `meta` carrying its session, then stays open. */
async function* streamingWithSession(): AsyncGenerator<QaEvent> {
  yield {
    type: "meta",
    answerId: "ansX",
    model: "m",
    fromPageId: "core",
    sessionId: "sess-live",
  };
  // Deliberately no `complete` — this asserts the jump is offered DURING the
  // stream, not only once the answer settles.
  await MID_STREAM;
}

/** The pre-`sessionId` wire: `meta` with the field absent entirely. */
async function* streamingWithoutSession(): AsyncGenerator<QaEvent> {
  yield { type: "meta", answerId: "ansX", model: "m", fromPageId: "core" };
  await MID_STREAM;
}

/** A stream that never reaches `meta` — the round-trip window itself. */
// eslint-disable-next-line require-yield
async function* streamingNothing(): AsyncGenerator<QaEvent> {
  await MID_STREAM;
}

/**
 * A turn that SETTLES. Needed to populate the snapshot cache at all: the
 * snapshot query is gated on the stream being done, so a turn left mid-stream
 * never fetches one and cannot go stale.
 */
async function* settledWithSession(): AsyncGenerator<QaEvent> {
  yield {
    type: "meta",
    answerId: "ansX",
    model: "m",
    fromPageId: "core",
    sessionId: "sess-live",
  };
  yield { type: "complete", totalBlocks: 0 };
}

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const loc = memoryLocation({ path: "/wiki/qa", record: true });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>{children}</Router>
    </QueryClientProvider>
  );
  return { wrapper, loc };
}

afterEach(cleanup);
beforeEach(() => {
  // `reset`, not `clear` — `clearAllMocks` drops recorded calls but KEEPS
  // implementations, so a `mockResolvedValue` set by one case leaks into the
  // next and the suite silently depends on its own declaration order.
  vi.resetAllMocks();
  getPage.mockResolvedValue(null);
});

describe("QAScreen — watch the answering session", () => {
  it("offers the jump WHILE streaming, off the live meta event", async () => {
    streamAnswer.mockImplementation(() => streamingWithSession());
    getAnswer.mockResolvedValue(makeAnswer({ answerId: "ansX" }));
    const { wrapper, loc } = makeWrapper();
    const user = userEvent.setup();

    render(<QAScreen question="what is the engine" pageId="core" slug="o/r" />, {
      wrapper,
    });

    const button = await screen.findByRole("button", { name: JUMP });
    await user.click(button);
    expect(loc.history.at(-1)).toBe("/s/sess-live");
  });

  it("offers the jump on a replayed ?answer= load, off the snapshot", async () => {
    getAnswer.mockResolvedValue(makeAnswer({ sessionId: "sess-replayed" }));
    const { wrapper, loc } = makeWrapper();
    const user = userEvent.setup();

    render(
      <QAScreen question="how does it work" pageId="core" slug="o/r" answerId="ans1" />,
      { wrapper },
    );

    await user.click(await screen.findByRole("button", { name: JUMP }));
    expect(loc.history.at(-1)).toBe("/s/sess-replayed");
    // The replay path must stay LLM-free — the jump is not a second run.
    expect(streamAnswer).not.toHaveBeenCalled();
  });

  it("renders NO affordance for a sessionless answer (key absent, not empty)", async () => {
    getAnswer.mockResolvedValue(makeAnswer()); // no `sessionId` key at all
    const { wrapper } = makeWrapper();

    render(
      <QAScreen question="how does it work" pageId="core" slug="o/r" answerId="ans1" />,
      { wrapper },
    );

    await waitFor(() => expect(getAnswer).toHaveBeenCalledWith("ans1"));
    expect(screen.queryByRole("button", { name: JUMP })).toBeNull();
  });

  it("renders no affordance while a legacy meta (no sessionId) streams", async () => {
    streamAnswer.mockImplementation(() => streamingWithoutSession());
    const { wrapper } = makeWrapper();

    render(<QAScreen question="what is the engine" pageId="core" slug="o/r" />, {
      wrapper,
    });

    await waitFor(() => expect(streamAnswer).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: JUMP })).toBeNull();
  });

  it("does not carry a previous conversation's session across a switch", async () => {
    // The stream reducer only clears on `meta`, and switching to a different
    // answer drops the hook to a null input — so no `meta` ever arrives to
    // clear it. Unmasked, the jump would keep pointing at the run that
    // answered the PREVIOUS question, permanently and with no visible tell.
    streamAnswer.mockImplementation(() => streamingWithSession()); // sess-live
    getAnswer.mockResolvedValue(makeAnswer({ answerId: "other", sessionId: "sess-other" }));
    const { wrapper, loc } = makeWrapper();
    const user = userEvent.setup();

    const view = render(
      <QAScreen question="first question" pageId="core" slug="o/r" />,
      { wrapper },
    );
    await screen.findByRole("button", { name: JUMP });

    // Same screen instance, now pointed at a DIFFERENT persisted answer.
    view.rerender(
      <QAScreen question="another question" pageId="core" slug="o/r" answerId="other" />,
    );

    await waitFor(() => expect(getAnswer).toHaveBeenCalledWith("other"));
    await user.click(await screen.findByRole("button", { name: JUMP }));
    expect(loc.history.at(-1)).toBe("/s/sess-other");
    expect(loc.history.at(-1)).not.toBe("/s/sess-live");
  });

  it("does not serve a cached snapshot's session to a new cold question", async () => {
    // The subtler half of the same staleness, via the OTHER operand.
    // `activeAnswerId` falls back to the stream's own (unmasked) id, so a
    // settled screen re-pointed at a fresh question keeps the snapshot query
    // keyed on the PREVIOUS answer — and a disabled TanStack query still
    // serves its cached entry. The jump must go quiet until the new turn
    // identifies itself, never inherit the prior run.
    streamAnswer.mockImplementation(() => settledWithSession()); // ansX / sess-live
    getAnswer.mockResolvedValue(makeAnswer({ answerId: "ansX", sessionId: "sess-live" }));
    const { wrapper } = makeWrapper();

    const view = render(
      <QAScreen question="first question" pageId="core" slug="o/r" />,
      { wrapper },
    );
    await screen.findByRole("button", { name: JUMP });
    // The turn must SETTLE for the snapshot to be fetched and cached at all —
    // that cache entry is the thing under test.
    await waitFor(() => expect(getAnswer).toHaveBeenCalledWith("ansX"));

    // Same instance, now a fresh COLD question: no `answerId` prop at all.
    // The next turn's stream never reaches `meta` here, which is exactly the
    // window the guard has to cover.
    streamAnswer.mockImplementation(() => streamingNothing());
    view.rerender(<QAScreen question="a different question" pageId="core" slug="o/r" />);

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: JUMP })).toBeNull(),
    );
  });
});
