/**
 * AppDetailHeader — the "Open a session about this app" action.
 *
 * Get-or-create against the app's OWN maintainer-session endpoint, replacing
 * the old static `maintainer_session_id ?? owner_session_id` jump (which
 * pointed a first-ever click at the build transcript, never a maintainer
 * conversation).
 *
 * vitest runs WITHOUT globals → explicit cleanup.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppDetailHeader } from "./AppDetailHeader";
import type { AppSpec } from "../../../types/apps";

vi.mock("../../../api/apps", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../api/apps")>();
  return { ...actual, openAppSession: vi.fn() };
});
import * as appsApi from "../../../api/apps";
const openAppSession = vi.mocked(appsApi.openAppSession);

vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
import { toast } from "sonner";
const toastError = vi.mocked(toast.error);

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
});

function baseSpec(over: Partial<AppSpec> = {}): AppSpec {
  return {
    app_id: "app-1",
    title: "Meeting Digest",
    summary: "Summarizes meetings",
    icon: "📝",
    owner_session_id: "s-owner",
    workspace_ref: { kind: "own", key: "app-1" },
    frontend: { entrypoint: "app.py", files: { "app.py": "" }, requirements: [] },
    collections: [],
    pipelines: [],
    policies: { on_pipeline_failure: "notify", retention_days: null, max_docs_per_collection: 1000 },
    maintainer_session_id: null,
    version: 1,
    status: "live",
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...over,
  };
}

function renderHeader(spec: AppSpec, onBack = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const loc = memoryLocation({ path: "/apps/app-1", record: true });
  render(
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>
        <AppDetailHeader spec={spec} onBack={onBack} />
      </Router>
    </QueryClientProvider>,
  );
  return { onBack, loc };
}

describe("AppDetailHeader — open-session action", () => {
  it("mints the maintainer session (no maintainer_session_id yet) and navigates to it", async () => {
    openAppSession.mockResolvedValue({ session_id: "sess-maint", created: true });
    const { loc } = renderHeader(baseSpec({ maintainer_session_id: null }));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Open a session about this app" }));
    });

    expect(openAppSession).toHaveBeenCalledWith("app-1");
    await waitFor(() => expect(loc.history.at(-1)).toBe("/s/sess-maint"));
  });

  it("reuses the existing maintainer session on a second click (get-or-create)", async () => {
    openAppSession.mockResolvedValue({ session_id: "sess-maint", created: false });
    const { loc } = renderHeader(baseSpec({ maintainer_session_id: "sess-maint" }));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Open a session about this app" }));
    });

    await waitFor(() => expect(loc.history.at(-1)).toBe("/s/sess-maint"));
  });

  it("toasts on a failed mint instead of navigating", async () => {
    openAppSession.mockRejectedValue(new Error("app not found"));
    const { loc } = renderHeader(baseSpec());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Open a session about this app" }));
    });

    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1));
    expect(toastError.mock.calls[0][0]).toMatch(/app not found/);
    expect(loc.history.at(-1)).toBe("/apps/app-1");
  });
});
