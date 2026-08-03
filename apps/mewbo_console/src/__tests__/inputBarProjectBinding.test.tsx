/**
 * InputBar — the durable project binding.
 *
 * Three defects, one composer: (1) the composer used to hydrate its project
 * display from the newest raw `context` event only, so a purpose-bound
 * session with a real, durable `spec.project` could still render "Temporary
 * directory"; (2) a purpose-bound session's project has no per-turn override
 * at all (`SessionSpec.OVERRIDABLE_WHEN_UNBOUND`), yet the picker accepted a
 * pick anyway, which the server just silently refused; (3) there was no
 * sanctioned way to change it. These tests pin: the spec is authoritative for
 * display, the picker routes a pick through the durable rebind mutation
 * instead of local state while locked, and a successful rebind updates the
 * displayed binding. Mirrors `inputBarToolLock.test.tsx`'s harness.
 *
 * The rebind route only ever BINDS (no unbind verb — see `useRebindProject`),
 * so the locked-picker tests below pick a real project from the list rather
 * than "Temporary directory", which `ConfigMenu` hides entirely while locked.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InputBar } from "@/components/InputBar";
import { AUTO_PROJECT, AUTO_PROJECT_LABEL } from "@/utils/projectLabel";
import type { ProjectSummary } from "@/api/client";
import type {
  SessionContext,
  SessionSpecBinding,
  SessionSpecEditable,
  SessionSpecResponse,
} from "@/types";

// ── mock every server hook InputBar owns; children are prop-driven ───────────
vi.mock("@/hooks/useSessionSpec", () => ({ useSessionSpec: vi.fn() }));
vi.mock("@/hooks/useRebindProject", () => ({ useRebindProject: vi.fn() }));
vi.mock("@/hooks/useMcpTools", () => ({ useMcpTools: vi.fn() }));
vi.mock("@/hooks/useSkills", () => ({ useSkills: vi.fn() }));
vi.mock("@/hooks/useProjects", () => ({ useProjects: vi.fn() }));
vi.mock("@/hooks/useModels", () => ({ useModels: vi.fn() }));
vi.mock("@/hooks/useCommands", () => ({ useCommands: vi.fn() }));
vi.mock("@/hooks/useProjectFiles", () => ({ useProjectFiles: vi.fn() }));
vi.mock("@/hooks/useProjectGit", () => ({ useProjectGit: vi.fn() }));
vi.mock("@/hooks/useContainerCompact", () => ({ useContainerCompact: vi.fn() }));

import { useSessionSpec } from "@/hooks/useSessionSpec";
import { useRebindProject } from "@/hooks/useRebindProject";
import { useMcpTools } from "@/hooks/useMcpTools";
import { useSkills } from "@/hooks/useSkills";
import { useProjects } from "@/hooks/useProjects";
import { useModels } from "@/hooks/useModels";
import { useCommands } from "@/hooks/useCommands";
import { useProjectFiles } from "@/hooks/useProjectFiles";
import { useProjectGit } from "@/hooks/useProjectGit";
import { useContainerCompact } from "@/hooks/useContainerCompact";

const OPEN_CHAT: SessionSpecBinding = {
  origin: "user",
  surface: "console",
  purpose_bound: false,
  project: null,
  slug: null,
  cwd: null,
  model: null,
  fallback_models: null,
  allowed_tools: null,
  strict_tool_scope: false,
  capabilities: null,
  skill_instructions_present: false,
  session_step_budget: null,
  mode: null,
};

const BOUND_TO_BEACON: SessionSpecBinding = {
  ...OPEN_CHAT,
  origin: "wiki",
  purpose_bound: true,
  project: "beacon",
};

const LOCKED_PROJECT: SessionSpecEditable = { project: false, model: true };

const RELAY_PROJECT: ProjectSummary = {
  name: "relay",
  path: "/srv/relay",
  source: "config",
  is_worktree: false,
};

function primeCommonHooks() {
  vi.mocked(useMcpTools).mockReturnValue({
    tools: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof useMcpTools>);
  vi.mocked(useSkills).mockReturnValue({
    skills: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof useSkills>);
  vi.mocked(useProjects).mockReturnValue({
    projects: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof useProjects>);
  vi.mocked(useModels).mockReturnValue({
    models: [],
    defaultModel: "",
    capabilities: {},
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof useModels>);
  vi.mocked(useCommands).mockReturnValue({ commands: [], loading: false });
  vi.mocked(useProjectFiles).mockReturnValue({
    files: [],
    attachments: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof useProjectFiles>);
  vi.mocked(useProjectGit).mockReturnValue({
    projectKey: null,
    gitRepo: false,
    branches: [],
    currentBranch: null,
    branchesInUse: [],
    worktrees: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
    createWorktreeFor: vi.fn(),
    deleteWorktreeFor: vi.fn(),
    mutating: false,
  } as unknown as ReturnType<typeof useProjectGit>);
  vi.mocked(useContainerCompact).mockReturnValue(false);
}

function setSpec(spec: SessionSpecBinding | null, editable: SessionSpecEditable = {}) {
  vi.mocked(useSessionSpec).mockReturnValue({
    spec,
    editable,
    source: spec ? "spec" : null,
    isLoading: false,
    error: null,
  });
}

function setRebind(
  mutate: ReturnType<typeof useRebindProject>["mutate"],
  isPending = false,
  error: Error | null = null,
) {
  vi.mocked(useRebindProject).mockReturnValue({
    mutate,
    isPending,
    error,
  } as unknown as ReturnType<typeof useRebindProject>);
}

function renderBar(opts: {
  mode: "home" | "detail";
  sessionId?: string;
  sessionContext?: SessionContext;
}) {
  const onSubmit = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = (context?: SessionContext) => (
    <QueryClientProvider client={qc}>
      <InputBar
        mode={opts.mode}
        sessionId={opts.sessionId}
        sessionContext={context}
        onSubmit={onSubmit}
      />
    </QueryClientProvider>
  );
  const { rerender } = render(tree(opts.sessionContext));
  return {
    onSubmit,
    /** Re-render with a fresh `context` payload, as a new context event would. */
    setContext: (context?: SessionContext) => rerender(tree(context)),
  };
}

