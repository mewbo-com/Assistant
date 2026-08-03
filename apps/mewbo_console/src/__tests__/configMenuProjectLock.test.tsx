/**
 * ConfigMenu's Project panel under a durable, non-editable binding.
 *
 * A purpose-bound session's project has no per-turn override at all (unlike
 * the tool ceiling, there is no "unlock this turn" — see
 * `InputBar.projectBindingLocked`). So while `projectLocked` is true, a pick
 * in this panel must route through the durable `onRebindProject` mutation
 * instead of the local-only `onSelectProject`, which the server would
 * otherwise silently refuse (`SessionSpec.field_editable`). Drives the real
 * `ConfigMenu`, stubbing only the repositories HTTP module — same harness as
 * `configMenuRepositories.test.tsx`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
import {
  AUTO_PROJECT,
  AUTO_PROJECT_DESCRIPTION,
  AUTO_PROJECT_LABEL,
} from "../utils/projectLabel";
import type { ProjectSummary } from "../api/client";

const beacon: ProjectSummary = {
  name: "beacon",
  path: "/srv/beacon",
  source: "config",
  is_worktree: false,
};

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderMenu(overrides: Partial<Parameters<typeof ConfigMenu>[0]> = {}) {
  const onSelectProject = vi.fn();
  const onRebindProject = vi.fn();
  const qc = makeQc();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  render(
    <ConfigMenu
      mcpOptions={[]}
      skills={[]}
      projects={[beacon]}
      // Left null rather than "beacon" — the trigger button's closed-state
      // label and this Command item would otherwise share the same text and
      // collide under `findByText`. Which project is CURRENTLY active is not
      // what these tests are pinning; the routing of a pick is.
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
      onSelectProject={onSelectProject}
      onSelectSkill={vi.fn()}
      onResetAll={vi.fn()}
      onRebindProject={onRebindProject}
      open
      onToggleOpen={vi.fn()}
      {...overrides}
    />,
    { wrapper },
  );
  return { onSelectProject, onRebindProject };
}

/**
 * Drill into the Project panel and return a `within`-scoped query bound to
 * the popover content. The trigger button's own closed-state label stays
 * mounted alongside the open panel, so an unscoped `screen.findByText` can
 * collide with a Command item sharing the same text (e.g. both read
 * "Temporary directory" when no project is active).
 */
async function openProjectPanel(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("Project"));
  return within(await screen.findByRole("dialog"));
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(cleanup);

describe("ConfigMenu — unlocked project (regression guard)", () => {
  it("selecting a project still calls onSelectProject, never onRebindProject", async () => {
    const user = userEvent.setup();
    const { onSelectProject, onRebindProject } = renderMenu({ projectLocked: false });
    const panel = await openProjectPanel(user);

    await user.click(await panel.findByText("beacon"));

    expect(onSelectProject).toHaveBeenCalledWith("beacon");
    expect(onRebindProject).not.toHaveBeenCalled();
  });

  it("still offers 'Temporary directory' when unlocked", async () => {
    const user = userEvent.setup();
    const { onSelectProject } = renderMenu({ projectLocked: false });
    const panel = await openProjectPanel(user);

    await user.click(await panel.findByText("Temporary directory"));

    expect(onSelectProject).toHaveBeenCalledWith(null);
  });

  it("offers Auto as an ordinary local pick, distinct from Temporary", async () => {
    const user = userEvent.setup();
    const { onSelectProject } = renderMenu({ projectLocked: false });
    const panel = await openProjectPanel(user);

    await user.click(await panel.findByText(AUTO_PROJECT_LABEL));

    expect(onSelectProject).toHaveBeenCalledWith(AUTO_PROJECT);
  });

  it("says who does the picking, on the row itself rather than in a tooltip", async () => {
    // "Auto" alone reads as though Mewbo guesses on the user's behalf. The row
    // has to say the AGENT chooses, visibly, before the pick is made.
    const user = userEvent.setup();
    renderMenu({ projectLocked: false });
    const panel = await openProjectPanel(user);

    expect(await panel.findByText(AUTO_PROJECT_DESCRIPTION)).toBeInTheDocument();
    expect(AUTO_PROJECT_DESCRIPTION).toMatch(/agent/i);
  });
});

describe("ConfigMenu — locked project (durable rebind)", () => {
  it("shows the bound-project banner instead of an editable-looking list", async () => {
    const user = userEvent.setup();
    renderMenu({ projectLocked: true });
    const panel = await openProjectPanel(user);

    expect(
      await panel.findByText(/this session is bound to a project/i),
    ).toBeInTheDocument();
  });

  it("routes a pick through onRebindProject, never onSelectProject", async () => {
    const user = userEvent.setup();
    const { onSelectProject, onRebindProject } = renderMenu({ projectLocked: true });
    const panel = await openProjectPanel(user);

    await user.click(await panel.findByText("beacon"));

    expect(onRebindProject).toHaveBeenCalledWith("beacon");
    expect(onSelectProject).not.toHaveBeenCalled();
  });

  it("hides 'Temporary directory' — the rebind route only ever BINDS", async () => {
    // Clearing a project would null the session's cwd (the exact defect this
    // route exists to fix), so there is no unbind pick to offer while locked.
    const user = userEvent.setup();
    renderMenu({ projectLocked: true });
    const panel = await openProjectPanel(user);

    await panel.findByText("beacon"); // the panel has rendered its items
    expect(panel.queryByText("Temporary directory")).not.toBeInTheDocument();
  });

  it("shows a pending state and refuses a second rebind while one is in flight", async () => {
    const user = userEvent.setup();
    const { onRebindProject } = renderMenu({
      projectLocked: true,
      projectRebinding: true,
    });
    const panel = await openProjectPanel(user);

    expect(await panel.findByText(/rebinding the session's project/i)).toBeInTheDocument();

    await user.click(await panel.findByText("beacon"));
    expect(onRebindProject).not.toHaveBeenCalled();
  });

  it("KEEPS the Auto row — it is a bind, not the unbind the route lacks", async () => {
    // The reason "Temporary directory" hides is a WIRE SHAPE: its pick is
    // `null`, and the rebind route has no representation for that. `auto` is a
    // real, non-empty key the same route accepts, so the reason does not apply.
    //
    // Hiding it anyway would be the worst possible asymmetry: a session created
    // in auto mode whose agent has since switched into a concrete project would
    // find every project pickable EXCEPT the one that restores the behaviour it
    // was created for. Whether a particular bound session may go back to auto is
    // the server's call, and it refuses inline (see the error test below).
    const user = userEvent.setup();
    const { onRebindProject, onSelectProject } = renderMenu({ projectLocked: true });
    const panel = await openProjectPanel(user);

    await user.click(await panel.findByText(AUTO_PROJECT_LABEL));

    expect(onRebindProject).toHaveBeenCalledWith(AUTO_PROJECT);
    expect(onSelectProject).not.toHaveBeenCalled();
  });

  it("refuses a second Auto rebind while one is in flight", async () => {
    const user = userEvent.setup();
    const { onRebindProject } = renderMenu({
      projectLocked: true,
      projectRebinding: true,
    });
    const panel = await openProjectPanel(user);

    await user.click(await panel.findByText(AUTO_PROJECT_LABEL));
    expect(onRebindProject).not.toHaveBeenCalled();
  });

  it("surfaces a failed rebind's error inline", async () => {
    const user = userEvent.setup();
    renderMenu({
      projectLocked: true,
      projectRebindError: "could not resolve project path",
    });
    const panel = await openProjectPanel(user);

    expect(await panel.findByText(/could not resolve project path/)).toBeInTheDocument();
  });
});
