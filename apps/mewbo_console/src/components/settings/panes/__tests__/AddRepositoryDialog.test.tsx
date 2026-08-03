/**
 * AddRepositoryDialog — the register form.
 *
 * These tests pin the two things the surface exists to guarantee: the submit is
 * a single POST that starts nothing, and the body carries only the four fields
 * the client owns (sending a server-owned `slug`/`platform`/`usage` is a 400, so
 * a form that quietly grew one would fail in production and nowhere else).
 * The rest cover the previews and the error routing, which are what make the
 * form usable before the server has seen the URL.
 */
import { cleanup, render as rtlRender, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { AddRepositoryDialog } from "../repositories/AddRepositoryDialog";
import * as wikiHooks from "../../../wiki/api/hooks";
import * as credentialHooks from "../../../../hooks/useGitCredentials";
import * as repositoryHooks from "../../../../hooks/useRepositories";
import { RepositoryError, type RepositoryDTO } from "../../../../api/repositories";
import type { GitCredentialSummary } from "../../../../api/git";

vi.mock("../../../wiki/api/hooks", () => ({ useWikiPlatforms: vi.fn() }));
vi.mock("../../../../hooks/useGitCredentials", () => ({ useGitCredentials: vi.fn() }));
vi.mock("../../../../hooks/useRepositories", () => ({ useCreateRepository: vi.fn() }));

const useWikiPlatforms = vi.mocked(wikiHooks.useWikiPlatforms);
const useGitCredentials = vi.mocked(credentialHooks.useGitCredentials);
const useCreateRepository = vi.mocked(repositoryHooks.useCreateRepository);

const REGISTERED: RepositoryDTO = {
  slug: "git.example.com/acme/orbit",
  host: "git.example.com",
  owner: "acme",
  repo: "orbit",
  repoUrl: "https://git.example.com/acme/orbit",
  platform: "gitea",
  defaultBranch: null,
  name: null,
  description: null,
  origin: "manual",
  createdAt: "2026-07-01T00:00:00Z",
  updatedAt: "2026-07-01T00:00:00Z",
  usage: { wiki: null, tasks: null, credential: null },
};

const HOST_CRED: GitCredentialSummary = {
  scope: "git.example.com",
  scopeType: "host",
  kind: "token",
  username: null,
  valueHint: "…aaaa",
  updatedAt: null,
};

type MutateOptions = {
  onSuccess?: (repository: RepositoryDTO) => void;
  onError?: (error: unknown) => void;
};

let mutate: ReturnType<typeof vi.fn>;
let onRegistered: ReturnType<typeof vi.fn>;

/** Mount `useCreateRepository` so `mutate` resolves with *repository*. */
function mockCreateSucceeds(repository: RepositoryDTO = REGISTERED) {
  mutate = vi.fn();
  mutate.mockImplementation((_input: unknown, options?: MutateOptions) => {
    options?.onSuccess?.(repository);
  });
  useCreateRepository.mockReturnValue({
    mutate,
    reset: vi.fn(),
    isPending: false,
    error: null,
  } as unknown as ReturnType<typeof repositoryHooks.useCreateRepository>);
}

/** Mount `useCreateRepository` so `mutate` rejects with *error*. */
function mockCreateFails(error: unknown) {
  mutate = vi.fn();
  mutate.mockImplementation((_input: unknown, options?: MutateOptions) => {
    options?.onError?.(error);
  });
  useCreateRepository.mockReturnValue({
    mutate,
    reset: vi.fn(),
    isPending: false,
    error,
  } as unknown as ReturnType<typeof repositoryHooks.useCreateRepository>);
}

function render(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function open() {
  return render(
    <AddRepositoryDialog open onOpenChange={vi.fn()} onRegistered={onRegistered} />,
  );
}

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  onRegistered = vi.fn();
  mockCreateSucceeds();
  useGitCredentials.mockReturnValue({
    credentials: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  });
  useWikiPlatforms.mockReturnValue({
    data: [
      {
        id: "gitea",
        name: "Gitea",
        mono: "",
        color: "",
        short: "",
        hosts: ["gitea.com"],
        tokenLabel: "",
        tokenScope: "",
        tokenUrl: null,
        tokenSteps: [],
      },
      // The real catalogue always ships this card with NO hosts — it is what an
      // unrecognised host resolves to, and the preview must be able to name it.
      {
        id: "git",
        name: "Generic Git",
        mono: "",
        color: "",
        short: "",
        hosts: [],
        tokenLabel: "",
        tokenScope: "",
        tokenUrl: null,
        tokenSteps: [],
      },
    ],
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof wikiHooks.useWikiPlatforms>);
});

describe("AddRepositoryDialog", () => {
  test("previews the canonical identity and the platform as the URL is typed", async () => {
    const user = userEvent.setup();
    open();

    await user.type(screen.getByLabelText("Repository URL"), "https://gitea.com/acme/orbit");

    expect(screen.getByText("gitea.com/acme/orbit")).toBeInTheDocument();
    expect(screen.getByText("on Gitea")).toBeInTheDocument();
  });

  test("a self-hosted host previews the platform the server will actually store", async () => {
    // Regression: the preview used to run the WIZARD's guess, whose `git.`
    // hostname heuristic read this as Gitea, while the registry stored `git`.
    // A tile the user can click away from may guess; a line that says "will be
    // registered as" may not.
    const user = userEvent.setup();
    open();

    await user.type(
      screen.getByLabelText("Repository URL"),
      "https://git.example.com/acme/orbit",
    );

    expect(screen.getByText("git.example.com/acme/orbit")).toBeInTheDocument();
    expect(screen.getByText("on Generic Git")).toBeInTheDocument();
    expect(screen.queryByText("on Gitea")).not.toBeInTheDocument();
  });

  test("a partial URL says so instead of guessing an identity", async () => {
    const user = userEvent.setup();
    open();

    await user.type(screen.getByLabelText("Repository URL"), "git.example.com");

    expect(
      screen.getByText(/Enter the full URL, including the host and the owner/i),
    ).toBeInTheDocument();
  });

  test("submits only the client-owned fields, omitting the optional ones left blank", async () => {
    const user = userEvent.setup();
    open();

    await user.type(
      screen.getByLabelText("Repository URL"),
      "https://git.example.com/acme/orbit",
    );
    await user.click(screen.getByRole("button", { name: "Register repository" }));

    expect(mutate).toHaveBeenCalledTimes(1);
    // Exactly the POST body the contract accepts: `slug`, `platform` and
    // `usage` are server-owned, and sending one is a 400.
    expect(mutate.mock.calls[0][0]).toEqual({
      repoUrl: "https://git.example.com/acme/orbit",
    });
  });

  test("carries an entered default branch and display name through, trimmed", async () => {
    const user = userEvent.setup();
    open();

    await user.type(
      screen.getByLabelText("Repository URL"),
      "  https://git.example.com/acme/orbit  ",
    );
    await user.type(screen.getByLabelText("Default branch (optional)"), "develop");
    await user.type(screen.getByLabelText("Display name (optional)"), "Orbit Control");
    await user.click(screen.getByRole("button", { name: "Register repository" }));

    expect(mutate.mock.calls[0][0]).toEqual({
      repoUrl: "https://git.example.com/acme/orbit",
      defaultBranch: "develop",
      name: "Orbit Control",
    });
  });

  test("reports the registered record and the credential opt-in to the caller", async () => {
    const user = userEvent.setup();
    open();

    await user.type(
      screen.getByLabelText("Repository URL"),
      "https://git.example.com/acme/orbit",
    );
    await user.click(
      screen.getByRole("switch", { name: /store a credential for this repository/i }),
    );
    await user.click(screen.getByRole("button", { name: "Register repository" }));

    // The slug handed on is the SERVER's, never one re-derived from the typed
    // URL, so a credential scope can't disagree with the repository.
    expect(onRegistered).toHaveBeenCalledWith(REGISTERED, true);
  });

  test("a stored credential that already covers the repository replaces the opt-in with a hint", async () => {
    useGitCredentials.mockReturnValue({
      credentials: [HOST_CRED],
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    const user = userEvent.setup();
    open();

    await user.type(
      screen.getByLabelText("Repository URL"),
      "https://git.example.com/acme/orbit",
    );

    expect(screen.getByText(/already\s+reaches this repository/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("switch", { name: /store a credential/i }),
    ).not.toBeInTheDocument();
  });

  test("a duplicate registration pins its message to the URL field, not the banner", async () => {
    mockCreateFails(
      new RepositoryError("repository_exists", "Already registered as acme/orbit.", 409),
    );
    const user = userEvent.setup();
    open();

    await user.type(
      screen.getByLabelText("Repository URL"),
      "https://git.example.com/acme/orbit",
    );
    await user.click(screen.getByRole("button", { name: "Register repository" }));

    expect(await screen.findByText("Already registered as acme/orbit.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(onRegistered).not.toHaveBeenCalled();
  });

  test("an unrecognised failure falls back to the banner", async () => {
    mockCreateFails(new Error("upstream exploded"));
    const user = userEvent.setup();
    open();

    await user.type(
      screen.getByLabelText("Repository URL"),
      "https://git.example.com/acme/orbit",
    );
    await user.click(screen.getByRole("button", { name: "Register repository" }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("upstream exploded")).toBeInTheDocument();
  });

  test("an empty URL is rejected before any request is made", async () => {
    const user = userEvent.setup();
    open();

    await user.click(screen.getByRole("button", { name: "Register repository" }));

    expect(await screen.findByText("Repository URL is required")).toBeInTheDocument();
    expect(mutate).not.toHaveBeenCalled();
  });
});
