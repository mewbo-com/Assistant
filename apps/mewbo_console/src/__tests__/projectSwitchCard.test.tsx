/**
 * `ProjectSwitchCard` — the ONE readout for a completed `switch_project`.
 *
 * Rendered directly rather than through `ConversationTimeline`, the
 * `compactionMarker.test.tsx` precedent: mounting the timeline pulls in
 * `TurnScroller`'s `IntersectionObserver`, unpolyfilled in this suite's jsdom.
 * Both surfaces render this same component, so a test here covers both.
 *
 * What is worth locking down is the READING. A switch moves every following
 * tool call, file read and sub-agent into a different directory, so the card
 * has to name the destination in its collapsed header — a user who has to
 * expand it to learn where the run went may as well have read the raw tool
 * result. The resolved detail (path, repo, branch, the agent's reason) is the
 * part that belongs behind the fold.
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { ProjectSwitchCard } from "../components/ProjectSwitchCard";
import type { ProjectSwitchMeta } from "../types";

const FULL: ProjectSwitchMeta = {
  project: "managed:0f1e2d3c",
  name: "beacon",
  cwd: "/srv/beacon",
  repo: "git.example.com/acme/beacon",
  branch: "main",
  previous: "relay",
  previousCwd: "/srv/relay",
  projectInstructionsFound: true,
};

afterEach(cleanup);

describe("ProjectSwitchCard", () => {
  it("names the destination without being expanded", () => {
    render(<ProjectSwitchCard meta={FULL} />);
    expect(screen.getByText("beacon")).toBeInTheDocument();
  });

  it("prefers the human name over the raw key", () => {
    // The key is `managed:<uuid>` for every Mewbo-owned checkout. A card that
    // showed it would put a uuid in the conversation.
    render(<ProjectSwitchCard meta={FULL} />);
    expect(screen.queryByText(/managed:/)).not.toBeInTheDocument();
  });

  it("shows where it came FROM when the tool reported a previous key", () => {
    // A switch is a movement, and half of it is the origin. Without this the
    // card reads the same whether the session had been anywhere before or not.
    render(<ProjectSwitchCard meta={FULL} />);
    expect(screen.getByText("relay")).toBeInTheDocument();
  });

  it("still reads as a move on a first switch, which has no previous KEY", async () => {
    // `previous_project` is null before a session's first switch — the loop was
    // handed a directory, never a catalog key. The origin directory carries it.
    const user = userEvent.setup();
    render(
      <ProjectSwitchCard
        meta={{ project: "relay", cwd: "/srv/relay", previousCwd: "/tmp/scratch" }} />,
    );
    await user.click(screen.getByText("Switched to relay"));
    expect(screen.getByText("/tmp/scratch")).toBeInTheDocument();
    expect(screen.getByText("/srv/relay")).toBeInTheDocument();
  });

  it("falls back to a self-contained sentence when there is no origin", () => {
    render(<ProjectSwitchCard meta={{ project: "relay" }} />);
    expect(screen.getByText("Switched to relay")).toBeInTheDocument();
  });

  it("keeps the resolved detail behind the fold, then reveals all of it", async () => {
    const user = userEvent.setup();
    render(<ProjectSwitchCard meta={FULL} />);
    expect(screen.queryByText("/srv/beacon")).not.toBeInTheDocument();

    await user.click(screen.getByText("beacon"));

    expect(screen.getByText("/srv/beacon")).toBeInTheDocument();
    expect(screen.getByText("git.example.com/acme/beacon")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
    // The one part of a switch that changes how the agent BEHAVES, not just
    // where it works — so it is named rather than left to the trace panel.
    expect(
      screen.getByText("Project instructions from this directory are now in effect."),
    ).toBeInTheDocument();
  });

  it("stays a flat, unclickable row when the tool resolved nothing to show", async () => {
    // `LogEventCard` decides it has a body from `children != null`, and `false`
    // is not null — so a `{cond && <div/>}` body would make a detail-less card
    // clickable and open onto nothing.
    const user = userEvent.setup();
    render(<ProjectSwitchCard meta={{ project: "relay" }} />);
    const title = screen.getByText("Switched to relay");

    await user.click(title);

    expect(screen.getByText("Switched to relay")).toBeInTheDocument();
  });
});
