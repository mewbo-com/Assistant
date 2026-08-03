/**
 * useRebindProject — the durable project rebind mutation.
 *
 * `PUT /api/sessions/<id>/project` is the one sanctioned path to change a
 * purpose-bound session's project (the per-turn override is refused
 * server-side — see `SessionSpec.field_editable`). It only ever BINDS — a
 * blank/null name is a 400, not an unbind, since clearing a project would
 * null the session's `cwd` (see the route's own docstring). The mutation must
 * call the route with the exact args given, and its `onSuccess` must write
 * the fresh spec projection straight onto the `session-spec` cache entry so
 * `useSessionSpec(sessionId)` reflects the new binding without a refetch.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRebindProject } from "@/hooks/useRebindProject";
import type { SessionSpecResponse } from "@/types";

vi.mock("@/api/client", () => ({ rebindSessionProject: vi.fn() }));
import * as apiClient from "@/api/client";
const rebindSessionProject = vi.mocked(apiClient.rebindSessionProject);

const RESPONSE: SessionSpecResponse = {
  session_id: "s1",
  spec: {
    origin: "user",
    surface: "console",
    purpose_bound: false,
    project: "beacon",
    slug: null,
    cwd: "/srv/projects/beacon",
    model: null,
    fallback_models: null,
    allowed_tools: null,
    strict_tool_scope: false,
    capabilities: null,
    skill_instructions_present: false,
    session_step_budget: null,
    mode: null,
  },
  editable: { project: true, model: true, fallback_models: true, mode: true },
  source: "spec",
};

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { qc, wrapper };
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
});

describe("useRebindProject", () => {
  it("PUTs the exact session id and project", async () => {
    rebindSessionProject.mockResolvedValue(RESPONSE);
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useRebindProject("s1"), { wrapper });

    await act(async () => {
      await result.current.mutateAsync("beacon");
    });

    expect(rebindSessionProject).toHaveBeenCalledWith("s1", "beacon");
  });

  it("writes the response straight onto the session-spec cache entry", async () => {
    rebindSessionProject.mockResolvedValue(RESPONSE);
    const { qc, wrapper } = makeWrapper();
    const { result } = renderHook(() => useRebindProject("s1"), { wrapper });

    await act(async () => {
      await result.current.mutateAsync("beacon");
    });

    expect(qc.getQueryData(["session-spec", "s1"])).toEqual(RESPONSE);
  });

  it("rejects without calling the route when no session id is bound yet", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useRebindProject(undefined), { wrapper });

    await act(async () => {
      await expect(result.current.mutateAsync("beacon")).rejects.toThrow();
    });
    expect(rebindSessionProject).not.toHaveBeenCalled();
  });
});
