import { useCallback, useEffect, useRef, useState } from "react";
import {
  SessionStreamFrame,
  isSessionStateFrame,
  isStreamEndFrame,
  streamSession,
} from "../api/sessionStream";
import { EventRecord } from "../types";
import { logApiError } from "../utils/errors";

/**
 * Delay before re-subscribing after the server closes a healthy stream. The
 * server closes the stream as soon as a session stops running, so this is what
 * makes an off-tab start (CLI, Aura, a trigger, or a plan/question dead-end)
 * surface without a manual refresh. It carries no transcript: the `after`
 * cursor makes the reconnect's replay empty, so the cost is one request with
 * an approximately empty body, not the re-download a naive reconnect pays.
 */
const RESUBSCRIBE_MS = 3_000;
/** First backoff step after a failed connect; doubles up to the ceiling. */
const BACKOFF_MIN_MS = 1_000;
const BACKOFF_MAX_MS = 15_000;

function backoffMs(attempt: number): number {
  return Math.min(BACKOFF_MIN_MS * 2 ** attempt, BACKOFF_MAX_MS);
}

/** Resolve after `ms`, or as soon as `signal` aborts. */
function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const id = setTimeout(finish, ms);
    signal.addEventListener("abort", finish, { once: true });
    function finish() {
      clearTimeout(id);
      signal.removeEventListener("abort", finish);
      resolve();
    }
  });
}

/**
 * The accumulated view of one session's stream.
 *
 * Immutable and pure — it owns the two rules that keep a reconnect honest, and
 * neither can live in the transport:
 *
 * 1. **De-duplication by content key.** The server replays a backlog on every
 *    connect, and the replay cursor is deliberately inclusive, so a reconnect
 *    re-delivers at least one timestamp group. `buildTimeline` has no duplicate
 *    tolerance of its own — it renders whatever row it is handed — so an
 *    un-deduplicated reconnect double-renders the transcript.
 * 2. **Referential stability.** `apply` returns `this` unchanged when a frame
 *    adds nothing, so the `events` array keeps its identity and the
 *    `useMemo(() => buildTimeline(events), [events])` downstream does not
 *    rebuild the whole timeline on every duplicate or heartbeat.
 */
export class SessionStreamState {
  private constructor(
    readonly events: EventRecord[],
    readonly running: boolean,
    readonly status: string | undefined,
    /** The server's own title projection — what lets the detail view name the
     *  session without waiting on the sessions listing. */
    readonly title: string | undefined,
    readonly doneReason: string | undefined,
    readonly terminated: boolean,
    readonly recoverable: boolean | undefined,
    private readonly seen: ReadonlySet<string>,
  ) {}

  static empty(): SessionStreamState {
    return new SessionStreamState(
      [],
      false,
      undefined,
      undefined,
      undefined,
      false,
      undefined,
      new Set(),
    );
  }

  /** Content key: there is no event id on the wire, so identity is the body. */
  static keyOf(event: EventRecord): string {
    const payload =
      event.payload && typeof event.payload === "object"
        ? JSON.stringify(event.payload)
        : String(event.payload ?? "");
    return `${event.ts}|${event.type}|${payload}`;
  }

  /**
   * Where to resume from. The newest timestamp this state holds; `undefined`
   * on a cold open, which asks the server for the full transcript.
   */
  get cursor(): string | undefined {
    return this.events.length ? this.events[this.events.length - 1].ts : undefined;
  }

