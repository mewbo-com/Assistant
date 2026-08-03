/**
 * The composer's project picker surfaces REGISTERED REPOSITORIES.
 *
 * Registering a repository is inert by design, so a registered repository has
 * no checkout until someone asks for one. That makes three properties worth
 * pinning, and each is a product rule rather than a rendering detail:
 *
 * 1. Opening the picker must never clone. A clone spends network, disk and a
 *    credential; it happens only when a labelled control is clicked.
 * 2. A repository that already has a checkout selects through the EXISTING
 *    `managed:<project_id>` contract. The anchoring contract is correct and
 *    must not grow a second field for repositories.
 * 3. The project id a session anchors to comes from the checkout RESPONSE,
 *    never from a guess, so a caller can never anchor to an id we invented.
 *
 * Drives the real `ConfigMenu` with the real hooks, stubbing only the HTTP
 * module — a test that mocked the hooks would assert nothing about the wiring
 * between the picker and the endpoint, which is the part that can break.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
    listRepositories: vi.fn(),
    checkoutRepository: vi.fn(),
  };
});

import * as repoApi from "../api/repositories";
import type { RepositoryDTO } from "../api/repositories";
import { ConfigMenu } from "../components/ConfigMenu";
import type { ProjectSummary } from "../api/client";

const listRepositories = vi.mocked(repoApi.listRepositories);
const checkoutRepository = vi.mocked(repoApi.checkoutRepository);

function repo(
  slug: string,
  overrides: Partial<RepositoryDTO> = {},
): RepositoryDTO {
  const [, owner, name] = slug.split("/");
  return {
    slug,
    host: slug.split("/")[0],
    owner,
    repo: name,
    repoUrl: `https://${slug}`,
    platform: "github",
    defaultBranch: null,
    name,
    description: null,
    origin: "manual",
    createdAt: "2020-01-01T00:00:00Z",
    updatedAt: "2020-01-01T00:00:00Z",
    usage: { wiki: null, tasks: null, credential: null },
    ...overrides,
  };
}

const beacon = repo("git.example.com/acme/beacon");
const relay = repo("github.com/acme/relay", {
  usage: {
    wiki: null,
    tasks: { projectId: "p-relay", name: "relay", path: "/srv/relay" },
    credential: null,
  },
});

/** The managed project a checked-out repository resolves to. */
const relayProject: ProjectSummary = {
  name: "relay",
  path: "/srv/relay",
  description: "Checkout of github.com/acme/relay",
  source: "managed",
  project_id: "p-relay",
  is_worktree: false,
};

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderMenu(
  overrides: Partial<Parameters<typeof ConfigMenu>[0]> = {},
  projects: ProjectSummary[] = [],
) {
  const onSelectProject = vi.fn();
  const qc = makeQc();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  render(
    <ConfigMenu
      mcpOptions={[]}
      skills={[]}
      projects={projects}
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
      open
      onToggleOpen={vi.fn()}
      {...overrides}
    />,
    { wrapper },
  );
  return { onSelectProject };
}

/**
 * Drill into the Project panel. The popover renders open (`open` is a prop, so
 * the root list is already mounted); the row label is a span inside its button,
 * and the click bubbles.
 */
async function openProjectPanel(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("Project"));
}

beforeEach(() => {
  vi.clearAllMocks();
  listRepositories.mockResolvedValue([beacon, relay]);
});
afterEach(cleanup);

describe("ConfigMenu repositories group", () => {
  it("lists registered repositories without cloning anything", async () => {
    const user = userEvent.setup();
    renderMenu();
    await openProjectPanel(user);

    expect(await screen.findByText("Repositories")).toBeInTheDocument();
    expect(await screen.findByText("beacon")).toBeInTheDocument();
    // The whole point of the endpoint being a separate verb: browsing is free.
    expect(checkoutRepository).not.toHaveBeenCalled();
  });

  it("labels the uncheckedout repository with an explicit Set up", async () => {
    const user = userEvent.setup();
    renderMenu();
    await openProjectPanel(user);

    expect(await screen.findByText("Set up")).toBeInTheDocument();
    // A repository that already has a checkout offers nothing to set up.
    expect(screen.getAllByText("Set up")).toHaveLength(1);
  });

  it("selects an already-checked-out repository through managed:<project_id>", async () => {
    const user = userEvent.setup();
    const { onSelectProject } = renderMenu();
    await openProjectPanel(user);

    await user.click(await screen.findByText("relay"));

    expect(onSelectProject).toHaveBeenCalledWith("managed:p-relay");
    expect(checkoutRepository).not.toHaveBeenCalled();
  });

  it("checks out on demand and then selects the project the SERVER returned", async () => {
    const user = userEvent.setup();
    checkoutRepository.mockResolvedValue(
      repo("git.example.com/acme/beacon", {
        usage: {
          wiki: null,
          tasks: { projectId: "p-fresh", name: "beacon", path: "/srv/p-fresh" },
          credential: null,
        },
      }),
    );
    const { onSelectProject } = renderMenu();
    await openProjectPanel(user);

    await user.click(await screen.findByText("beacon"));

    await waitFor(() => expect(checkoutRepository).toHaveBeenCalledWith(beacon.slug));
    // The id is the server's, read off the response — never derived locally.
    await waitFor(() =>
      expect(onSelectProject).toHaveBeenCalledWith("managed:p-fresh"),
    );
  });

  it("surfaces a failed checkout instead of silently selecting nothing", async () => {
    const user = userEvent.setup();
    checkoutRepository.mockRejectedValue(
      new repoApi.RepositoryError(
        "checkout_failed",
        "could not check out git.example.com/acme/beacon: fatal: could not read Username",
        503,
      ),
    );
    const { onSelectProject } = renderMenu();
    await openProjectPanel(user);

    await user.click(await screen.findByText("beacon"));

    expect(
      await screen.findByText(/could not read Username/),
    ).toBeInTheDocument();
    expect(onSelectProject).not.toHaveBeenCalled();
  });

  it("refuses to select when a 2xx carries no project id", async () => {
    const user = userEvent.setup();
    // A checkout that reports success but whose usage projection cannot see it.
    // Anchoring a session to a fabricated id would be worse than refusing.
    checkoutRepository.mockResolvedValue(repo("git.example.com/acme/beacon"));
    const { onSelectProject } = renderMenu();
    await openProjectPanel(user);

    await user.click(await screen.findByText("beacon"));

    expect(await screen.findByText(/could not link it to a project/i)).toBeInTheDocument();
    expect(onSelectProject).not.toHaveBeenCalled();
  });

  it("does not list a repository's checkout twice", async () => {
    const user = userEvent.setup();
    renderMenu({}, [relayProject]);
    await openProjectPanel(user);

    await screen.findByText("Repositories");
    // The managed project IS the repository's checkout, so it belongs under
    // Repositories and nowhere else. Two rows for one thing is the bug.
    expect(screen.queryByText("Managed")).not.toBeInTheDocument();
    expect(screen.getAllByText("relay")).toHaveLength(1);
  });
});
