/**
 * useSessionEvents — the SSE transport, and the reconnect that must not
 * double-render.
 *
 * The console used to poll `/events` on a 1 Hz clock; it now holds one SSE
 * stream per open session. That moves the whole risk surface onto RECONNECT,
 * because the server replays the backlog on EVERY connect and the replay
 * cursor is deliberately INCLUSIVE — several events can share a timestamp, so
 * the cursor's own timestamp group is re-sent rather than skipped. Downstream,
 * `buildTimeline` renders whatever row it is handed and has zero duplicate
 * tolerance, so an un-deduplicated reconnect is a user-visible double-render
 * of the transcript, not a bookkeeping detail.
 *
 * Two layers, mirroring how the hook is built:
 *
 *  1. `SessionStreamState` as a pure unit — de-duplication by content key and
 *     the referential stability that keeps `useMemo(() => buildTimeline(events),
 *     [events])` from rebuilding the whole timeline on every replayed frame.
 *  2. The hook driving a scripted async-generator transport — cursor on
 *     reconnect, recovery from a mid-stream drop, the reconnect ladder, run
 *     state threaded off `session_state`, termination as an absorbing state,
 *     and unmount aborting.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Only the transport is replaced. The hook also imports `isSessionStateFrame`
// / `isStreamEndFrame` from this module, and a wholesale factory would leave
// those undefined — every control frame would then fold in as a transcript
// row and the events array would carry frames that have no `ts` at all.
vi.mock("@/api/sessionStream", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/sessionStream")>()),
  streamSession: vi.fn(),
}));

import * as sessionStream from "@/api/sessionStream";
import type { SessionStateFrame, SessionStreamFrame } from "@/api/sessionStream";
import { SessionStreamState, useSessionEvents } from "@/hooks/useSessionEvents";
import type { EventRecord } from "@/types";

const streamSession = vi.mocked(sessionStream.streamSession);

/**
 * The three rungs of the hook's reconnect ladder, spelled out because the
 * distinction between them IS the policy: a clean close and a drop are not the
 * same event.
 *
 * - clean close after frames landed → the server had nothing more to send, so
 *   waiting out the idle delay is correct;
 * - DROP after frames landed → the run may still be producing output nobody is
 *   receiving, so retry promptly; delivering frames resets the ladder, which is
 *   what makes this always its first rung;
 * - connect that delivered NOTHING → the ladder increments before the wait, so
 *   even the first retry sits a rung higher, and consecutive failures double
 *   from there (up to a 15s ceiling).
 */
const RESUBSCRIBE_MS = 3_000;
const BACKOFF_MIN_MS = 1_000;
const DROP_RETRY_MS = BACKOFF_MIN_MS;
const CONNECT_FAIL_RETRY_MS = BACKOFF_MIN_MS * 2;
const CONNECT_FAIL_RETRY_2_MS = BACKOFF_MIN_MS * 4;

function evt(
  ts: string,
  type = "assistant",
  payload: Record<string, unknown> = { text: type },
): EventRecord {
  return { ts, type, payload };
}

function stateFrame(patch: Partial<SessionStateFrame> = {}): SessionStateFrame {
  return {
    type: "session_state",
    running: false,
    status: "completed",
    done_reason: "completed",
    title: "a session",
    recoverable: false,
    terminated: false,
    terminated_at: null,
    ...patch,
  };
}

const STREAM_END: SessionStreamFrame = { type: "stream_end" };

/**
 * A real macrotask boundary, taken with the timer captured at module load —
 * before any test installs fake ones.
 *
 * This is what makes the scripted transport faithful rather than merely
 * convenient, and it is load-bearing. `sseStream` yields every frame from
 * behind an `await reader.read()`, i.e. genuine I/O, so React commits between
 * frames and the hook's `stateRef` — which carries BOTH the reconnect cursor
 * and the terminated check — is current by the time a connection ends. A
 * generator whose `next()` resolves synchronously never yields the event loop,
 * so React's scheduler (a MessageChannel macrotask) never runs, `stateRef`
 * stays pinned at its mount value, and the hook appears to resume from a stale
 * cursor and to miss a termination it has already been told about. Both are
 * artifacts of the double; neither is reachable over a socket.
 */
