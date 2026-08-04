/**
 * IdeLoader — the create/poll/redirect state machine's error surface.
 *
 * Two contracts pinned here. First: a 409 from the create POST is a TERMINAL
 * failure whose server-authored `message` renders verbatim and immediately —
 * never retried into the poll loop, never waiting out the 60s deadline. This
 * is the channel the mount resolver's actionable 409s (e.g. a checkout
 * directory missing on the host) depend on; a regression to a generic
 * fallback would drop that diagnostic with nothing in the suite failing.
 * Second: the pre-existing poll-timeout path still ends in the same error
 * phase with its own message once a create succeeds but the instance never
 * reaches `ready`.
 *
 * Only `apiFetch`'s I/O boundary (`global.fetch`) is stubbed — `createIde`/
 * `getIde` run for real, matching this suite's "stub only the I/O boundary"
 * convention. Fake timers stand in for both the 1s poll interval and the 60s
 * deadline so the test asserts on the deadline passing, not on wall clock
 * time; `settle`/`advance` mirror the pattern in `useSessionEvents.test.tsx`.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdeLoader } from "@/components/IdeLoader";
import type { IdeInstance } from "@/api/ide";

const realSetTimeout = globalThis.setTimeout;
function tick(): Promise<void> {
  return new Promise((resolve) => {
    realSetTimeout(resolve, 0);
  });
}

/** Drain pending real macrotasks without moving the fake clock. */
async function settle(ticks = 10): Promise<void> {
  for (let i = 0; i < ticks; i += 1) {
    await act(async () => {
      await tick();
    });
  }
}

/** Move the fake clock by `ms`, draining real work on both sides. */
async function advance(ms: number): Promise<void> {
  await settle();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await settle();
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response;
}

function instance(overrides: Partial<IdeInstance> = {}): IdeInstance {
  return {
    session_id: "sess-1",
    status: "pending",
    url: "",
    project_name: "acme/beacon",
    project_path: "/srv/checkouts/acme-beacon",
    created_at: "2026-01-01T00:00:00Z",
    expires_at: "2026-01-01T01:00:00Z",
    max_deadline: "2026-01-01T04:00:00Z",
    remaining_seconds: 3600,
    extensions: 0,
    ...overrides,
  };
}

// Typed off a factory so the spy's own signature is inferred. Annotating it
// `MockInstance<unknown[], unknown>` does not typecheck: a mock's parameters are
// contravariant, so the wider list is not a supertype of fetch's.
const spyOnFetch = () => vi.spyOn(global, "fetch");
let fetchSpy: ReturnType<typeof spyOnFetch>;

beforeEach(() => {
  vi.useFakeTimers();
  fetchSpy = spyOnFetch();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("IdeLoader — 409 from createIde", () => {
  it("renders the server's message verbatim and never enters the poll loop", async () => {
    const message =
      "The project directory /srv/checkouts/acme-beacon does not exist on this host.";
    fetchSpy.mockResolvedValueOnce(jsonResponse(409, { message }));

    render(<IdeLoader sessionId="sess-1" />);
    await settle();

    expect(screen.getByText("Failed to open Web IDE")).toBeInTheDocument();
    expect(screen.getByText(message)).toBeInTheDocument();

    // Terminal immediately: only the create POST fired, and it was a POST.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [, init] = fetchSpy.mock.calls[0];
    expect((init as RequestInit | undefined)?.method).toBe("POST");

    // Not retried into the poll loop: advancing past the full 60s deadline
    // must not trigger a status GET — the failure surfaced immediately, not
    // after waiting out the timeout.
    await advance(60_000);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
});

describe("IdeLoader — poll timeout", () => {
  it("ends in the error phase with the timeout message once the deadline passes", async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResponse(200, instance({ status: "pending", password: "secret" })),
    );
    // Every subsequent GET reports "starting" — the instance never reaches
    // ready, so the loop must run out the clock rather than redirect.
    fetchSpy.mockResolvedValue(jsonResponse(200, instance({ status: "starting" })));

    render(<IdeLoader sessionId="sess-1" />);
    await settle();

    await advance(60_000);

    expect(
      screen.getByText("Timed out waiting for the IDE container to become ready."),
    ).toBeInTheDocument();
    expect(screen.getByText("Failed to open Web IDE")).toBeInTheDocument();

    // The create POST plus at least one status GET from the poll loop.
    expect(fetchSpy.mock.calls.length).toBeGreaterThan(1);
    const getCalls = fetchSpy.mock.calls.filter(
      ([, init]) => (init as RequestInit | undefined)?.method === undefined,
    );
    expect(getCalls.length).toBeGreaterThan(0);
  });
});
