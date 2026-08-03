/**
 * ProjectCard — the "Open a session about this wiki" action.
 *
 * Get-or-create: clicking mints/reuses the project's maintainer session and
 * navigates to it, WITHOUT also firing the card's own `onOpen` (the click
 * lands inside a `role="button"` article, same trap `onSettings`/`onDelete`
 * already guard with `stopPropagation`).
 *
 * vitest runs WITHOUT globals → explicit cleanup.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectCard } from "./ProjectCard";
import type { Project } from "../api/types";

vi.mock("../api/client", () => ({ openProjectSession: vi.fn() }));
import * as wikiClient from "../api/client";
const openProjectSession = vi.mocked(wikiClient.openProjectSession);

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));
import { toast } from "sonner";
const toastError = vi.mocked(toast.error);

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
});

function baseProject(over: Partial<Project> = {}): Project {
  return {
    slug: "git.example.com/acme/widgets",
    source: "gitea",
    lang: "Python",
    indexedAt: "2026-07-01T00:00:00Z",
    pages: 12,
    desc: "A widget factory",
    ...over,
  };
}

function renderCard(onOpen = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const loc = memoryLocation({ path: "/wiki", record: true });
  render(
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>
        <ProjectCard project={baseProject()} onOpen={onOpen} onSettings={vi.fn()} onDelete={vi.fn()} />
      </Router>
    </QueryClientProvider>,
  );
  return { onOpen, loc };
}

describe("ProjectCard — open-session action", () => {
  it("mints/reuses the session and navigates, without triggering the card's onOpen", async () => {
    openProjectSession.mockResolvedValue({ sessionId: "sess-1", created: true });
    const { onOpen, loc } = renderCard();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Open a session about this wiki" }));
    });

    expect(openProjectSession).toHaveBeenCalledWith("git.example.com/acme/widgets");
    await waitFor(() => expect(loc.history.at(-1)).toBe("/s/sess-1"));
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("toasts on a failed mint instead of navigating", async () => {
    openProjectSession.mockRejectedValue(new Error("no such project"));
    const { loc } = renderCard();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Open a session about this wiki" }));
    });

    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1));
    expect(toastError.mock.calls[0][0]).toMatch(/no such project/);
    expect(loc.history.at(-1)).toBe("/wiki");
  });
});
