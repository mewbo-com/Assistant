/**
 * RepositoriesPane — render tests for the repository registry table, plus pure
 * unit tests for the `RepositoryRow` view model it renders through.
 *
 * The assertions worth keeping are the ones that encode the product model this
 * surface was rebuilt to express: an unindexed repository still appears (the
 * list is the registry, not the wiki), "no wiki possible" and "no wiki yet"
 * stay distinguishable, and the two destructive actions are two different
 * buttons with two different confirms hitting two different endpoints.
 *
 * `vi.mock` covers the I/O-boundary hooks only. `RepositoryRow` is NOT mocked —
 * the sort, the search haystack, the wiki-state derivation and the paging are
 * real code under test, both through the rendered pane and directly at the
 * bottom of this file. `api/git` keeps every real export via `importOriginal`
 * and stubs only the network call.
 */
import {
  cleanup,
  render as rtlRender,
  screen,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { RepositoriesPane } from "../RepositoriesPane";
import { RepositoryRow } from "../repositories/repositoryRows";
import * as wikiHooks from "../../../wiki/api/hooks";
import * as credentialHooks from "../../../../hooks/useGitCredentials";
import * as repositoryHooks from "../../../../hooks/useRepositories";
import * as gitApi from "../../../../api/git";
import type { RepositoryDTO, RepositoryUsage } from "../../../../api/repositories";

vi.mock("../../../wiki/api/hooks", () => ({
  useWikiProjects: vi.fn(),
  useDeleteProject: vi.fn(),
  useRequestWikiRefresh: vi.fn(),
  useWikiPlatforms: vi.fn(),
}));

vi.mock("../../../../hooks/useGitCredentials", () => ({
  useGitCredentials: vi.fn(),
}));

vi.mock("../../../../hooks/useRepositories", () => ({
  useRepositories: vi.fn(),
  useCreateRepository: vi.fn(),
  useDeleteRepository: vi.fn(),
  useCheckoutRepository: vi.fn(),
}));

// Keep the real `matchCredential`/`scopeTypeOf`/`hostOf` helpers — the add
// dialog's coverage hint depends on the genuine chain-precedence logic. Stub
// only the network call.
vi.mock("../../../../api/git", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../../api/git")>();
  return {
    ...actual,
    validateGitCredential: vi.fn(),
  };
});

const useWikiProjects = vi.mocked(wikiHooks.useWikiProjects);
const useDeleteProject = vi.mocked(wikiHooks.useDeleteProject);
const useRequestWikiRefresh = vi.mocked(wikiHooks.useRequestWikiRefresh);
const useWikiPlatforms = vi.mocked(wikiHooks.useWikiPlatforms);
const useGitCredentials = vi.mocked(credentialHooks.useGitCredentials);
const useRepositories = vi.mocked(repositoryHooks.useRepositories);
const useCreateRepository = vi.mocked(repositoryHooks.useCreateRepository);
const useDeleteRepository = vi.mocked(repositoryHooks.useDeleteRepository);
const useCheckoutRepository = vi.mocked(repositoryHooks.useCheckoutRepository);
const validateGitCredential = vi.mocked(gitApi.validateGitCredential);

const NO_USAGE: RepositoryUsage = { wiki: null, tasks: null, credential: null };

function repository(
  overrides: Partial<RepositoryDTO> & Pick<RepositoryDTO, "slug" | "host" | "owner" | "repo">,
): RepositoryDTO {
  const { usage, ...rest } = overrides;
  return {
    repoUrl: `https://${overrides.host}/${overrides.owner}/${overrides.repo}`,
    platform: "gitea",
    defaultBranch: "main",
    name: null,
    description: null,
    origin: "manual",
    createdAt: "2026-07-01T00:00:00Z",
    updatedAt: "2026-07-01T00:00:00Z",
    ...rest,
    usage: { ...NO_USAGE, ...(usage ?? {}) },
  };
}

