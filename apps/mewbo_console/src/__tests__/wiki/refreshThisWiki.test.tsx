/**
 * Render coverage for the wiki index-status card. One assertion runs through
 * every verdict: the card states its status with a WORD and a GLYPH (never
 * colour alone), and surfaces exactly one re-index action — primary when the
 * verdict asks for action, neutral when the wiki is current, and replaced by a
 * progress link while an index is already running.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RefreshThisWiki } from "@/components/wiki/RefreshThisWiki";
import type { ProjectFreshness } from "@/components/wiki/api/types";

const mocks = vi.hoisted(() => ({
  freshness: vi.fn(),
  active: vi.fn(),
  recoverable: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("@/components/wiki/api/hooks", () => ({
  useProjectFreshness: () => mocks.freshness(),
  useActiveIndexingJobs: () => mocks.active(),
  useRecoverableJobs: () => mocks.recoverable(),
  useRequestWikiRefresh: () => ({ mutate: mocks.refresh, isPending: false }),
}));

const SLUG = "git.example.com/acme/widgets";

function freshness(partial: Partial<ProjectFreshness>) {
  return {
    data: {
      indexedSha: null,
      remoteSha: null,
      behindBy: null,
      upToDate: null,
      checkedAt: null,
      ...partial,
    },
    isLoading: false,
  };
}

beforeEach(() => {
  mocks.freshness.mockReturnValue({ data: undefined, isLoading: false });
  mocks.active.mockReturnValue({ data: [] });
  mocks.recoverable.mockReturnValue({ data: [] });
  mocks.refresh.mockReset();
});

afterEach(cleanup);

/** The verdict live-region — the card's status readout. */
function verdict() {
  return screen.getByRole("status");
}

