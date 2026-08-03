/**
 * `useSessions` — referential stability of the two session arrays, and the
 * one optimistic-write rule that stops the cache lying about what it holds.
 *
 * `sessions` and `archivedSessions` are passed to `MewboRuntimeProvider`,
 * which memoises them into assistant-ui's `useExternalStoreRuntime`. That
 * runtime's thread-list core compares the incoming `threads` /
 * `archivedThreads` by REFERENCE (`external-store-thread-list-runtime-core`:
 * a bare `===`, no shallow compare) and calls `_notifySubscribers()` whenever
 * they differ. Its subscribers read through `useSyncExternalStore`, whose
 * `getState` mints a fresh object each call, and React re-checks that snapshot
 * after every commit — so one unstable array upstream becomes an unbounded
 * render loop and React tears the tree down with:
 *
 *     Maximum update depth exceeded   (minified: React error #185)
 *
 * The `archived` query is declared `enabled: false`, so `archived.data` is
 * permanently `undefined` and the fallback below is the value that is ALWAYS
 * read. `useExternalStoreRuntime`'s own `setAdapter` effect carries no
 * dependency array, so it re-runs after every commit.
 *
 * ⚠️ Scope, measured rather than assumed: pinning these arrays did NOT by
 * itself stop the crash this file was born from. The loop that actually took
 * the console down came from the title-sync effect in `SessionDetailView`
 * (see the second describe block, and that effect's own comment). These
 * assertions stand on their own — an unstable array here is a real hazard the
 * vendor amplifies — but do not read them as the cure.
 *
 * Both arrays are pinned. `sessions` is stable today only because its query is
 * enabled and TanStack hands back a cached reference; that is a property of
 * the query's configuration, not of this code, so it gets a test too.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/client", () => ({
  archiveSession: vi.fn(),
  createSession: vi.fn(),
  listSessions: vi.fn(async () => []),
  pinSession: vi.fn(),
  regenerateTitle: vi.fn(),
  unarchiveSession: vi.fn(),
  unpinSession: vi.fn(),
  updateSessionTitle: vi.fn(async (id: string, title: string) => ({
    session_id: id,
    title,
  })),
}));

import { listSessions } from "@/api/client";
import { useSessions } from "@/hooks/useSessions";
import type { SessionSummary } from "@/types";

afterEach(cleanup);

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return React.createElement(QueryClientProvider, { client }, children);
}

describe("useSessions reference stability", () => {
  it("returns the SAME archivedSessions array across renders while the query is disabled", () => {
    const { result, rerender } = renderHook(() => useSessions(), { wrapper });

    const first = result.current.archivedSessions;
    rerender();
    const second = result.current.archivedSessions;
    rerender();
    const third = result.current.archivedSessions;

    // Object.is identity, not deep equality — reference is what the vendor
    // store compares, so `toEqual` here would pass against the bug.
    expect(second).toBe(first);
    expect(third).toBe(first);
  });

  it("returns the SAME sessions array across renders before data arrives", () => {
    const { result, rerender } = renderHook(() => useSessions(), { wrapper });

    const first = result.current.sessions;
    rerender();

    expect(result.current.sessions).toBe(first);
  });
});

/**
 * The optimistic cache patches must be idempotent, because a repeated write is
 * what turned a title sync into an unbounded render loop ("Maximum update depth
 * exceeded") on any directly-visited session page.
 */
describe("useSessions optimistic patches are idempotent", () => {
  const ROW = {
    session_id: "s1",
    title: "Original",
  } as unknown as SessionSummary;

  beforeEach(() => {
    vi.mocked(listSessions).mockResolvedValue([ROW] as never);
  });

  async function mounted() {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrap = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client }, children);
    const hook = renderHook(() => useSessions(), { wrapper: wrap });
    await waitFor(() =>
      expect(client.getQueryData(["sessions", "active"])).toBeDefined(),
    );
    return { client, hook };
  }

  it("does NOT fabricate an empty list when nothing is cached yet", async () => {
    const { client, hook } = await mounted();
    // The archived query is `enabled: false`, so it never loads.
    expect(client.getQueryData(["sessions", "archived"])).toBeUndefined();

    await act(async () => {
      await hook.result.current.updateTitle("s1", "New title");
    });

    // The old `prev?.map(...) ?? []` published "zero sessions" here, which is
    // what stopped a detail view from ever finding its own row.
    expect(client.getQueryData(["sessions", "archived"])).toBeUndefined();
  });


  it("still applies a genuine change", async () => {
    const { client, hook } = await mounted();
    const seed = client.getQueryData(["sessions", "active"]);

    await act(async () => {
      await hook.result.current.updateTitle("s1", "Renamed");
    });

    const next = client.getQueryData<SessionSummary[]>(["sessions", "active"]);
    expect(next).not.toBe(seed);
    expect(next?.[0]?.title).toBe("Renamed");
  });
});