const realSetTimeout = globalThis.setTimeout;
function tick(): Promise<void> {
  return new Promise((resolve) => {
    realSetTimeout(resolve, 0);
  });
}

/**
 * Drain pending real macrotasks (generator frames, React commits) WITHOUT
 * moving the fake clock.
 *
 * RTL's `waitFor` is deliberately unused in this file. It polls with
 * `setTimeout`, which these tests have faked, and the usual escape hatch —
 * `useFakeTimers({shouldAdvanceTime: true})` — couples the fake clock to real
 * elapsed time. That turns every "has NOT reconnected yet" assertion into a
 * race against machine load: a poll that happens to take a second silently
 * advances the clock past the very delay it was meant to prove, and the test
 * fails only on a busy runner. Ticking by hand keeps the clock under the
 * test's exclusive control, so the ladder's rungs can be asserted exactly.
 *
 * The bound is a count of macrotasks, not a duration, so it is independent of
 * how fast the machine is; a connect needs roughly one tick per frame.
 */
async function settle(ticks = 30): Promise<void> {
  for (let i = 0; i < ticks; i += 1) {
    await act(async () => {
      await tick();
    });
  }
}

/**
 * Move the fake clock by exactly `ms`, draining real work on both sides so an
 * in-flight connect has finished arming its delay before the clock moves, and
 * whatever the clock started has landed before the next assertion.
 */
async function advance(ms: number): Promise<void> {
  await settle();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await settle();
}

/**
 * One scripted connect: yields `frames` in order, then closes — or throws
 * `failWith`, which is what a socket dropping looks like to the consumer.
 */
function connect(frames: SessionStreamFrame[], failWith?: string) {
  return async function* (): AsyncGenerator<SessionStreamFrame> {
    for (const frame of frames) {
      await tick();
      yield frame;
    }
    await tick();
    if (failWith) throw new Error(failWith);
  };
}