describe("RefreshThisWiki — verdict is a word + a glyph", () => {
  it("up to date: the word, a glyph, and a calm (neutral) re-index button", () => {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 0 }));
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "up-to-date");
    expect(within(v).getByText("Up to date")).toBeInTheDocument();
    expect(v.querySelector("svg")).toBeTruthy();
    // Calm: the action is present but does not shout.
    expect(screen.getByRole("button", { name: /re-index this wiki/i })).toBeInTheDocument();
  });

  it("behind: the counted word + glyph, attention re-index still one action", () => {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 3 }));
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "behind");
    expect(within(v).getByText("3 commits behind")).toBeInTheDocument();
    expect(v.querySelector("svg")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /re-index this wiki/i })).toHaveLength(1);
  });

  it("update available: word + glyph", () => {
    mocks.freshness.mockReturnValue(
      freshness({ behindBy: null, remoteSha: "new", indexedSha: "old" }),
    );
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "update-available");
    expect(within(v).getByText("Update available")).toBeInTheDocument();
    expect(v.querySelector("svg")).toBeTruthy();
  });

  it("indexing: states the verdict and swaps the action for a progress link", () => {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 0 }));
    mocks.active.mockReturnValue({
      data: [{ jobId: "j1", slug: SLUG, status: "scanning" }],
    });
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "indexing");
    expect(within(v).getByText("Indexing now")).toBeInTheDocument();
    // No re-index while indexing — the one action becomes "view progress".
    expect(screen.queryByRole("button", { name: /re-index this wiki/i })).toBeNull();
    expect(screen.getByRole("link", { name: /view progress/i })).toHaveAttribute(
      "href",
      expect.stringContaining("jobId=j1"),
    );
  });

  it("failed: word + glyph, re-index offered as the remedy", () => {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 0 }));
    mocks.recoverable.mockReturnValue({
      data: [{ jobId: "j1", slug: SLUG, status: "failed", recoverable: {} }],
    });
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "failed");
    expect(within(v).getByText("Last index failed")).toBeInTheDocument();
    expect(v.querySelector("svg")).toBeTruthy();
    expect(screen.getByRole("button", { name: /re-index this wiki/i })).toBeInTheDocument();
  });

  it("multiple recoverable jobs for one slug: picks the NEWEST by updatedAt, not array position", () => {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 0 }));
    mocks.recoverable.mockReturnValue({
      data: [
        // First in the array, but the OLDER attempt by updatedAt — array
        // position must not win over the timestamp.
        {
          jobId: "older",
          slug: SLUG,
          status: "cancelled",
          recoverable: {},
          updatedAt: "2024-01-01T00:00:00Z",
        },
        {
          jobId: "newer",
          slug: SLUG,
          status: "failed",
          recoverable: {},
          updatedAt: "2024-06-01T00:00:00Z",
        },
      ],
    });
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    // The later updatedAt ("failed") must win over the earlier one
    // ("cancelled"), which the old `.find()` (first match) would have picked.
    expect(within(v).getByText("Last index failed")).toBeInTheDocument();
  });

  it("a job with no updatedAt sorts oldest, matching the server's own tie rule", () => {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 0 }));
    mocks.recoverable.mockReturnValue({
      data: [
        // No stamp at all (never even cloned) — must lose to any stamped job.
        { jobId: "no-stamp", slug: SLUG, status: "failed", recoverable: {} },
        {
          jobId: "stamped",
          slug: SLUG,
          status: "cancelled",
          recoverable: {},
          updatedAt: "2024-01-01T00:00:00Z",
        },
      ],
    });
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(within(v).getByText("Last index was cancelled")).toBeInTheDocument();
  });

  it("pins the uniform-timestamp-format assumption the string compare depends on", () => {
    // The component compares `updatedAt` as a plain string, which is only
    // chronologically correct because the server has exactly ONE writer for
    // this field (`IndexingJob.format_stamp`, verified via git history to
    // spell it `strftime("%Y-%m-%dT%H:%M:%SZ")` unconditionally — always a
    // literal "Z", never a "+00:00" offset). If that ever stops holding, an
    // offset-suffixed stamp loses to a "Z"-suffixed one even at the SAME
    // instant, because "+" (0x2B) sorts below "Z" (0x5A). This test makes
    // that failure mode visible rather than only documented in a comment —
    // it earns its place even though the format is uniform today, because it
    // is what should force a second look if a caller ever starts feeding this
    // component a mixed-format `updatedAt`.
    mocks.freshness.mockReturnValue(freshness({ behindBy: 0 }));
    mocks.recoverable.mockReturnValue({
      data: [
        // Same instant as the row below, spelled with an explicit offset —
        // a shape the real server never emits for this field.
        {
          jobId: "offset-spelling",
          slug: SLUG,
          status: "cancelled",
          recoverable: {},
          updatedAt: "2024-06-01T00:00:00+00:00",
        },
        // Same instant, spelled the way the server actually does.
        {
          jobId: "z-spelling",
          slug: SLUG,
          status: "failed",
          recoverable: {},
          updatedAt: "2024-06-01T00:00:00Z",
        },
      ],
    });
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(within(v).getByText("Last index failed")).toBeInTheDocument();
  });

  it("unknown: says so honestly rather than showing a false green", () => {
    mocks.freshness.mockReturnValue({ data: undefined, isLoading: false });
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "unknown");
    expect(within(v).getByText(/freshness unknown/i)).toBeInTheDocument();
    // Explicitly NOT the green verdict.
    expect(within(v).queryByText("Up to date")).toBeNull();
  });
});

/**
 * The card is the ONLY route from the UI to a documentation-regenerating run.
 * Before this it offered one button that always resolved to the scoped path for
 * any fingerprinted project — i.e. for every healthy one — so a user whose pages
 * described deleted code had no reachable remedy at all.
 */
describe("RefreshThisWiki — choosing a refresh path", () => {
  async function openConfirm() {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 0 }));
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);
    await userEvent.click(screen.getByRole("button", { name: /re-index this wiki/i }));
  }

  it("offers both paths, each saying what it will and will not do", async () => {
    await openConfirm();

    expect(screen.getByRole("button", { name: /^refresh$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /full rebuild/i })).toBeInTheDocument();
    // The consequence, not just the label — a user choosing between them needs
    // to know which one touches prose.
    expect(screen.getByText(/rewrites no documentation/i)).toBeInTheDocument();
    expect(screen.getByText(/regenerates every page from scratch/i)).toBeInTheDocument();
  });

  it("sends mode auto for a refresh", async () => {
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: /^refresh$/i }));

    expect(mocks.refresh).toHaveBeenCalledWith(
      { slug: SLUG, mode: "auto" },
      expect.anything(),
    );
  });

  it("sends mode full for a rebuild — the path that regenerates prose", async () => {
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: /full rebuild/i }));

    expect(mocks.refresh).toHaveBeenCalledWith(
      { slug: SLUG, mode: "full" },
      expect.anything(),
    );
  });
});
