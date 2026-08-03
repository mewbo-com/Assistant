/**
 * SessionHeader — Web IDE capsule visibility.
 *
 * The capsule mounts whenever the session names something the backend's
 * session→IDE resolver can turn into a checkout: a configured project, a wiki
 * project the session indexes/maintains (`slug`), or a Mewbo App it
 * builds/maintains (`app_id`). Precedence is per-field and deliberately NOT
 * uniform (see `hasIdeMountTarget`'s doc in SessionHeader.tsx): `project`
 * reads the LIVE context (`context` prop, mirrors `getLastContext` — the
 * latest context event's payload, verbatim, never merged), while `slug`/
 * `app_id` read the session's MERGED snapshot (`session.context`, built
 * server-side by folding every context event forward) because the wiki/app
 * harness stamps them once at creation and never restates them per turn.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionHeader } from "@/components/SessionHeader";
import type { SessionContext, SessionSummary } from "@/types";

vi.mock("@/api/client", () => ({
  listProjects: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/hooks/useWebIdeEnabled", () => ({
  useWebIdeEnabled: vi.fn(),
}));
import { useWebIdeEnabled } from "@/hooks/useWebIdeEnabled";
const mockWebIdeEnabled = vi.mocked(useWebIdeEnabled);

vi.mock("@/hooks/useIdeStatus", () => ({
  useIdeStatus: vi.fn(),
}));
import { useIdeStatus } from "@/hooks/useIdeStatus";
const mockIdeStatus = vi.mocked(useIdeStatus);

/**
 * `sessionContext` seeds `SessionSummary.context` — the server's merged,
 * cross-turn snapshot. `liveContext` seeds the `context` prop — the
 * verbatim-latest live read. They default to the same value so the simple
 * single-turn cases don't have to spell both; tests that need to pin the
 * turn-2+ scenario (live context has dropped a key the snapshot still
 * carries) pass them separately.
 */
function mount(
  sessionContext: SessionContext | undefined,
  liveContext: SessionContext | undefined = sessionContext,
) {
  const session: SessionSummary = {
    session_id: "sess-1",
    title: "A session",
    status: "completed",
    origin: "user",
    context: sessionContext,
  };
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { hook } = memoryLocation({ path: "/" });
  return render(
    <QueryClientProvider client={qc}>
      <Router hook={hook}>
        <SessionHeader
          session={session}
          context={liveContext}
          usage={null}
          isTerminated={false}
          onBack={() => undefined}
        />
      </Router>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  mockWebIdeEnabled.mockReturnValue(true);
  mockIdeStatus.mockReturnValue({
    instance: null,
    refresh: vi.fn(),
    setInstance: vi.fn(),
  });
});

describe("SessionHeader — Web IDE capsule predicate", () => {
  it("shows the capsule for a session bound to a configured project", () => {
    mount({ project: "my-project" });
    expect(screen.getByText("Open in Coder")).toBeInTheDocument();
  });

  it("shows the capsule for a wiki maintainer session (slug, no project)", () => {
    mount({ slug: "github.com/acme/beacon" });
    expect(screen.getByText("Open in Coder")).toBeInTheDocument();
  });

  it("shows the capsule for an app builder/maintainer session (app_id, no project)", () => {
    mount({ app_id: "app-42" });
    expect(screen.getByText("Open in Coder")).toBeInTheDocument();
  });

  it("hides the capsule when the context carries none of the three keys", () => {
    mount({ mcp_tools: [] });
    expect(screen.queryByText("Open in Coder")).not.toBeInTheDocument();
  });

  it("hides the capsule when the context is entirely absent", () => {
    mount(undefined);
    expect(screen.queryByText("Open in Coder")).not.toBeInTheDocument();
  });

  it("hides the capsule when the Web IDE feature flag is off, even with a mount target", () => {
    mockWebIdeEnabled.mockReturnValue(false);
    mount({ project: "my-project" });
    expect(screen.queryByText("Open in Coder")).not.toBeInTheDocument();
  });

  // The regression a browser pass caught and no unit test previously covered:
  // a real app session's FIRST context event carries `app_id`, but the
  // wiki/app harness never restates it, so `getLastContext` (the live read)
  // returns it only on turn one. Every earlier test built a context with the
  // key present on both sources at once, which is exactly why this was blind
  // to it — the capsule must keep showing once the live event has moved past
  // creation and no longer carries `app_id` itself.
  it("keeps the capsule for an app session past its first turn (app_id dropped from live context, still in the snapshot)", () => {
    mount({ app_id: "app-42" }, { model: "claude-opus-5", mode: "act" });
    expect(screen.getByText("Open in Coder")).toBeInTheDocument();
  });

  it("keeps the capsule for a wiki maintainer session past its first turn (slug dropped from live context, still in the snapshot)", () => {
    mount({ slug: "github.com/acme/beacon" }, { mcp_tools: ["mcp__wiki__ask"] });
    expect(screen.getByText("Open in Coder")).toBeInTheDocument();
  });

  // The flip side of the same law: `project` must NOT fall back to the
  // snapshot — an auto-select session that switched away from a project (or
  // never had one) must not have the capsule kept alive by a stale snapshot
  // value, the way it correctly IS for `slug`/`app_id` above.
  it("does not fall back to the snapshot's project once the live context has moved past it", () => {
    mount({ project: "my-project" }, { mcp_tools: [] });
    expect(screen.queryByText("Open in Coder")).not.toBeInTheDocument();
  });

  it("prefers the server-returned project_name over the client label once an instance exists", () => {
    mockIdeStatus.mockReturnValue({
      instance: {
        session_id: "sess-1",
        status: "ready",
        url: "https://ide.example/x",
        project_name: "acme/beacon (wiki checkout)",
        project_path: "/srv/wiki/acme-beacon",
        created_at: "2026-01-01T00:00:00Z",
        expires_at: "2026-01-01T01:00:00Z",
        max_deadline: "2026-01-01T04:00:00Z",
        remaining_seconds: 3600,
        extensions: 0,
      },
      refresh: vi.fn(),
      setInstance: vi.fn(),
    });
    mount({ slug: "github.com/acme/beacon" });
    expect(
      screen.getByTitle("Open acme/beacon (wiki checkout) in Coder"),
    ).toBeInTheDocument();
  });
});