/** The options bag the hook handed the transport on its `n`-th connect. */
function callOptions(n: number) {
  return streamSession.mock.calls[n][1] ?? {};
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

beforeEach(() => {
  // `mockReset`, not `mockClear` — an unconsumed `mockImplementationOnce` from
  // a previous test would otherwise be the next test's first connect.
  streamSession.mockReset();
  vi.useFakeTimers();
  // `logApiError` writes to console.error by design; a stream that drops is
  // the expected input of two tests below, not a surprise worth printing.
  vi.spyOn(console, "error").mockImplementation(() => undefined);
});

// ── The pure fold ────────────────────────────────────────────────────────────

describe("SessionStreamState — de-duplication and referential stability", () => {
  it("drops a replayed event and returns the SAME state and events array", () => {
    const a = evt("t1");
    const b = evt("t2");
    const before = SessionStreamState.empty().apply(a).apply(b);

    // The inclusive cursor means a reconnect re-delivers what we already hold.
    const after = before.apply(b);

    // Identity, not just equality: a fresh array would re-run the
    // `useMemo(() => buildTimeline(events), [events])` downstream and rebuild
    // the entire timeline for a frame that added nothing.
    expect(after).toBe(before);
    expect(after.events).toBe(before.events);
    expect(after.events.map((e) => e.ts)).toEqual(["t1", "t2"]);
  });

  it("keeps two events that share a timestamp — the reason the cursor is inclusive", () => {
    const call = evt("t1", "tool_use", { tool: "shell" });
    const result = evt("t1", "tool_result", { ok: true });
    const state = SessionStreamState.empty().apply(call).apply(result);

    // Identity is the body, not the timestamp: trimming the replay EXCLUSIVELY
    // at `t1` would have lost whichever of these the client had not yet seen.
    expect(state.events).toHaveLength(2);
    expect(state.cursor).toBe("t1");
  });

  it("folds a session_state frame without touching the events array", () => {
    const base = SessionStreamState.empty().apply(evt("t1"));
    const running = base.apply(stateFrame({ running: true, status: "running" }));

    expect(running).not.toBe(base);
    expect(running.events).toBe(base.events);
    expect(running.running).toBe(true);

    // A repeat of the state we already hold changes nothing…
    expect(running.apply(stateFrame({ running: true, status: "running" }))).toBe(
      running,
    );
    // …and `stream_end` is the transport talking, never transcript content.
    expect(running.apply(STREAM_END)).toBe(running);
  });

  it("has no cursor on a cold open and tracks the newest event after that", () => {
    expect(SessionStreamState.empty().cursor).toBeUndefined();
    expect(
      SessionStreamState.empty().apply(evt("t1")).apply(evt("t2")).cursor,
    ).toBe("t2");
  });
});

// ── The hook driving the transport ───────────────────────────────────────────

describe("useSessionEvents — reconnect", () => {
  it("absorbs the inclusive backlog replay without double-rendering, and resumes from the last ts", async () => {
    const [a, b, c, d] = [evt("t1"), evt("t2"), evt("t3"), evt("t4")];
    streamSession
      .mockImplementationOnce(connect([a, b, c, stateFrame(), STREAM_END]))
      // The reconnect's replay re-sends `c` (the cursor's own timestamp group)
      // before the one event the client is actually missing.
      .mockImplementationOnce(connect([c, d, stateFrame(), STREAM_END]))
      .mockImplementation(connect([]));

    const { result } = renderHook(() => useSessionEvents("s1"));
    await settle();

    expect(result.current.events.map((e) => e.ts)).toEqual(["t1", "t2", "t3"]);
    expect(streamSession).toHaveBeenCalledTimes(1);
    expect(streamSession.mock.calls[0][0]).toBe("s1");
    // Cold open: no cursor, so the server sends the full transcript.
    expect(callOptions(0).after).toBeUndefined();

    // A clean close waits out the full idle delay, not a moment less.
    await advance(RESUBSCRIBE_MS - 1);
    expect(streamSession).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(streamSession).toHaveBeenCalledTimes(2);

    // The cursor is the newest event held, not a synthetic "now".
    expect(callOptions(1).after).toBe("t3");

    // `c` appears ONCE. A duplicate here is a double-rendered transcript row.
    expect(result.current.events.map((e) => e.ts)).toEqual([
      "t1",
      "t2",
      "t3",
      "t4",
    ]);
  });

  it("recovers from a stream that drops mid-run, keeping what it already delivered", async () => {
    const [a, b, c] = [evt("t1"), evt("t2"), evt("t3")];
    streamSession
      .mockImplementationOnce(connect([a, b], "socket closed"))
      // The retry replays `b` inclusively alongside the new `c`.
      .mockImplementationOnce(connect([b, c, stateFrame(), STREAM_END]))
      .mockImplementation(connect([]));

    const { result } = renderHook(() => useSessionEvents("s1"));
    await settle();

    expect(result.current.error).toBe("socket closed");
    // Frames that landed before the throw are held, not rolled back.
    expect(result.current.events.map((e) => e.ts)).toEqual(["t1", "t2"]);
    expect(streamSession).toHaveBeenCalledTimes(1);

    // A drop is retried promptly — a third of the idle resubscribe delay,
    // because a dropped stream may be leaving live output unreceived.
    await advance(DROP_RETRY_MS - 1);
    expect(streamSession).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(streamSession).toHaveBeenCalledTimes(2);

    expect(callOptions(1).after).toBe("t2");
    // Retained, and not re-appended when the replay hands `t2` back.
    expect(result.current.events.map((e) => e.ts)).toEqual(["t1", "t2", "t3"]);
    // A connect that delivered frames clears the previous failure.
    expect(result.current.error).toBeNull();
  });

  it("climbs the backoff ladder while consecutive connects deliver nothing", async () => {
    streamSession
      .mockImplementationOnce(connect([], "connect refused"))
      .mockImplementationOnce(connect([], "connect refused again"))
      .mockImplementationOnce(connect([evt("t1"), stateFrame(), STREAM_END]))
      .mockImplementation(connect([]));

    const { result } = renderHook(() => useSessionEvents("s1"));
    await settle();

    expect(result.current.error).toBe("connect refused");
    expect(streamSession).toHaveBeenCalledTimes(1);

    // A connect that delivered nothing increments the ladder BEFORE waiting,
    // so it is still holding at the rung a mid-run drop would have retried on
    // — that gap is what separates the two arms.
    await advance(DROP_RETRY_MS);
    expect(streamSession).toHaveBeenCalledTimes(1);
    await advance(CONNECT_FAIL_RETRY_MS - DROP_RETRY_MS);
    expect(streamSession).toHaveBeenCalledTimes(2);
    // Nothing was ever delivered, so the retry is still a cold open.
    expect(callOptions(1).after).toBeUndefined();
    expect(result.current.error).toBe("connect refused again");

    // A second consecutive failure doubles the wait rather than repeating it.
    await advance(CONNECT_FAIL_RETRY_2_MS - 1);
    expect(streamSession).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(streamSession).toHaveBeenCalledTimes(3);

    expect(result.current.events.map((e) => e.ts)).toEqual(["t1"]);
    expect(result.current.error).toBeNull();
  });
});

describe("useSessionEvents — run state off the stream", () => {
  it("threads running/status/doneReason/terminated/recoverable out of a session_state frame", async () => {
    streamSession.mockImplementation(
      connect([
        stateFrame({
          running: true,
          status: "running",
          done_reason: "",
          recoverable: true,
        }),
      ]),
    );

    const { result } = renderHook(() => useSessionEvents("s1"));
    await settle();

    expect(result.current.running).toBe(true);
    expect(result.current.status).toBe("running");
    expect(result.current.doneReason).toBe("");
    expect(result.current.terminated).toBe(false);
    expect(result.current.recoverable).toBe(true);
    // The run state rode the stream — there is no companion fetch carrying it.
    expect(result.current.events).toHaveLength(0);
  });

  it("stops reconnecting once a session reports terminated", async () => {
    streamSession.mockImplementation(
      connect([
        stateFrame({
          status: "terminated",
          done_reason: "",
          terminated: true,
          terminated_at: "t0",
        }),
        evt("t1", "session_terminated", {}),
        STREAM_END,
      ]),
    );

    const { result } = renderHook(() => useSessionEvents("s1"));
    await settle();

    expect(result.current.terminated).toBe(true);
    expect(streamSession).toHaveBeenCalledTimes(1);

    // Termination is absorbing — the runtime ships no un-terminate primitive,
    // so re-subscribing forever would be pure waste. Well past the resubscribe
    // delay and past the backoff ceiling several times over.
    await advance(RESUBSCRIBE_MS * 20);
    expect(streamSession).toHaveBeenCalledTimes(1);
  });
});

describe("useSessionEvents — unmount", () => {
  it("aborts the live stream and never reconnects", async () => {
    streamSession.mockImplementation(
      connect([evt("t1"), stateFrame(), STREAM_END]),
    );

    const { result, unmount } = renderHook(() => useSessionEvents("s1"));
    await settle();

    expect(result.current.events).toHaveLength(1);
    const { signal } = callOptions(0);
    expect(signal?.aborted).toBe(false);

    unmount();

    // The transport is told to stop mid-read rather than being left to finish
    // — the reason this is built on `fetch`/`AbortSignal` and not EventSource.
    expect(signal?.aborted).toBe(true);

    await advance(RESUBSCRIBE_MS * 5);
    expect(streamSession).toHaveBeenCalledTimes(1);
  });
});
