/**
 * SessionItem — generic Continue / Restart recovery affordance (Part F).
 *
 * The buttons appear only when the backend marks the session ``recoverable``
 * and it is not running; clicking them POSTs the matching recover action via
 * the shared ``useRecoverSession`` hook (Continue → "continue", Restart →
 * "retry") and stops row-click propagation.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionItem } from "../SessionItem";
import {
  AUTO_PROJECT,
  AUTO_PROJECT_LABEL,
  ProjectLabel,
} from "../../utils/projectLabel";
import type { SessionSummary } from "../../types";
import * as client from "../../api/client";

vi.mock("../../api/client", () => ({
  recoverSession: vi.fn().mockResolvedValue({
    session_id: "s1",
    action: "continue",
    accepted: true,
    run_id: "s1:r1",
  }),
}));

const recoverSession = vi.mocked(client.recoverSession);

function renderItem(
  session: SessionSummary,
  onClick = vi.fn(),
  extra: { onArchive?: () => void; onUnarchive?: () => void; onPin?: () => void; onUnpin?: () => void } = {},
) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const { hook } = memoryLocation();
  const ui: ReactElement = (
    <QueryClientProvider client={qc}>
      <Router hook={hook}>
        <SessionItem
          session={session}
          projectLabel={new ProjectLabel([])}
          onClick={onClick}
          {...extra}
        />
      </Router>
    </QueryClientProvider>
  );
  return { onClick, ...render(ui) };
}

const base: SessionSummary = {
  session_id: "s1",
  title: "Crashed session",
  status: "failed",
};

afterEach(cleanup);
beforeEach(() => {
  recoverSession.mockClear();
});

describe("SessionItem — recovery affordance", () => {
  it("hides Continue / Restart when the session is not recoverable", () => {
    renderItem({ ...base, recoverable: false });
    expect(screen.queryByRole("button", { name: /continue/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /restart/i })).toBeNull();
  });

  it("hides Continue / Restart while the session is running", () => {
    renderItem({ ...base, recoverable: true, running: true });
    expect(screen.queryByRole("button", { name: /continue/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /restart/i })).toBeNull();
  });

  it("shows both buttons when recoverable and idle", () => {
    renderItem({ ...base, recoverable: true });
    expect(screen.getByRole("button", { name: /continue/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /restart/i })).toBeInTheDocument();
  });

  it("Continue dispatches action 'continue' and does not open the row", async () => {
    const user = userEvent.setup();
    const { onClick } = renderItem({ ...base, recoverable: true });
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await waitFor(() =>
      expect(recoverSession).toHaveBeenCalledWith("s1", "continue", undefined, undefined, undefined),
    );
    expect(onClick).not.toHaveBeenCalled();
  });

  it("Restart dispatches action 'retry'", async () => {
    const user = userEvent.setup();
    renderItem({ ...base, recoverable: true });
    await user.click(screen.getByRole("button", { name: /restart/i }));
    await waitFor(() =>
      expect(recoverSession).toHaveBeenCalledWith("s1", "retry", undefined, undefined, undefined),
    );
  });
});

describe("SessionItem — capability & workspace transparency", () => {
  it("renders a chip per scoped capability and the workspace id", () => {
    renderItem({ ...base, capabilities: ["scg"], workspace: "ws-7870f5ab" });
    expect(screen.getByText("scg")).toBeInTheDocument();
    expect(screen.getByText("ws-7870f5ab")).toBeInTheDocument();
  });

  it("renders no capability/workspace chips for a plain session", () => {
    renderItem({ ...base, capabilities: [], workspace: null });
    expect(screen.queryByText("scg")).toBeNull();
    expect(screen.queryByText(/^ws-/)).toBeNull();
  });

  it("renders a known capability id as its human label, not the raw id", () => {
    renderItem({ ...base, capabilities: ["stlite"] });
    expect(screen.getByText("Widget")).toBeInTheDocument();
    expect(screen.queryByText("stlite")).toBeNull();
  });

  it("falls back to the raw id for an unknown capability", () => {
    renderItem({ ...base, capabilities: ["some_future_cap"] });
    expect(screen.getByText("some_future_cap")).toBeInTheDocument();
  });
});

describe("SessionItem — diff stat", () => {
  it("renders +additions and -deletions when the session has a diff_stat", () => {
    renderItem({ ...base, diff_stat: { additions: 12, deletions: 3 } });
    expect(screen.getByText("+12")).toBeInTheDocument();
    expect(screen.getByText("-3")).toBeInTheDocument();
  });

  it("renders neither when diff_stat is absent", () => {
    renderItem({ ...base });
    expect(screen.queryByText(/^\+\d/)).toBeNull();
    expect(screen.queryByText(/^-\d/)).toBeNull();
  });

  it("renders neither for a zero/zero diff_stat", () => {
    renderItem({ ...base, diff_stat: { additions: 0, deletions: 0 } });
    expect(screen.queryByText(/^\+\d/)).toBeNull();
    expect(screen.queryByText(/^-\d/)).toBeNull();
  });
});

describe("SessionItem — the auto-select row label", () => {
  // The landing-page row reads through the same `ProjectLabel` resolver the
  // header and the composer do, so naming the sentinel in one place named it
  // in three. Pinned here because the row is the surface a user scans FIRST,
  // and "auto" sitting beside a fork glyph reads as a registered project.
  it("names auto-select rather than printing the wire token", () => {
    renderItem({ ...base, context: { project: AUTO_PROJECT } });
    expect(screen.getByText(AUTO_PROJECT_LABEL)).toBeInTheDocument();
    expect(screen.queryByText(AUTO_PROJECT)).toBeNull();
  });

  it("names the concrete project once the agent has switched", () => {
    renderItem({ ...base, context: { project: "relay" } });
    expect(screen.getByText("relay")).toBeInTheDocument();
    expect(screen.queryByText(AUTO_PROJECT_LABEL)).toBeNull();
  });
});

describe("SessionItem — pin/unpin", () => {
  it("renders no pin control when neither handler is supplied", () => {
    renderItem({ ...base });
    expect(screen.queryByRole("button", { name: /pin session/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /unpin session/i })).toBeNull();
  });

  it("shows 'Pin session' for an unpinned row and calls onPin, not opening the row", async () => {
    const user = userEvent.setup();
    const onPin = vi.fn();
    const { onClick } = renderItem({ ...base, pinned: false }, vi.fn(), { onPin, onUnpin: vi.fn() });
    const button = screen.getByRole("button", { name: /pin session/i });
    await user.click(button);
    expect(onPin).toHaveBeenCalledWith("s1");
    expect(onClick).not.toHaveBeenCalled();
  });

  it("shows 'Unpin session' for a pinned row and calls onUnpin", async () => {
    const user = userEvent.setup();
    const onUnpin = vi.fn();
    renderItem({ ...base, pinned: true, pinned_at: "2026-01-01T00:00:00+00:00" }, vi.fn(), {
      onPin: vi.fn(),
      onUnpin,
    });
    await user.click(screen.getByRole("button", { name: /unpin session/i }));
    expect(onUnpin).toHaveBeenCalledWith("s1");
  });

  it("renders a quiet Pinned marker only when pinned", () => {
    renderItem({ ...base, pinned: true, pinned_at: "2026-01-01T00:00:00+00:00" });
    expect(screen.getByLabelText("Pinned")).toBeInTheDocument();
    cleanup();
    renderItem({ ...base, pinned: false });
    expect(screen.queryByLabelText("Pinned")).toBeNull();
  });
});