/**
 * The composer's own Project readout, scoped so an open panel's Command item
 * can never stand in for the trigger label under an unscoped query.
 */
function trigger() {
  return within(screen.getByRole("button", { name: "Configure session" }));
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  primeCommonHooks();
});

describe("InputBar — durable project display", () => {
  it("renders the durable spec's project, not 'Temporary directory'", () => {
    setSpec(BOUND_TO_BEACON, { project: true });
    setRebind(vi.fn());
    renderBar({ mode: "detail", sessionId: "s1" });

    expect(screen.getByText("beacon")).toBeInTheDocument();
    expect(screen.queryByText("Temporary directory")).not.toBeInTheDocument();
  });

  it("survives a partial context event that changes only the model", () => {
    // A non-console surface can write a `context` event carrying `model` alone.
    // That moves the session-switch reset effect's deps while `project` stays
    // ABSENT, so the spec effect's own deps do NOT move — and while the reset
    // effect also wrote `activeProject`, it ran alone and blanked the pill
    // permanently. `setActiveProject` now has exactly one writer.
    setSpec(BOUND_TO_BEACON, { project: true });
    setRebind(vi.fn());
    const { setContext } = renderBar({
      mode: "detail",
      sessionId: "s1",
      sessionContext: { model: "sonnet" },
    });
    expect(trigger().getByText("beacon")).toBeInTheDocument();

    setContext({ model: "opus" });

    expect(trigger().getByText("beacon")).toBeInTheDocument();
    expect(trigger().queryByText("Temporary directory")).not.toBeInTheDocument();
  });
});

