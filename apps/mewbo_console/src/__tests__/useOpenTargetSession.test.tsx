/**
 * useOpenTargetSession — the shared "open a session about this thing" mutation
 * both the wiki project card and the app detail header wire against a
 * different get-or-create endpoint. Pinned at the hook seam, mirroring
 * `useForkSession`/`useRecoverSession`'s test harness
 * (`src/__tests__/wiki/recoveryFixes.test.tsx`).
 *
 * vitest runs WITHOUT globals → explicit cleanup.
 */
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useOpenTargetSession } from "@/hooks/useOpenTargetSession";

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));
import { toast } from "sonner";
const toastError = vi.mocked(toast.error);

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
});

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const loc = memoryLocation({ path: "/wiki", record: true });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>{children}</Router>
    </QueryClientProvider>
  );
  return { wrapper, loc };
}

describe("useOpenTargetSession", () => {
  it("navigates to /s/<id> on a successful mint", async () => {
    const open = vi.fn().mockResolvedValue("sess-123");
    const { wrapper, loc } = makeWrapper();
    const { result } = renderHook(() => useOpenTargetSession(open, "Couldn't open"), { wrapper });

    await act(async () => {
      await result.current.mutateAsync();
    });

    expect(open).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(loc.history.at(-1)).toBe("/s/sess-123"));
  });

  it("URL-encodes a session id carrying reserved characters", async () => {
    const open = vi.fn().mockResolvedValue("sess/weird id");
    const { wrapper, loc } = makeWrapper();
    const { result } = renderHook(() => useOpenTargetSession(open, "Couldn't open"), { wrapper });

    await act(async () => {
      await result.current.mutateAsync();
    });

    await waitFor(() =>
      expect(loc.history.at(-1)).toBe(`/s/${encodeURIComponent("sess/weird id")}`),
    );
  });

  it("toasts the error label + reason on a failed mint, without navigating", async () => {
    const open = vi.fn().mockRejectedValue(new Error("no such project"));
    const { wrapper, loc } = makeWrapper();
    const { result } = renderHook(
      () => useOpenTargetSession(open, "Couldn't open the wiki session"),
      { wrapper },
    );

    await act(async () => {
      await result.current.mutateAsync().catch(() => undefined);
    });

    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1));
    expect(toastError.mock.calls[0][0]).toMatch(/Couldn't open the wiki session.*no such project/);
    expect(loc.history.at(-1)).toBe("/wiki");
  });

  it("does not navigate a stale caller once it has unmounted before the mint resolves", async () => {
    // The regression this pins: onSuccess/onError must be bound at the
    // mutate()/mutateAsync() CALL site (query-core gates those on
    // hasListeners(), false post-unmount) rather than in the useMutation
    // hook options (which fire unconditionally from Mutation.execute()
    // regardless of whether anything is still mounted to care).
    let resolveOpen!: (sessionId: string) => void;
    const open = vi.fn(
      () => new Promise<string>((resolve) => { resolveOpen = resolve; }),
    );
    const { wrapper, loc } = makeWrapper();
    const { result, unmount } = renderHook(
      () => useOpenTargetSession(open, "Couldn't open"),
      { wrapper },
    );

    await act(async () => {
      result.current.mutate();
      // Mutation.execute() awaits its (unset) onMutate before reaching the
      // retryer that actually calls `open()` — even a plain `await undefined`
      // defers to a microtask, so one tick has to pass before `resolveOpen`
      // is assigned.
      await Promise.resolve();
    });
    expect(open).toHaveBeenCalledTimes(1);
    unmount();

    await act(async () => {
      resolveOpen("sess-should-not-navigate");
      // Flush the microtasks the mutation's internal promise chain and
      // query-core's batched notify run on.
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(loc.history.at(-1)).toBe("/wiki");
    expect(toastError).not.toHaveBeenCalled();
  });

  it("fires exactly one mint on a rapid double-click with no yield in between", async () => {
    // Both real call sites additionally disable their button on
    // `openSession.isPending`, but that value only flips once React
    // re-renders off query-core's `setTimeout(0)`-scheduled notification — a
    // macrotask two back-to-back clicks (no `await` between them) both run
    // before. The hook's own synchronous `inFlight` ref is what actually
    // holds here, independent of when the button's `disabled` attribute
    // catches up.
    const open = vi.fn(() => new Promise<string>((resolve) => setTimeout(() => resolve("sess-1"), 0)));
    const { wrapper } = makeWrapper();

    function Harness() {
      const { mutate, isPending } = useOpenTargetSession(open, "Couldn't open");
      return (
        <button disabled={isPending} onClick={() => mutate()}>
          open
        </button>
      );
    }

    render(<Harness />, { wrapper });
    const button = screen.getByRole("button");

    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(open).toHaveBeenCalledTimes(1));
  });
});
