/**
 * The session event transport: one SSE stream per open session.
 *
 * Built on the shared `sseStream` primitive rather than native `EventSource`,
 * for the reasons `api/sse.ts` already documents (abortable on unmount, no
 * per-feature frame parser). `EventSource` cannot be aborted mid-read and
 * reconnects on its own schedule rather than on ours, so it doesn't fit this
 * stream's cursor-replay contract.
 *
 * Wire shape — three frame kinds arrive on one stream:
 *
 * - a transcript `EventRecord` (`{ts, type, payload}`),
 * - `session_state`, the authoritative run state, emitted right after the
 *   replay leg and again just before the stream closes,
 * - `stream_end`, the terminal marker.
 *
 * The server replays the backlog on every connect. `after` trims that replay
 * to the events a reconnecting client does not already hold, and the bound is
 * INCLUSIVE — several events can share a timestamp, so the cursor's own
 * timestamp group is re-sent and the consumer de-duplicates by content.
 */

import { API_BASE, API_KEY } from "./client";
import { sseStream } from "./sse";
import { EventRecord } from "../types";

/** Authoritative run state, pushed on the stream so it needs no companion fetch. */
export type SessionStateFrame = {
  type: "session_state";
  running: boolean;
  status: string;
  done_reason: string;
  title: string;
  recoverable: boolean;
  terminated: boolean;
  terminated_at: string | null;
};

/** Terminal marker: the run finished and the server is closing the stream. */
export type StreamEndFrame = { type: "stream_end" };

export type SessionStreamFrame = SessionStateFrame | StreamEndFrame | EventRecord;

export function isSessionStateFrame(
  frame: SessionStreamFrame,
): frame is SessionStateFrame {
  return frame.type === "session_state";
}

export function isStreamEndFrame(
  frame: SessionStreamFrame,
): frame is StreamEndFrame {
  return frame.type === "stream_end";
}

/**
 * A transcript event, as opposed to one of the two control frames. Control
 * frames carry no `ts` and must never reach the timeline builder, which treats
 * every element it is given as a transcript row.
 */
export function isTranscriptEvent(
  frame: SessionStreamFrame,
): frame is EventRecord {
  return !isSessionStateFrame(frame) && !isStreamEndFrame(frame);
}

/**
 * Open the session stream. Yields until the server closes it (`stream_end`,
 * or an idle timeout) or `signal` aborts.
 */
export function streamSession(
  sessionId: string,
  options: { after?: string; signal?: AbortSignal } = {},
): AsyncGenerator<SessionStreamFrame> {
  const cursor = options.after
    ? `?after=${encodeURIComponent(options.after)}`
    : "";
  return sseStream<SessionStreamFrame>(
    `/api/sessions/${encodeURIComponent(sessionId)}/stream${cursor}`,
    { base: API_BASE, apiKey: API_KEY, signal: options.signal },
  );
}
