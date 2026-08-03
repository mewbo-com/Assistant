/**
 * The composer can point a NEW session at a wiki project or a Mewbo App.
 *
 * The distinction this file exists to pin is that such a pick is not a project
 * scope. It does not answer "where does this session run"; it answers "what is
 * this session about", and it is spent by ROUTING creation to the product's own
 * get-or-create endpoint instead of `POST /api/sessions`. Four properties
 * follow, and each is a product rule rather than a rendering detail:
 *
 * 1. The tab lists BOTH products, and an archived app is not offered — its
 *    maintainer is gone, so the endpoint would be a dead end.
 * 2. A pick yields a discriminated target, never a project key. Nothing about
 *    it reaches `SessionContext`.
 * 3. The tab does not exist in detail mode. An existing session's purpose
 *    binding is durable and the server refuses to retarget it; shipping the
 *    control there would offer a pick that can only fail.
 * 4. Each arm of the target union routes to ITS OWN endpoint. Picking the wrong
 *    arm would send a wiki question to an app's maintainer session, which no
 *    type check can catch once both ids are strings.
 *
 * Drives the real `ConfigMenu` with the real hooks, stubbing only the HTTP
 * modules — mocking the hooks would assert nothing about the wiring between the
 * picker and the endpoints, which is the part that can break.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("../api/repositories", async () => {
  const actual =
    await vi.importActual<typeof import("../api/repositories")>("../api/repositories");
  return { ...actual, listRepositories: vi.fn(), checkoutRepository: vi.fn() };
});
vi.mock("../api/apps", async () => {
  const actual = await vi.importActual<typeof import("../api/apps")>("../api/apps");
  return { ...actual, listApps: vi.fn(), openAppSession: vi.fn() };
});
vi.mock("../components/wiki/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("../components/wiki/api/client")>(
      "../components/wiki/api/client",
    );
  return { ...actual, listProjects: vi.fn(), openProjectSession: vi.fn() };
});

import * as repoApi from "../api/repositories";
import * as appsApi from "../api/apps";
import * as wikiApi from "../components/wiki/api/client";
import type { Project as WikiProject } from "../components/wiki/api/types";
import type { AppSummary } from "../types/apps";
import { ConfigMenu } from "../components/ConfigMenu";
import { openTargetSession } from "../utils/sessionTarget";

const listRepositories = vi.mocked(repoApi.listRepositories);
const listApps = vi.mocked(appsApi.listApps);
const openAppSession = vi.mocked(appsApi.openAppSession);
const listProjects = vi.mocked(wikiApi.listProjects);
const openProjectSession = vi.mocked(wikiApi.openProjectSession);

const beaconWiki: WikiProject = {
  slug: "git.example.com/acme/beacon",
  source: "gitea",
  lang: "Python",
  indexedAt: "2020-01-01T00:00:00Z",
  pages: 42,
  desc: "Fleet telemetry collector.",
};

function app(overrides: Partial<AppSummary> = {}): AppSummary {
  return {
    app_id: "app-ledger",
    title: "Ledger",
    summary: "Tracks spend across the fleet.",
    icon: "wallet",
    status: "live",
    version: 3,
    workspace_ref: { kind: "own", key: "app-ledger" },
    created_at: "2020-01-01T00:00:00Z",
    updated_at: "2020-01-02T00:00:00Z",
    ...overrides,
  };
}

const ledger = app();
const retired = app({ app_id: "app-retired", title: "Retired", status: "archived" });

function renderMenu(overrides: Partial<Parameters<typeof ConfigMenu>[0]> = {}) {
  const onSelectTarget = vi.fn();
  const onSelectProject = vi.fn();
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
      onSelectProject={onSelectProject}
      onSelectSkill={vi.fn()}
      onResetAll={vi.fn()}
      targetsEnabled
      activeTarget={null}
      onSelectTarget={onSelectTarget}
      open
      onToggleOpen={vi.fn()}
      {...overrides}
    />,
    { wrapper },
  );
  return { onSelectTarget, onSelectProject };
}

/**
 * Drill into the Project panel. The root row renames itself to "Target" once
 * one is chosen — it reports what the drill-in RESOLVES to — so match either.
 */
async function openProjectPanel(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText(/^(Project|Target)$/));
}

/** Drill in, then onto the Wiki & Apps tab. */
async function openTargetTab(user: ReturnType<typeof userEvent.setup>) {
  await openProjectPanel(user);
  await user.click(await screen.findByRole("tab", { name: /Wiki & Apps/ }));
}

beforeEach(() => {
  vi.clearAllMocks();
  listRepositories.mockResolvedValue([]);
  listProjects.mockResolvedValue([beaconWiki]);
  listApps.mockResolvedValue([ledger, retired]);
});
afterEach(cleanup);