describe("InputBar — 'Reset to defaults' and the durable binding", () => {
  it("restores the bound project rather than blanking it, in detail mode", async () => {
    // There is no wire representation of "clear the project": omitting it means
    // INHERIT to `SessionSpecOverrides.from_request_context`, and while locked
    // the send omits it anyway. So a blanked pill states a change the next turn
    // cannot make, and nothing re-hydrates it.
    const user = userEvent.setup();
    setSpec(BOUND_TO_BEACON, { project: true });
    setRebind(vi.fn());
    renderBar({ mode: "detail", sessionId: "s1" });

    await user.click(screen.getByRole("button", { name: "Configure session" }));
    await user.click(await screen.findByText("Reset to defaults"));

    expect(trigger().getByText("beacon")).toBeInTheDocument();
    expect(trigger().queryByText("Temporary directory")).not.toBeInTheDocument();
  });

  it("still clears the project in home mode — a new session has none", async () => {
    const user = userEvent.setup();
    setSpec(null);
    setRebind(vi.fn());
    vi.mocked(useProjects).mockReturnValue({
      projects: [RELAY_PROJECT],
      loading: false,
      error: null,
      refresh: vi.fn(),
    } as unknown as ReturnType<typeof useProjects>);
    renderBar({ mode: "home" });

    await user.click(screen.getByRole("button", { name: "Configure session" }));
    await user.click(await screen.findByText("Project"));
    await user.click(await screen.findByText("relay"));
    expect(trigger().getByText("relay")).toBeInTheDocument();

    await user.click(await screen.findByText("Reset to defaults"));

    expect(trigger().getByText("Temporary directory")).toBeInTheDocument();
  });
});

