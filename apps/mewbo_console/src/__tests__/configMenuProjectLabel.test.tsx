/**
 * ConfigMenu's trigger label — the composer's Project readout.
 *
 * The console persists `context.project` as `managed:<uuid>`, so naming a
 * session's project is a LOOKUP, not a string copy. `ProjectLabel`
 * (`utils/projectLabel.ts`) is the one resolver that does it — the session list
 * already reads through it — and these tests pin that the composer's trigger
 * (and the "Project" row it mirrors) does too. Four states the hand-rolled
 * `projects.find(...)` it replaced got wrong: a cold projects cache, a project
 * the worktree reaper has since deleted, a worktree (whose own `name` IS its
 * branch, so the Project row read identically to the Worktree row and named the
 * parent repo nowhere), and the plain configured project that must keep working.
 *
 * Every assertion reads the RENDERED trigger label, scoped with `within` so a
 * Command item sharing the same text in an open panel can never stand in for it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("../api/repositories", async () => {
  const actual =
    await vi.importActual<typeof import("../api/repositories")>(
      "../api/repositories",
    );
  return {
    ...actual,
    listRepositories: vi.fn().mockResolvedValue([]),
    checkoutRepository: vi.fn(),
  };
});

import { ConfigMenu } from "../components/ConfigMenu";
import { AUTO_PROJECT, AUTO_PROJECT_LABEL } from "../utils/projectLabel";
import type { ProjectSummary } from "../api/client";

const BEACON_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0";
const WORKTREE_ID = "9a8b7c6d-5e4f-3021-1203-f4e5d6c7b8a9";

const CONFIG_PROJECT: ProjectSummary = {
  name: "relay",
  path: "/srv/relay",
  source: "config",
  is_worktree: false,
};

const MANAGED_PARENT: ProjectSummary = {
  name: "beacon",
  path: "/srv/beacon",
  source: "managed",
  project_id: BEACON_ID,
  is_worktree: false,
};

const MANAGED_WORKTREE: ProjectSummary = {
  // A managed worktree's project name IS its branch
  // (`project_store.py` mints it as `name=branch`), which is exactly why
  // printing `entry.name` here duplicated the Worktree row.
  name: "feature/login",
  path: "/srv/beacon-wt",
  source: "managed",
  project_id: WORKTREE_ID,
  is_worktree: true,
  parent_project_id: BEACON_ID,
  branch: "feature/login",
};

function renderMenu(overrides: Partial<Parameters<typeof ConfigMenu>[0]> = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  render(
    <ConfigMenu
      mcpOptions={[]}
      skills={[]}
      projects={[]}
      activeProject={null}
      activeSkill={null}
      mcpLoading={false}
      mcpError={null}
      skillsLoading={false}
      skillsError={null}
      projectsLoading={false}
      projectsError={null}
      onRefreshMcp={vi.fn()}
      onRefreshSkills={vi.fn()}
      onRefreshProjects={vi.fn()}
      onToggleMcp={vi.fn()}
      onSelectProject={vi.fn()}
      onSelectSkill={vi.fn()}
      onResetAll={vi.fn()}
      open={false}
      onToggleOpen={vi.fn()}
      {...overrides}
    />,
    { wrapper },
  );
  return within(screen.getByRole("button", { name: "Configure session" }));
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(cleanup);

describe("ConfigMenu — the project label is resolved, never a raw key", () => {
  it("names a managed project instead of printing its uuid", () => {
    const trigger = renderMenu({
      projects: [MANAGED_PARENT],
      activeProject: `managed:${BEACON_ID}`,
    });

    expect(trigger.getByText("beacon")).toBeInTheDocument();
    expect(trigger.queryByText(new RegExp(BEACON_ID))).not.toBeInTheDocument();
  });

  it("renders no raw key while the projects list is still loading", () => {
    // `useProjects()` sets no `staleTime`, so the first paint of a session page
    // is a cache miss. A uuid on screen for that beat is worse than a neutral
    // placeholder, because it reads as the project's actual name.
    const trigger = renderMenu({
      projects: [],
      projectsLoading: true,
      activeProject: `managed:${BEACON_ID}`,
    });

    expect(trigger.queryByText(new RegExp(BEACON_ID))).not.toBeInTheDocument();
    expect(trigger.queryByText(/^managed:/)).not.toBeInTheDocument();
  });

  it("renders no raw key for a project that is absent from the list", () => {
    // The worktree reaper deletes a childless promoted parent, so a session can
    // outlive its managed project. Loading has SETTLED here — the id is simply
    // gone — so the fallback has to be a permanent, readable one.
    const trigger = renderMenu({
      projects: [CONFIG_PROJECT],
      projectsLoading: false,
      activeProject: `managed:${BEACON_ID}`,
    });

    expect(trigger.queryByText(new RegExp(BEACON_ID))).not.toBeInTheDocument();
    expect(trigger.getByText("Managed project")).toBeInTheDocument();
  });

  it("names a worktree's PARENT repo, not the bare branch", () => {
    const trigger = renderMenu({
      projects: [MANAGED_PARENT, MANAGED_WORKTREE],
      activeProject: `managed:${WORKTREE_ID}`,
      activeBranch: "feature/login",
    });

    expect(trigger.getByText("beacon")).toBeInTheDocument();
    expect(trigger.queryByText("feature/login")).not.toBeInTheDocument();
  });

  it("still passes a configured project's name straight through", () => {
    const trigger = renderMenu({
      projects: [CONFIG_PROJECT],
      activeProject: "relay",
    });

    expect(trigger.getByText("relay")).toBeInTheDocument();
  });

  it("still says 'Temporary directory' when nothing is bound", () => {
    const trigger = renderMenu({ projects: [CONFIG_PROJECT], activeProject: null });

    expect(trigger.getByText("Temporary directory")).toBeInTheDocument();
  });

  it("reads as Auto in auto-select mode, never 'Temporary directory'", () => {
    // Auto-select DOES start in a temporary directory, which is exactly why the
    // label must not say so: a user who reads "Temporary directory" has no way
    // to tell that the agent is expected to move the session out of it.
    const trigger = renderMenu({
      projects: [CONFIG_PROJECT],
      activeProject: AUTO_PROJECT,
    });

    expect(trigger.getByText(AUTO_PROJECT_LABEL)).toBeInTheDocument();
    expect(trigger.queryByText("Temporary directory")).not.toBeInTheDocument();
  });
});
