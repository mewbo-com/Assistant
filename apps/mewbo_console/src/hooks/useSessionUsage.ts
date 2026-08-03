import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { fetchUsage } from "../api/client";
import { EventRecord, SessionUsage } from "../types";

/**
 * The event types that move a session's token totals. Usage is recomputed
 * server-side from the transcript, so it can only change when one of these is
 * appended — which is what lets the stream drive the refetch instead of a
 * clock.
 */
const USAGE_BEARING_EVENTS: ReadonlySet<string> = new Set([
  "llm_call_end",
  "completion",
]);

/**
 * Fetch the session's faceted token usage (root agent vs sub-agents plus
 * compaction stats).
 *
 * Refetched when the session's own event stream delivers an event that can
 * have changed the totals, rather than on an interval: the numbers then update
 * the moment a call settles instead of up to a poll period later, and an idle
 * session issues no requests at all. `events` is the accumulated transcript
 * from `useSessionEvents`.
 */
export function useSessionUsage(
  sessionId?: string,
  events: EventRecord[] = [],
): {
  usage: SessionUsage | null;
  isLoading: boolean;
  error: unknown;
} {
  const revision = useMemo(
    () => events.reduce((n, e) => (USAGE_BEARING_EVENTS.has(e.type) ? n + 1 : n), 0),
    [events],
  );

  const q = useQuery<SessionUsage>({
    queryKey: ["session-usage", sessionId ?? "", revision],
    enabled: Boolean(sessionId),
    queryFn: () => {
      if (!sessionId) {
        // Unreachable — `enabled` gates invocation — but TypeScript needs
        // the narrowing and ESLint rejects the non-null assertion.
        return Promise.reject(new Error("no session id"));
      }
      return fetchUsage(sessionId);
    },
    // The revision is part of the key, so a new one is a cache miss. Hold the
    // previous numbers while it resolves rather than blanking the readout.
    placeholderData: (prev) => prev,
    staleTime: 2000,
  });

  return {
    usage: q.data ?? null,
    isLoading: q.isLoading,
    error: q.error,
  };
}