/** Indexed, covered by a repo-pinned credential, with a task workspace. */
const BEACON = repository({
  slug: "git.example.com/acme/beacon",
  host: "git.example.com",
  owner: "acme",
  repo: "beacon",
  origin: "wiki",
  usage: {
    wiki: { indexed: true, indexedAt: "2026-07-19T00:00:00Z", pages: 12 },
    tasks: { projectId: "p-1", name: "Beacon", path: "/work/beacon" },
    credential: { scope: "git.example.com/acme/beacon", scopeType: "repo" },
  },
});

/** Registered and untouched: exactly the row the old wiki-backed list could
 *  never show, which is why it is the fixture the defect is asserted against. */
const ORBIT = repository({
  slug: "git.example.com/acme/orbit",
  host: "git.example.com",
  owner: "acme",
  repo: "orbit",
  usage: {
    wiki: { indexed: false, indexedAt: null, pages: 0 },
    tasks: null,
    credential: { scope: "git.example.com", scopeType: "host" },
  },
});

/** No graph extra on this deployment: `usage.wiki` is null, not "not indexed". */
const CITADEL = repository({
  slug: "github.com/vendor/citadel",
  host: "github.com",
  owner: "vendor",
  repo: "citadel",
  platform: "github",
  usage: { wiki: null, tasks: null, credential: null },
});

function render(ui: ReactElement) {
  const { hook, history } = memoryLocation({ path: "/settings", record: true });
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = rtlRender(
    <QueryClientProvider client={qc}>
      <Router hook={hook}>{ui}</Router>
    </QueryClientProvider>,
  );
  return { ...utils, history };
}

let deleteIndexMutate: ReturnType<typeof vi.fn>;
let deregisterMutate: ReturnType<typeof vi.fn>;
let checkoutMutate: ReturnType<typeof vi.fn>;
let refreshMutate: ReturnType<typeof vi.fn>;
let createMutate: ReturnType<typeof vi.fn>;

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  // jsdom shares `window.location` across a whole file, so a test that leaves a
  // query string behind would start the next one somewhere else.
  window.history.replaceState({}, "", "/settings");

  useRepositories.mockReturnValue({
    repositories: [BEACON, ORBIT, CITADEL],
    loading: false,
    error: null,
    refresh: vi.fn(),
  });

  useGitCredentials.mockReturnValue({
    credentials: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  });

  useWikiProjects.mockReturnValue({
    data: [{ slug: BEACON.slug, landingPageId: "beacon-core" }],
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof wikiHooks.useWikiProjects>);

  useWikiPlatforms.mockReturnValue({
    data: [],
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof wikiHooks.useWikiPlatforms>);

  deleteIndexMutate = vi.fn();
  useDeleteProject.mockReturnValue({
    mutate: deleteIndexMutate,
    isPending: false,
    error: null,
  } as unknown as ReturnType<typeof wikiHooks.useDeleteProject>);

  deregisterMutate = vi.fn();
  useDeleteRepository.mockReturnValue({
    mutate: deregisterMutate,
    isPending: false,
    error: null,
    variables: undefined,
  } as unknown as ReturnType<typeof repositoryHooks.useDeleteRepository>);

  checkoutMutate = vi.fn();
  useCheckoutRepository.mockReturnValue({
    mutate: checkoutMutate,
    isPending: false,
    error: null,
    variables: undefined,
  } as unknown as ReturnType<typeof repositoryHooks.useCheckoutRepository>);

  createMutate = vi.fn();
  useCreateRepository.mockReturnValue({
    mutate: createMutate,
    reset: vi.fn(),
    isPending: false,
    error: null,
  } as unknown as ReturnType<typeof repositoryHooks.useCreateRepository>);

  refreshMutate = vi.fn();
  useRequestWikiRefresh.mockReturnValue({
    mutate: refreshMutate,
    isPending: false,
    variables: undefined,
  } as unknown as ReturnType<typeof wikiHooks.useRequestWikiRefresh>);

  validateGitCredential.mockResolvedValue({ ok: true, detail: "authenticated" });
});