describe("ConfigMenu Wiki & Apps tab", () => {
  it("lists indexed wiki projects and live apps under their own groups", async () => {
    const user = userEvent.setup();
    renderMenu();
    await openTargetTab(user);

    expect(await screen.findByText("Wiki")).toBeInTheDocument();
    expect(await screen.findByText(beaconWiki.slug)).toBeInTheDocument();
    expect(await screen.findByText("Apps")).toBeInTheDocument();
    expect(await screen.findByText("Ledger")).toBeInTheDocument();
  });

  it("does not offer an archived app", async () => {
    const user = userEvent.setup();
    renderMenu();
    await openTargetTab(user);

    await screen.findByText("Ledger");
    // An archived app has no maintainer session to open, so listing it would
    // route the operator into a refusal.
    expect(screen.queryByText("Retired")).not.toBeInTheDocument();
  });

  it("yields a wiki target, and never a project selection", async () => {
    const user = userEvent.setup();
    const { onSelectTarget, onSelectProject } = renderMenu();
    await openTargetTab(user);

    await user.click(await screen.findByText(beaconWiki.slug));

    expect(onSelectTarget).toHaveBeenCalledWith({
      kind: "wiki",
      slug: beaconWiki.slug,
      label: beaconWiki.slug,
    });
    // The whole design: a target is not a scope. Nothing here may reach the
    // project contract.
    expect(onSelectProject).not.toHaveBeenCalled();
  });

  it("yields an app target keyed by app_id", async () => {
    const user = userEvent.setup();
    const { onSelectTarget, onSelectProject } = renderMenu();
    await openTargetTab(user);

    await user.click(await screen.findByText("Ledger"));

    expect(onSelectTarget).toHaveBeenCalledWith({
      kind: "app",
      appId: "app-ledger",
      label: "Ledger",
    });
    expect(onSelectProject).not.toHaveBeenCalled();
  });

  it("offers a way back to an untargeted session", async () => {
    const user = userEvent.setup();
    const { onSelectTarget } = renderMenu({
      activeTarget: { kind: "app", appId: "app-ledger", label: "Ledger" },
    });
    await openTargetTab(user);

    await user.click(await screen.findByText("None"));

    expect(onSelectTarget).toHaveBeenCalledWith(null);
  });

  it("reports the chosen target on the trigger, not the project", async () => {
    renderMenu({ activeTarget: { kind: "wiki", slug: "a/b/c", label: "a/b/c" } });

    // A targeted submit never spends the project scope, so naming the project
    // there would advertise a directory the run will not use. Two sites read
    // the same label — the composer trigger and the root summary row — and
    // both must agree.
    expect((await screen.findAllByText("a/b/c")).length).toBe(2);
    expect(screen.queryByText("Temporary directory")).not.toBeInTheDocument();
  });

  it("is absent in detail mode", async () => {
    const user = userEvent.setup();
    renderMenu({ targetsEnabled: false });
    await openProjectPanel(user);

    // Not merely hidden — never rendered. The server refuses to retarget a
    // bound session, so the control has nothing it could do here.
    expect(screen.queryByRole("tab", { name: /Wiki & Apps/ })).not.toBeInTheDocument();
    // The project picker itself is untouched by the tabs being absent.
    expect(
      await screen.findByRole("option", { name: "Temporary directory" }),
    ).toBeInTheDocument();
  });

  it("costs no product read until the tab is opened", async () => {
    const user = userEvent.setup();
    renderMenu();
    await openProjectPanel(user);
    await screen.findByRole("option", { name: "Temporary directory" });

    // Radix unmounts the inactive panel, which is what makes the reads lazy —
    // the same "browsing is free" rule the Repositories group follows.
    expect(listProjects).not.toHaveBeenCalled();
    expect(listApps).not.toHaveBeenCalled();
  });
});

describe("openTargetSession", () => {
  it("routes a wiki target to the wiki project's session endpoint", async () => {
    openProjectSession.mockResolvedValue({ sessionId: "s-wiki", created: true });

    const id = await openTargetSession({
      kind: "wiki",
      slug: "git.example.com/acme/beacon",
      label: "beacon",
    });

    // No opts passed through ⇒ the endpoint's default get-or-create behaviour,
    // the same call shape the wiki gallery card's "open" button makes.
    expect(openProjectSession).toHaveBeenCalledWith("git.example.com/acme/beacon", {});
    expect(openAppSession).not.toHaveBeenCalled();
    expect(id).toBe("s-wiki");
  });

  it("routes an app target to the app's session endpoint", async () => {
    openAppSession.mockResolvedValue({ session_id: "s-app", created: false });

    const id = await openTargetSession({
      kind: "app",
      appId: "app-ledger",
      label: "Ledger",
    });

    expect(openAppSession).toHaveBeenCalledWith("app-ledger", {});
    expect(openProjectSession).not.toHaveBeenCalled();
    // The two clients spell the id differently; the caller sees one shape.
    expect(id).toBe("s-app");
  });

  // The composer (App.handleCreateAndRun) is a CREATE surface: it must not
  // silently reuse whatever session a target last produced, which is exactly
  // what the wiki card / app header's own direct calls above are FOR. Passing
  // `{requestNew: true}` is how it asks each endpoint for a genuinely new
  // session instead of the default get-or-create — `openTargetSession`'s own
  // parameter name, independent of what each route spells it on the wire.
  it("forwards a request for a genuinely new session — wiki target", async () => {
    openProjectSession.mockResolvedValue({ sessionId: "s-wiki-2", created: true });

    await openTargetSession(
      { kind: "wiki", slug: "git.example.com/acme/beacon", label: "beacon" },
      { requestNew: true },
    );

    expect(openProjectSession).toHaveBeenCalledWith(
      "git.example.com/acme/beacon",
      { requestNew: true },
    );
  });

  it("forwards a request for a genuinely new session — app target", async () => {
    openAppSession.mockResolvedValue({ session_id: "s-app-2", created: true });

    await openTargetSession(
      { kind: "app", appId: "app-ledger", label: "Ledger" },
      { requestNew: true },
    );

    expect(openAppSession).toHaveBeenCalledWith("app-ledger", { requestNew: true });
  });
});