  /** Fold one frame in, returning `this` when it changes nothing. */
  apply(frame: SessionStreamFrame): SessionStreamState {
    if (isSessionStateFrame(frame)) {
      if (
        frame.running === this.running &&
        frame.status === this.status &&
        frame.title === this.title &&
        frame.done_reason === this.doneReason &&
        frame.terminated === this.terminated &&
        frame.recoverable === this.recoverable
      ) {
        return this;
      }
      return new SessionStreamState(
        this.events,
        frame.running,
        frame.status,
        frame.title,
        frame.done_reason,
        frame.terminated,
        frame.recoverable,
        this.seen,
      );
    }
    // `stream_end` says the server is closing the connection, not that the
    // session reached a terminal state — the reconnect loop owns what happens
    // next, and the run state it needs came from the `session_state` frame the
    // server emits immediately before this one.
    if (isStreamEndFrame(frame)) return this;

    const key = SessionStreamState.keyOf(frame);
    if (this.seen.has(key)) return this;
    const seen = new Set(this.seen);
    seen.add(key);
    return new SessionStreamState(
      [...this.events, frame],
      this.running,
      this.status,
      this.title,
      this.doneReason,
      this.terminated,
      this.recoverable,
      seen,
    );
  }
}

/**
 * Subscribe to a session's event stream.
 *
 * One transport, shared with every other surface: the stream pushes transcript
 * events as they are appended, so rendering is not quantised to a poll clock.
 * The connection is re-established when the server closes it, resuming from
 * the last timestamp held so a reconnect does not re-download the transcript.
 */
export function useSessionEvents(sessionId?: string) {
  const [state, setState] = useState<SessionStreamState>(SessionStreamState.empty);
  const [error, setError] = useState<string | null>(null);
  // Bumped to tear the current connection down and open a fresh one.
  const [epoch, setEpoch] = useState(0);
  // The loop reads the live cursor without re-subscribing on every event, so
  // the connection's identity stays tied to the session, not to its contents.
  const stateRef = useRef<SessionStreamState>(state);

  // Declared BEFORE the subscribe effect on purpose: effects run in order, and
  // a new session must reset the cursor before the first connect reads it, or
  // that connect would resume from the previous session's tail.
  useEffect(() => {
    const empty = SessionStreamState.empty();
    stateRef.current = empty;
    setState(empty);
    setError(null);
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    const controller = new AbortController();
    const { signal } = controller;
    let attempt = 0;

    const consume = async () => {
      while (!signal.aborted) {
        let delivered = false;
        let failed = false;
        try {
          const frames = streamSession(sessionId, {
            after: stateRef.current.cursor,
            signal,
          });
          for await (const frame of frames) {
            if (signal.aborted) return;
            delivered = true;
            setState((prev) => {
              const next = prev.apply(frame);
              stateRef.current = next;
              return next;
            });
          }
          if (delivered) setError(null);
        } catch (err) {
          if (signal.aborted) return;
          failed = true;
          setError(logApiError("streamSession", err));
        }
        if (signal.aborted) return;
        // Termination is absorbing — the runtime ships no un-terminate
        // primitive — so a terminated session can never start running again
        // and re-subscribing to it forever would be pure waste.
        if (stateRef.current.terminated) return;
        // A connection that delivered frames proved the endpoint healthy, so
        // it earns a fresh backoff ladder however it ended — otherwise a
        // long-lived stream would inherit stale backoff from an unrelated
        // failure hours earlier.
        attempt = delivered ? 0 : attempt + 1;
        // The two ways a connection ends are not the same event. A clean close
        // means the server had nothing more to send, so waiting is correct. A
        // DROP means the run may still be producing output nobody is
        // receiving, so retry promptly rather than sitting out the idle delay.
        await delay(
          failed ? backoffMs(attempt) : RESUBSCRIBE_MS,
          signal,
        );
      }
    };

    void consume();
    return () => controller.abort();
  }, [sessionId, epoch]);

  /** Drop everything and re-open — for a `/recover` that truncated the transcript. */
  const reset = useCallback(() => {
    const empty = SessionStreamState.empty();
    stateRef.current = empty;
    setState(empty);
    setError(null);
    setEpoch((n) => n + 1);
  }, []);

  /** Re-open now instead of waiting out the re-subscribe delay. */
  const resume = useCallback(() => {
    if (!sessionId) return;
    setEpoch((n) => n + 1);
  }, [sessionId]);

  return {
    events: state.events,
    running: state.running,
    status: state.status,
    title: state.title,
    doneReason: state.doneReason,
    terminated: state.terminated,
    recoverable: state.recoverable,
    error,
    reset,
    resume,
  };
}