/**
 * The `<tr>` containing *text*. Throws with the searched-for text rather than
 * handing back a null the caller has to guard, so a row that stopped rendering
 * fails with the name of the row instead of a null dereference three lines on.
 */
function rowFor(text: string): HTMLElement {
  const row = screen.getByText(text).closest("tr");
  if (!row) throw new Error(`no table row contains "${text}"`);
  return row;
}

async function openMenu(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("button", { name: `Actions for ${name}` }));
  return screen.findByRole("menu");
}

describe("RepositoriesPane", () => {
  test("lists every registered repository, including one that has never been indexed", () => {
    render(<RepositoriesPane />);

    expect(screen.getByText("beacon")).toBeInTheDocument();
    expect(screen.getByText("orbit")).toBeInTheDocument();
    expect(screen.getByText("citadel")).toBeInTheDocument();

    // The whole point of reading the registry: an unindexed repository has a
    // row, and it says so rather than showing a bare placeholder.
    expect(within(rowFor("orbit")).getByText("No wiki yet")).toBeInTheDocument();
  });

  test("the usage column distinguishes 'no wiki yet' from 'wiki unavailable'", () => {
    render(<RepositoriesPane />);

    expect(within(rowFor("beacon")).getByText(/^Indexed /)).toBeInTheDocument();
    expect(within(rowFor("orbit")).getByText("No wiki yet")).toBeInTheDocument();
    // `usage.wiki === null` means the deployment ships no graph extra; offering
    // "Generate wiki" there would be an action that cannot work.
    expect(within(rowFor("citadel")).getByText("Wiki unavailable")).toBeInTheDocument();
  });

  test("the usage column reports the task workspace and the covering credential", () => {
    render(<RepositoriesPane />);

    const beaconRow = rowFor("beacon");
    expect(within(beaconRow).getByText("Beacon")).toBeInTheDocument();
    expect(within(beaconRow).getByText(BEACON.slug)).toBeInTheDocument();

    const orbitRow = rowFor("orbit");
    expect(within(orbitRow).getByText("Tasks not set up")).toBeInTheDocument();
    expect(within(orbitRow).getByText("git.example.com")).toBeInTheDocument();

    expect(within(rowFor("citadel")).getByText("Server git config")).toBeInTheDocument();
  });

  test("search filters rows by repo name, owner, and host", async () => {
    const user = userEvent.setup();
    render(<RepositoriesPane />);

    const search = screen.getByRole("searchbox", { name: /search repositories/i });

    await user.type(search, "beacon");
    expect(screen.getByText("beacon")).toBeInTheDocument();
    expect(screen.queryByText("orbit")).not.toBeInTheDocument();
    expect(screen.queryByText("citadel")).not.toBeInTheDocument();

    await user.clear(search);
    await user.type(search, "acme");
    expect(screen.getByText("beacon")).toBeInTheDocument();
    expect(screen.getByText("orbit")).toBeInTheDocument();
    expect(screen.queryByText("citadel")).not.toBeInTheDocument();

    await user.clear(search);
    await user.type(search, "github.com");
    expect(screen.getByText("citadel")).toBeInTheDocument();
    expect(screen.queryByText("beacon")).not.toBeInTheDocument();
  });

  test("the primary action opens the register dialog instead of navigating to the wiki wizard", async () => {
    const user = userEvent.setup();
    const { history } = render(<RepositoriesPane />);

    await user.click(screen.getByRole("button", { name: "Add repository" }));

    const dialog = await screen.findByRole("dialog", { name: "Add repository" });
    expect(
      within(dialog).getByText(/Nothing is cloned, nothing is indexed, and no model runs/i),
    ).toBeInTheDocument();
    // The defect being fixed: the old button navigated straight into the wiki
    // onboarding wizard and started a paid indexing run.
    expect(history).toEqual(["/settings"]);
  });

  test("an unindexed row offers 'Generate wiki', and it is the action that navigates to the wizard", async () => {
    const user = userEvent.setup();
    const { history } = render(<RepositoriesPane />);

    const menu = await openMenu(user, "orbit");
    expect(
      within(menu).queryByRole("menuitem", { name: /^add$/i }),
    ).not.toBeInTheDocument();
    await user.click(within(menu).getByRole("menuitem", { name: /generate wiki/i }));

    expect(history[history.length - 1]).toContain("/wiki/configure");
    // It carries the SLUG, not the url. The wizard seeds url, platform and
    // default branch from the registry record, and its seed deliberately
    // yields to an explicit `?url=` — so handing it the url would fill one
    // field and silently drop the other two.
    expect(history[history.length - 1]).toContain(
      `repo=${encodeURIComponent(ORBIT.slug)}`,
    );
  });

  test("a repository with no checkout offers 'Set up for tasks', and it clones", async () => {
    const user = userEvent.setup();
    const { history } = render(<RepositoriesPane />);

    const menu = await openMenu(user, "orbit");
    await user.click(within(menu).getByRole("menuitem", { name: /set up for tasks/i }));

    // The action IS the checkout endpoint, keyed by the canonical slug.
    expect(checkoutMutate).toHaveBeenCalledWith(ORBIT.slug, expect.anything());
    // And it stays in Settings. "Generate wiki" is the ONE row action allowed
    // to navigate; a second one would make the rule unlearnable.
    expect(history[history.length - 1]).not.toContain("/wiki/");
  });

  test("the label says it sets something up, never that it merely uses it", async () => {
    const user = userEvent.setup();
    render(<RepositoriesPane />);

    const menu = await openMenu(user, "orbit");

    // A clone spends network, disk and a credential and can run for minutes.
    // "Use in tasks" would describe switching to something that already
    // exists, which is the one thing this action does not do.
    expect(
      within(menu).queryByRole("menuitem", { name: /use in tasks/i }),
    ).not.toBeInTheDocument();
    expect(
      within(menu).getByRole("menuitem", { name: /set up for tasks/i }),
    ).toBeInTheDocument();
  });

  test("a repository that already has a checkout is offered no set-up action", async () => {
    const user = userEvent.setup();
    render(<RepositoriesPane />);

    const menu = await openMenu(user, "beacon");

    // The tasks column already reports the workspace. A second item that did
    // nothing would read as a broken menu rather than as a no-op.
    expect(
      within(menu).queryByRole("menuitem", { name: /set up for tasks/i }),
    ).not.toBeInTheDocument();
    expect(checkoutMutate).not.toHaveBeenCalled();
  });

  test("a deployment without the graph extra is offered no wiki actions at all", async () => {
    const user = userEvent.setup();
    render(<RepositoriesPane />);

    const menu = await openMenu(user, "citadel");
    expect(within(menu).queryByRole("menuitem", { name: /generate wiki/i })).not.toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: /re-index wiki/i })).not.toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: /delete wiki index/i })).not.toBeInTheDocument();
    // Deregistering never depends on a product being installed.
    expect(within(menu).getByRole("menuitem", { name: /remove from mewbo/i })).toBeInTheDocument();
  });

  test("deleting the wiki index and removing the repository are separate actions on separate endpoints", async () => {
    const user = userEvent.setup();
    render(<RepositoriesPane />);

    const menu = await openMenu(user, "beacon");
    expect(within(menu).getByRole("menuitem", { name: /delete wiki index/i })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: /remove from mewbo/i })).toBeInTheDocument();

    await user.click(within(menu).getByRole("menuitem", { name: /delete wiki index/i }));
    const deleteDialog = await screen.findByRole("dialog", { name: "Delete wiki index?" });
    expect(
      within(deleteDialog).getByText(/The repository stays registered with Mewbo/i),
    ).toBeInTheDocument();
    await user.click(within(deleteDialog).getByRole("button", { name: "Delete wiki index" }));

    expect(deleteIndexMutate).toHaveBeenCalledWith(BEACON.slug, expect.any(Object));
    expect(deregisterMutate).not.toHaveBeenCalled();
  });

  test("'Remove from Mewbo' deregisters only, and its confirm says the wiki survives", async () => {
    const user = userEvent.setup();
    render(<RepositoriesPane />);

    const menu = await openMenu(user, "beacon");
    await user.click(within(menu).getByRole("menuitem", { name: /remove from mewbo/i }));

    expect(deregisterMutate).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog", {
      name: "Remove this repository from Mewbo?",
    });
    // The server re-adopts any wiki-indexed project missing from the registry,
    // so an indexed row WILL come back. A confirm that promised otherwise would
    // make correct server behaviour read as a bug.
    expect(within(dialog).getByText(/will reappear/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/Delete the wiki index first/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Remove repository" }));

    expect(deregisterMutate).toHaveBeenCalledWith(BEACON.slug, expect.any(Object));
    expect(deleteIndexMutate).not.toHaveBeenCalled();
  });

  test("an unindexed repository's removal confirm promises no reappearance", async () => {
    const user = userEvent.setup();
    render(<RepositoriesPane />);

    const menu = await openMenu(user, "orbit");
    await user.click(within(menu).getByRole("menuitem", { name: /remove from mewbo/i }));

    const dialog = await screen.findByRole("dialog", {
      name: "Remove this repository from Mewbo?",
    });
    expect(within(dialog).queryByText(/will reappear/i)).not.toBeInTheDocument();
  });

  test("a task workspace with no name or path still renders a labelled chip", () => {
    useRepositories.mockReturnValue({
      repositories: [
        repository({
          slug: "git.example.com/acme/lumen",
          host: "git.example.com",
          owner: "acme",
          repo: "lumen",
          // Only `projectId` is guaranteed on the wire.
          usage: { wiki: null, tasks: { projectId: "p-9", name: null, path: null }, credential: null },
        }),
      ],
      loading: false,
      error: null,
      refresh: vi.fn(),
    });

    render(<RepositoriesPane />);

    const row = rowFor("lumen");
    expect(within(row).getByText("Ready for tasks")).toBeInTheDocument();
    expect(within(row).queryByText("Tasks not set up")).not.toBeInTheDocument();
  });

  test("credential validation is offered only where a credential actually covers the row", async () => {
    const user = userEvent.setup();
    render(<RepositoriesPane />);

    const beaconMenu = await openMenu(user, "beacon");
    expect(
      within(beaconMenu).getByRole("menuitem", { name: /manage credential/i }),
    ).toBeInTheDocument();
    expect(
      within(beaconMenu).getByRole("menuitem", { name: /validate credential/i }),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");

    const citadelMenu = await openMenu(user, "citadel");
    expect(
      within(citadelMenu).getByRole("menuitem", { name: /manage credential/i }),
    ).toBeInTheDocument();
    expect(
      within(citadelMenu).queryByRole("menuitem", { name: /validate credential/i }),
    ).not.toBeInTheDocument();
  });

  test("empty state teaches the model rather than showing an empty table", () => {
    useRepositories.mockReturnValue({
      repositories: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
    });

    render(<RepositoriesPane />);

    expect(screen.getByText("No repositories yet.")).toBeInTheDocument();
    expect(screen.getByText(/each product then opts in on its own/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

// ── RepositoryRow — pure unit tests (no React, no network) ────────────────

describe("RepositoryRow.wikiState", () => {
  test("absent wiki usage means the capability is unavailable, not that nothing is indexed", () => {
    expect(RepositoryRow.fromDto(CITADEL).wikiState()).toBe("unavailable");
    expect(RepositoryRow.fromDto(ORBIT).wikiState()).toBe("not-indexed");
    expect(RepositoryRow.fromDto(BEACON).wikiState()).toBe("indexed");
  });

  test("an unparseable index timestamp collapses to no label rather than raw wire text", () => {
    const row = RepositoryRow.fromDto(
      repository({
        slug: "git.example.com/acme/lumen",
        host: "git.example.com",
        owner: "acme",
        repo: "lumen",
        usage: { wiki: { indexed: true, indexedAt: "not-a-date", pages: 3 }, tasks: null, credential: null },
      }),
    );

    expect(row.indexedAt).toBeNull();
    expect(row.indexedLabel()).toBeNull();
    expect(row.indexedTitle()).toBe("");
    // Still indexed — only the "when" is unknown.
    expect(row.wikiState()).toBe("indexed");
  });
});

describe("RepositoryRow tasks usage", () => {
  test("falls through name to path to a bare statement, so a chip is never blank", () => {
    const withName = RepositoryRow.fromDto(BEACON);
    expect(withName.tasksLabel()).toBe("Beacon");
    expect(withName.tasksTitle()).toBe("Agentic tasks run in /work/beacon");

    const pathOnly = RepositoryRow.fromDto({
      ...BEACON,
      usage: { ...BEACON.usage, tasks: { projectId: "p-1", name: null, path: "/work/x" } },
    });
    expect(pathOnly.tasksLabel()).toBe("/work/x");

    const idOnly = RepositoryRow.fromDto({
      ...BEACON,
      usage: { ...BEACON.usage, tasks: { projectId: "p-1", name: null, path: null } },
    });
    expect(idOnly.tasksLabel()).toBe("Ready for tasks");
    expect(idOnly.tasksTitle()).toBe("A workspace is set up for agentic tasks");

    expect(RepositoryRow.fromDto(ORBIT).tasksLabel()).toBeNull();
  });

  test("a null page count reads as no count rather than as zero pages", () => {
    const row = RepositoryRow.fromDto({
      ...BEACON,
      usage: {
        ...BEACON.usage,
        wiki: { indexed: true, indexedAt: "2026-07-19T00:00:00Z", pages: null },
      },
    });
    expect(row.indexed).toBe(true);
    expect(row.pages).toBe(0);
  });
});

describe("RepositoryRow.displayName", () => {
  test("prefers the operator's label and falls back to the repo name", () => {
    expect(RepositoryRow.fromDto(ORBIT).displayName()).toBe("orbit");
    expect(
      RepositoryRow.fromDto({ ...ORBIT, name: "Orbit Control" }).displayName(),
    ).toBe("Orbit Control");
    // A whitespace-only name is not a name.
    expect(RepositoryRow.fromDto({ ...ORBIT, name: "   " }).displayName()).toBe("orbit");
  });

  test("the search haystack covers the display name as well as the identity", () => {
    const row = RepositoryRow.fromDto({ ...ORBIT, name: "Orbit Control" });
    expect(row.matches("orbit control")).toBe(true);
    expect(row.matches("acme")).toBe(true);
    expect(row.matches("git.example.com")).toBe(true);
    expect(row.matches("beacon")).toBe(false);
    expect(row.matches("  ")).toBe(true);
  });
});

describe("RepositoryRow.paginate", () => {
  function makeRows(n: number): RepositoryRow[] {
    const repositories = Array.from({ length: n }, (_, i) =>
      repository({
        slug: `git.example.com/acme/repo-${String(i).padStart(2, "0")}`,
        host: "git.example.com",
        owner: "acme",
        repo: `repo-${String(i).padStart(2, "0")}`,
      }),
    );
    return RepositoryRow.build(repositories);
  }

  test("clamps an out-of-range page to the last page instead of returning an empty slice", () => {
    const page = RepositoryRow.paginate(makeRows(25), 5, 10);

    expect(page.page).toBe(2);
    expect(page.pageCount).toBe(3);
    expect(page.rows).toHaveLength(5);
    expect(page.from).toBe(21);
    expect(page.to).toBe(25);
    expect(page.total).toBe(25);
  });

  test("clamps a negative page to zero", () => {
    const page = RepositoryRow.paginate(makeRows(25), -3, 10);

    expect(page.page).toBe(0);
    expect(page.from).toBe(1);
    expect(page.to).toBe(10);
  });

  test("reports correct from/to/total for an in-range page", () => {
    const page = RepositoryRow.paginate(makeRows(25), 1, 10);

    expect(page.page).toBe(1);
    expect(page.rows).toHaveLength(10);
    expect(page.from).toBe(11);
    expect(page.to).toBe(20);
    expect(page.total).toBe(25);
  });
});
