/**
 * SessionHeader — "Open app" artifact-jump affordance.
 *
 * A session that IS an app's builder/maintainer session carries `app_id` on
 * its context event (stamped by `AppLifecycle._agent_session_context` — see
 * `types.ts:SessionContext`). SessionHeader renders a jump button straight off
 * that already-loaded field — no extra fetch, no `useApps()` cross-reference.
 * The button is absent for a plain session and navigates via the apps
 * router's `buildHref` (never a hand-built path) for a linked one.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionHeader } from "../SessionHeader";
import type { SessionSummary } from "../../types";
import * as client from "../../api/client";
import * as wikiClient from "../wiki/api/client";
import { buildHref as buildWikiHref } from "../wiki/router";
import type { Project } from "../wiki/api/types";

// `useWebIdeEnabled` (mounted unconditionally by SessionHeader) reads config
// via `getConfig()` — stub it so the suite never hits the real API client.
// A missing export here does NOT fail the mock factory — the query simply
// rejects and degrades to its empty state, so assertions keep passing while the
// path under test never runs. `listProjects` feeds the header's repo link.
vi.mock("../../api/client", () => ({
  getConfig: vi.fn().mockResolvedValue({ config: {}, secrets: {} }),
  listProjects: vi.fn().mockResolvedValue([]),
}));

// `useWikiSessionLink` (mounted only for `origin === "wiki"` sessions) reads
// through the wiki module's own client seam — stub the transport, keep the
// real TanStack hook so the enabled/staleTime/retry contract is exercised.
// `listProjects` backs the same-seam `useWikiProjects()` the button re-calls
// to build the deep link — mirrors the nav rail's WikiSection cache read.
vi.mock("../wiki/api/client", () => ({
  getWikiSessionLink: vi.fn(),
  listProjects: vi.fn(),
}));

const getConfig = vi.mocked(client.getConfig);
const getWikiSessionLink = vi.mocked(wikiClient.getWikiSessionLink);
const listProjects = vi.mocked(wikiClient.listProjects);

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    slug: "org/repo",
    source: "github",
    lang: "en",
    indexedAt: "2026-01-01T00:00:00Z",
    pages: 3,
    desc: "A repo",
    ...overrides,
  };
}

function renderHeader(
  session: SessionSummary,
  path = "/s/s1",
  liveProps: { liveStatus?: string; liveDoneReason?: string } = {},
) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const { hook, history } = memoryLocation({ path, record: true });
  const ui: ReactElement = (
    <QueryClientProvider client={qc}>
      <Router hook={hook}>
        <SessionHeader
          session={session}
          usage={null}
          isTerminated={false}
          onBack={vi.fn()}
          {...liveProps} />
      </Router>
    </QueryClientProvider>
  );
  return { history, ...render(ui) };
}

const base: SessionSummary = {
  session_id: "s1",
  title: "A session",
  status: "idle",
};

afterEach(cleanup);
beforeEach(() => {
  getConfig.mockClear();
  getWikiSessionLink.mockClear();
  listProjects.mockReset().mockResolvedValue([]);
});

describe("SessionHeader — live status precedence", () => {
  it("renders the poll-derived status/done_reason over a stale list-snapshot", () => {
    // The snapshot (from the sessions LIST query) still reads a finished,
    // canceled run — but the live poll says a NEW turn is running. The
    // header must show the live truth, not the stale snapshot.
    renderHeader(
      { ...base, status: "canceled", done_reason: "canceled" },
      "/s/s1",
      { liveStatus: "running", liveDoneReason: "" },
    );
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.queryByText("Canceled")).toBeNull();
  });

  it("falls back to the list-snapshot before the poll has returned a value", () => {
    renderHeader({ ...base, status: "canceled", done_reason: "canceled" });
    expect(screen.getByText("Canceled")).toBeInTheDocument();
  });
});

describe("SessionHeader — Open app jump", () => {
  it("is absent for a plain session with no app_id on context", () => {
    renderHeader(base);
    expect(screen.queryByRole("button", { name: "Open app" })).toBeNull();
  });

  it("is absent when context is present but carries no app_id", () => {
    renderHeader({ ...base, context: { project: "demo" } });
    expect(screen.queryByRole("button", { name: "Open app" })).toBeNull();
  });

  it("renders for an app-linked session and navigates to the app detail route", async () => {
    const user = userEvent.setup();
    const { history } = renderHeader({
      ...base,
      context: { app_id: "app-42" },
    });

    const button = screen.getByRole("button", { name: "Open app" });
    expect(button).toHaveAttribute("title", "Open the app this session builds");

    await user.click(button);
    expect(history.at(-1)).toBe("/apps/app-42");
  });
});

describe("SessionHeader — Open wiki jump", () => {
  it("is absent for a plain (non-wiki) session", () => {
    renderHeader(base);
    expect(screen.queryByRole("button", { name: "Open wiki" })).toBeNull();
    expect(getWikiSessionLink).not.toHaveBeenCalled();
  });

  it("navigates to the project's landing page when the cached project record has one", async () => {
    getWikiSessionLink.mockResolvedValue({ slug: "org/repo", kind: "indexing" });
    listProjects.mockResolvedValue([
      makeProject({ slug: "org/repo", source: "github", landingPageId: "overview" }),
    ]);
    const user = userEvent.setup();
    const { history } = renderHeader({ ...base, origin: "wiki" });

    const button = await screen.findByRole("button", { name: "Open wiki" });
    expect(button).toHaveAttribute("title", "Open the wiki project this session belongs to");

    await user.click(button);
    expect(history.at(-1)).toBe(
      buildWikiHref({ kind: "page", pageId: "overview", slug: "org/repo", platform: "github" }),
    );
  });

  it("falls back to the wiki gallery when the resolved slug isn't in the cached project list", async () => {
    getWikiSessionLink.mockResolvedValue({ slug: "org/repo", kind: "indexing" });
    listProjects.mockResolvedValue([]); // project not (yet) in the cache
    const user = userEvent.setup();
    const { history } = renderHeader({ ...base, origin: "wiki" });

    const button = await screen.findByRole("button", { name: "Open wiki" });
    await user.click(button);
    expect(history.at(-1)).toBe(buildWikiHref({ kind: "landing" }));
  });

  it("is absent for a wiki-origin session when the link endpoint 404s (resolves null)", async () => {
    getWikiSessionLink.mockResolvedValue(null);
    renderHeader({ ...base, origin: "wiki" });

    await waitFor(() => expect(getWikiSessionLink).toHaveBeenCalledWith("s1"));
    expect(screen.queryByRole("button", { name: "Open wiki" })).toBeNull();
  });
});

// A Q&A session and an indexing session are BOTH `origin: "wiki"`, and both
// resolve through the same endpoint — the discriminant is the only thing
// separating them, and it used to be ignored, so every Q&A session was routed
// to its project's landing page. These cases pin the branch from both sides.
describe("SessionHeader — Open answer jump (the `qa` discriminant)", () => {
  const qaLink = {
    kind: "qa" as const,
    slug: "org/repo",
    answerId: "ans-42",
    fromPageId: "overview",
    question: "how does indexing resume?",
  };

  it("navigates to the answer's deep link, not the project landing page", async () => {
    getWikiSessionLink.mockResolvedValue(qaLink);
    // A landing page IS available — the indexing branch would have taken it.
    listProjects.mockResolvedValue([
      makeProject({ slug: "org/repo", source: "github", landingPageId: "overview" }),
    ]);
    const user = userEvent.setup();
    const { history } = renderHeader({ ...base, origin: "wiki" });

    const button = await screen.findByRole("button", { name: "Open answer" });
    expect(button).toHaveAttribute(
      "title",
      "Open the wiki answer this session is generating",
    );

    await user.click(button);
    expect(history.at(-1)).toBe(
      buildWikiHref({
        kind: "qa",
        question: "how does indexing resume?",
        pageId: "overview",
        slug: "org/repo",
        answer: "ans-42",
      }),
    );
    // The `?answer=` id is what makes the destination the answer rather than
    // the project — assert it explicitly so a href refactor can't drop it.
    expect(history.at(-1)).toContain("answer=ans-42");
  });

  it("is labelled for the answer it opens, never 'Open wiki'", async () => {
    getWikiSessionLink.mockResolvedValue(qaLink);
    renderHeader({ ...base, origin: "wiki" });

    await screen.findByRole("button", { name: "Open answer" });
    expect(screen.queryByRole("button", { name: "Open wiki" })).toBeNull();
  });

  it("resolves the answer without the project cache (no landingPageId needed)", async () => {
    getWikiSessionLink.mockResolvedValue(qaLink);
    listProjects.mockResolvedValue([]); // project not in the cache at all
    const user = userEvent.setup();
    const { history } = renderHeader({ ...base, origin: "wiki" });

    await user.click(await screen.findByRole("button", { name: "Open answer" }));
    // The indexing branch would have fallen back to the gallery here.
    expect(history.at(-1)).not.toBe(buildWikiHref({ kind: "landing" }));
    expect(history.at(-1)).toContain("answer=ans-42");
  });

  it("falls back to the project route when a `qa` link carries no answer id", async () => {
    // Defensive: an older server that knows the kind but not the id. Routing
    // to `?answer=undefined` would be worse than the pre-fix behaviour.
    getWikiSessionLink.mockResolvedValue({ kind: "qa", slug: "org/repo" } as never);
    listProjects.mockResolvedValue([
      makeProject({ slug: "org/repo", source: "github", landingPageId: "overview" }),
    ]);
    const user = userEvent.setup();
    const { history } = renderHeader({ ...base, origin: "wiki" });

    await user.click(await screen.findByRole("button", { name: "Open wiki" }));
    expect(history.at(-1)).toBe(
      buildWikiHref({ kind: "page", pageId: "overview", slug: "org/repo", platform: "github" }),
    );
  });
});
