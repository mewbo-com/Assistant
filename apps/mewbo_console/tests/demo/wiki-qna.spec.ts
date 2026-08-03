import { test, expect } from "./fixtures";
import {
  ASSISTANT_OVERVIEW_PAGE_ID,
  ASSISTANT_SLUG,
  QA_ANSWER_ID,
  wikiQaSnapshotHref,
} from "./wiki-helpers";

/**
 * Shot wikiQna (08) — the stored Q&A snapshot for
 * `qa-assistant-what-is-this-for-0001` (Assistant, from `overview`,
 * "What is this project for?"). The `?answer=` deep link renders the
 * PERSISTED answer (`GET /v1/wiki/qa/<id>`) and never opens a live stream —
 * see `wiki/CLAUDE.md` "Idempotent Q&A URL" — so this shot has zero LLM
 * involvement and no stream-timing nondeterminism to fight.
 *
 * Existing placeholder raster ~1270x1261 (near-square, tall). Viewport
 * picked to roughly match; the QA screen's two-column grid needs `lg`
 * (1024px+) to render side-by-side.
 */
test.use({ viewport: { width: 1280, height: 1200 } });

test("wikiQna — stored Q&A snapshot", async ({ page, demo }) => {
  await page.goto(
    wikiQaSnapshotHref({
      answer: QA_ANSWER_ID,
      pageId: ASSISTANT_OVERVIEW_PAGE_ID,
      slug: ASSISTANT_SLUG,
    }),
  );

  await expect(
    page.getByRole("heading", { level: 1, name: "What is this project for?" }),
  ).toBeVisible();

  // Answer prose — the seeded snapshot's H2 section heading.
  await expect(page.getByRole("heading", { name: "Core Capabilities" })).toBeVisible();
  // The seeded snapshot also includes a "Repository Structure" table.
  await expect(page.getByText("Repository Structure")).toBeVisible();

  // Left-column cited-sources rail (visually uppercased via CSS; the DOM
  // text itself is mixed-case "Cited sources"). The label alone is satisfied
  // by an EMPTY rail (zero SourceCards) — it names the section, not its
  // contents — so pin the rail's SourceCard count too. The seeded answer
  // cites 5 sources (Project Overview / Agentic Search Engine / Source
  // Capability Graph (SCG) / Channels & Integrations / Mewbo API Server);
  // each renders as a `<details>` inside the rail's own `space-y-2.5` list.
  // The sticky rail container ALSO holds a sixth, unrelated `<details>` — the
  // "Retrieval details" footer accordion — as a direct child of the rail
  // itself rather than of that list (verified against a live render: an
  // unscoped `details` count under the rail resolves to 6, not 5), so the
  // list wrapper is the scope, not the rail.
  await expect(page.getByText(/cited sources/i)).toBeVisible();
  await expect(
    page.locator(
      'div[class*="max-h-[calc(100vh-3rem)]"] div[class*="space-y-2.5"] > details',
    ),
  ).toHaveCount(5);

  await demo.capturePage("wikiQna");
});