describe("InputBar — auto-select is a project KEY, not an absence", () => {
  /** Type a line and press Enter; returns the `context` the composer assembled. */
  async function submit(
    user: ReturnType<typeof userEvent.setup>,
    onSubmit: ReturnType<typeof vi.fn>,
  ) {
    await user.click(screen.getByLabelText("Task description"));
    await user.keyboard("look around and fix the flaky test{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
    return onSubmit.mock.calls[0][1] as SessionContext;
  }

  it("sends `project: \"auto\"`, where Temporary directory omits the key", async () => {
    // The whole contract in one assertion. Omitting `project` means a plain temp
    // dir; the sentinel means a temp dir the AGENT is expected to move out of.
    // Two different requests, and the composer is the one place that decides.
    const user = userEvent.setup();
    setSpec(null);
    setRebind(vi.fn());
    const { onSubmit } = renderBar({ mode: "home" });

    await user.click(screen.getByRole("button", { name: "Configure session" }));
    await user.click(await screen.findByText("Project"));
    await user.click(await screen.findByText(AUTO_PROJECT_LABEL));

    expect(await submit(user, onSubmit)).toMatchObject({ project: AUTO_PROJECT });
  });

  it("scopes tool/skill/file lookups to NO project while auto is selected", async () => {
    // `ProjectCatalog.resolve` refuses the sentinel by design, so passing it to
    // a project-scoped endpoint asks for a key no resolver accepts. The failure
    // would be quiet — an empty list reads as "this project has no MCP tools" —
    // which is exactly why it is pinned rather than left to review.
    const user = userEvent.setup();
    setSpec(null);
    setRebind(vi.fn());
    renderBar({ mode: "home" });

    await user.click(screen.getByRole("button", { name: "Configure session" }));
    await user.click(await screen.findByText("Project"));
    await user.click(await screen.findByText(AUTO_PROJECT_LABEL));

    for (const hook of [useMcpTools, useSkills]) {
      const last = vi.mocked(hook).mock.calls.at(-1);
      expect(last?.[0]).toBeNull();
    }
    expect(vi.mocked(useProjectFiles).mock.calls.at(-1)?.[0]).toMatchObject({
      project: null,
    });
  });

  it("shows the durable auto binding on a session already in auto mode", () => {
    setSpec({ ...OPEN_CHAT, purpose_bound: true, project: AUTO_PROJECT }, LOCKED_PROJECT);
    setRebind(vi.fn());
    renderBar({ mode: "detail", sessionId: "s1" });

    expect(trigger().getByText(AUTO_PROJECT_LABEL)).toBeInTheDocument();
    expect(trigger().queryByText("Temporary directory")).not.toBeInTheDocument();
  });
});

describe("InputBar — locked project routes through the durable rebind", () => {
  it("calls the rebind mutation instead of a local pick", async () => {
    const user = userEvent.setup();
    const mutate = vi.fn();
    setSpec(BOUND_TO_BEACON, LOCKED_PROJECT);
    setRebind(mutate);
    vi.mocked(useProjects).mockReturnValue({
      projects: [RELAY_PROJECT],
      loading: false,
      error: null,
      refresh: vi.fn(),
    } as unknown as ReturnType<typeof useProjects>);
    renderBar({ mode: "detail", sessionId: "s1" });

    await user.click(screen.getByRole("button", { name: "Configure session" }));
    await user.click(await screen.findByText("Project"));
    // "Temporary directory" is hidden while locked — the route only ever
    // BINDS — so the deliberate act is picking a different real project.
    await user.click(await screen.findByText("relay"));

    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate.mock.calls[0][0]).toBe("relay");
  });

  it("updates the displayed binding after a successful rebind", async () => {
    const user = userEvent.setup();
    // The mutation's `mutate` fires the caller's `onSuccess` synchronously with
    // a fresh spec — mirrors what `useRebindProject`'s real mutation does once
    // the PUT resolves, without driving an actual network round trip here.
    const response: SessionSpecResponse = {
      session_id: "s1",
      spec: { ...BOUND_TO_BEACON, project: "relay" },
      editable: LOCKED_PROJECT,
      source: "spec",
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const mutate = vi.fn((_project: string, opts?: any) => {
      (opts?.onSuccess as ((r: SessionSpecResponse) => void) | undefined)?.(response);
    });
    setSpec(BOUND_TO_BEACON, LOCKED_PROJECT);
    setRebind(mutate);
    vi.mocked(useProjects).mockReturnValue({
      projects: [RELAY_PROJECT],
      loading: false,
      error: null,
      refresh: vi.fn(),
    } as unknown as ReturnType<typeof useProjects>);
    renderBar({ mode: "detail", sessionId: "s1" });

    const trigger = screen.getByRole("button", { name: "Configure session" });
    await user.click(within(trigger).getByText("beacon"));
    await user.click(await screen.findByText("Project"));
    await user.click(await screen.findByText("relay"));

    // The composer's TRIGGER label (not just the still-open panel's now-bold
    // row) reads the rebound project, confirming `setActiveProject` ran off
    // the response — scoped to the trigger since the panel stays open post-
    // pick and would otherwise show "relay" twice.
    expect(await within(trigger).findByText("relay")).toBeInTheDocument();
    expect(within(trigger).queryByText("beacon")).not.toBeInTheDocument();
  });

  it("surfaces a failed rebind's server-given reason, not a raw JSON blob", async () => {
    // Mirrors the shape `readJson` actually throws for the API's structured
    // refusal envelope: no top-level `message`/`detail`, so the raw body text
    // becomes `Error.message` verbatim (see `reasonFrom`, `httpBase.ts`). The
    // reason string is verified against a live route response.
    const structuredError = new Error(
      JSON.stringify({
        error: { code: 400, reason: "Project 'Ghost' not configured.", retryable: false },
      }),
    );
    const user = userEvent.setup();
    setSpec(BOUND_TO_BEACON, LOCKED_PROJECT);
    setRebind(vi.fn(), false, structuredError);
    vi.mocked(useProjects).mockReturnValue({
      projects: [RELAY_PROJECT],
      loading: false,
      error: null,
      refresh: vi.fn(),
    } as unknown as ReturnType<typeof useProjects>);
    renderBar({ mode: "detail", sessionId: "s1" });

    await user.click(screen.getByRole("button", { name: "Configure session" }));
    await user.click(await screen.findByText("Project"));

    expect(
      await screen.findByText("Project 'Ghost' not configured."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/"error":/)).not.toBeInTheDocument();
  });
});
