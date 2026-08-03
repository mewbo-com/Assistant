/**
 * SessionHeader — the two session facts beside the context bar: the repository
 * the session works in, and how much it changed.
 *
 * Both are read off already-loaded data. The repo slug resolves through the
 * shared `["projects"]` query the composer's picker and the landing page keep
 * warm (`managed:<uuid>` by id, a worktree via its PARENT), and the link is
 * built by the SAME `RepoLink`/`canonicalRepoUrl` pair the wiki uses — a
 * project with no git remote carries no `repo`, so nothing renders rather than
 * a fabricated github.com href. The diff count comes from the server-side
 * `SessionSummary.diff_stat`, the same field the landing-page row reads, so
 * the two surfaces cannot disagree.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionHeader } from "../SessionHeader";
import type { SessionContext, SessionSummary } from "../../types";
import type { ProjectSummary } from "../../api/contracts";
import * as client from "../../api/client";
import { AUTO_PROJECT, AUTO_PROJECT_LABEL } from "../../utils/projectLabel";

// `useWebIdeEnabled` reads config and `useProjects` reads the project list —
// both mount unconditionally, so stub the transport and keep the real hooks.
vi.mock("../../api/client", () => ({
  getConfig: vi.fn().mockResolvedValue({ config: {}, secrets: {} }),
  listProjects: vi.fn(),
}));

// `useWikiSessionLink`/`useWikiProjects` only run for a wiki-origin session,
// but the module is imported either way — stub its transport seam.
vi.mock("../wiki/api/client", () => ({
  getWikiSessionLink: vi.fn(),
  listProjects: vi.fn(),
}));

const getConfig = vi.mocked(client.getConfig);
const listProjects = vi.mocked(client.listProjects);

const MANAGED_ID = "1f2e3d4c";

/** A managed checkout with a real remote — the backend fills repo + aliases. */
const withRemote: ProjectSummary = {
  name: "assistant",
  path: "/w/assistant",
  source: "managed",
  project_id: MANAGED_ID,
  repo: { host: "github.com", owner: "bearlike", name: "Assistant" },
  aliases: ["github.com/bearlike/Assistant", "bearlike/Assistant", "Assistant"],
};

/** A managed dir with no git remote — `repo`/`aliases` are ABSENT, not null. */
const withoutRemote: ProjectSummary = {
  name: "scratch",
  path: "/w/scratch",
  source: "managed",
  project_id: MANAGED_ID,
};

function renderHeader(session: SessionSummary, context?: SessionContext) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const { hook } = memoryLocation({ path: "/s/s1", record: true });
  const ui: ReactElement = (
    <QueryClientProvider client={qc}>
      <Router hook={hook}>
        <SessionHeader
          session={session}
          context={context}
          usage={null}
          isTerminated={false}
          onBack={vi.fn()} />
      </Router>
    </QueryClientProvider>
  );
  return render(ui);
}

const base: SessionSummary = {
  session_id: "s1",
  title: "A session",
  status: "idle",
  context: { project: `managed:${MANAGED_ID}` },
};

afterEach(cleanup);
beforeEach(() => {
  getConfig.mockClear();
  listProjects.mockReset().mockResolvedValue([]);
});

describe("SessionHeader — repository fact", () => {
  it("links a session whose project has a remote to the canonical repo URL", async () => {
    listProjects.mockResolvedValue([withRemote]);
    renderHeader(base);

    const link = await screen.findByRole("link", { name: "bearlike/Assistant" });
    expect(link).toHaveAttribute("href", "https://github.com/bearlike/Assistant");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("title", "Open on github.com");
  });

  it("resolves a worktree session through its PARENT project's remote", async () => {
    const worktree: ProjectSummary = {
      name: "assistant-feat",
      path: "/w/assistant-feat",
      source: "managed",
      project_id: "wt-9",
      is_worktree: true,
      parent_project_id: MANAGED_ID,
      branch: "feat/x",
    };
    listProjects.mockResolvedValue([withRemote, worktree]);
    renderHeader({ ...base, context: { project: "managed:wt-9" } });

    const link = await screen.findByRole("link", { name: "bearlike/Assistant" });
    expect(link).toHaveAttribute("href", "https://github.com/bearlike/Assistant");
  });

  it("renders no link at all when the project has no git remote", async () => {
    listProjects.mockResolvedValue([withoutRemote]);
    renderHeader(base);

    // Wait for the projects query to settle so this isn't a pre-fetch pass.
    await vi.waitFor(() => expect(listProjects).toHaveBeenCalled());
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    expect(screen.queryByText(/github\.com/)).toBeNull();
  });

  it("renders no link for a session with no project bound at all", async () => {
    listProjects.mockResolvedValue([withRemote]);
    renderHeader({ ...base, context: {} });

    await vi.waitFor(() => expect(listProjects).toHaveBeenCalled());
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });
});

describe("SessionHeader — overall diff count", () => {
  it("renders the session's server-side additions and deletions", () => {
    renderHeader({ ...base, diff_stat: { additions: 128, deletions: 47 } });
    expect(screen.getByText("+128")).toBeInTheDocument();
    expect(screen.getByText("-47")).toBeInTheDocument();
  });

  it("renders nothing when the session edited no files", () => {
    renderHeader({ ...base, diff_stat: { additions: 0, deletions: 0 } });
    expect(screen.queryByText(/^\+\d/)).toBeNull();
    expect(screen.queryByText(/^-\d/)).toBeNull();
  });

  it("renders nothing when the summary carries no diff_stat", () => {
    renderHeader(base);
    expect(screen.queryByText(/^\+\d/)).toBeNull();
    expect(screen.queryByText(/^-\d/)).toBeNull();
  });
});

describe("SessionHeader — which project the session is in RIGHT NOW", () => {
  // No longer a fixed property of a session: an auto-select session starts in a
  // temporary directory and the agent moves it, possibly more than once. The
  // header is the only always-visible surface that can answer "where is this
  // running" without opening the composer, so it reads the LIVE context rather
  // than the list-fetch snapshot.

  it("names the project, resolved rather than printed as a raw key", async () => {
    listProjects.mockResolvedValue([withRemote]);
    renderHeader(base);

    expect(await screen.findByText("assistant")).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(MANAGED_ID))).toBeNull();
  });

  it("follows a switch: the LIVE context beats the session snapshot", async () => {
    // The exact regression this prop exists to prevent. `session.context` is a
    // list-fetch snapshot naming where the session STARTED; after the agent
    // switches, the header must name where it IS.
    const relay: ProjectSummary = {
      name: "relay",
      path: "/w/relay",
      source: "managed",
      project_id: "relay-1",
    };
    listProjects.mockResolvedValue([withoutRemote, relay]);
    renderHeader(
      { ...base, context: { project: `managed:${MANAGED_ID}` } },
      { project: "managed:relay-1" },
    );

    expect(await screen.findByText("relay")).toBeInTheDocument();
    expect(screen.queryByText("scratch")).toBeNull();
  });

  it("reads as Auto before the agent has chosen anything", async () => {
    listProjects.mockResolvedValue([withRemote]);
    renderHeader(base, { project: AUTO_PROJECT });

    expect(await screen.findByText(AUTO_PROJECT_LABEL)).toBeInTheDocument();
  });

  it("renders no project segment for a session with none", async () => {
    listProjects.mockResolvedValue([withRemote]);
    renderHeader({ ...base, context: {} });

    await vi.waitFor(() => expect(listProjects).toHaveBeenCalled());
    expect(screen.queryByTitle(/^Running in /)).toBeNull();
  });
});
