import { test, expect } from "./fixtures";
import { GROVE_LANDING_PAGE_ID, GROVE_SLUG, stubFreshness, wikiPageHref } from "./wiki-helpers";

/**
 * Shot wikiOverview (02) — Grove's landing page (`grove-overview`, titled
 * "Grove Overview"): sidebar page-tree, markdown body (incl. a mermaid
 * flowchart), and the right-rail "On this page" TOC.
 *
 * Existing placeholder raster ~1920x1386; see wiki-landing.spec.ts for the
 * viewport-sizing rationale (deviceScaleFactor stays at the config default).
 */
test.use({ viewport: { width: 1900, height: 1400 } });

test("wikiOverview — Grove overview page", async ({ page, demo }) => {
  // WikiTopBar renders a FreshnessBadge on every settled in-project screen —
  // stub the outbound ls-remote probe so it never depends on real network.
  await stubFreshness(page);

  await page.goto(wikiPageHref(GROVE_LANDING_PAGE_ID, GROVE_SLUG));

  // The page title <h1> is `#page-top`; the Grove overview body ALSO starts
  // with a `# Grove Overview` markdown heading, so match the title by id.
  await expect(page.locator("#page-top")).toHaveText("Grove Overview");

  // Page tree + right-rail "On this page" TOC. The tree lives in the NavRail
  // now (`nav-rail/sections.tsx` WikiPagesSection), NOT in a wiki-owned
  // sidebar, so anchor it by the rail's own landmark and by a sibling page row.
  //
  // This replaced `page.locator("aside nav").first()`, which measured nothing:
  // `aside nav` resolves to TWO navs here — the rail's "Products" and the
  // TOC's "On this page" — and `.first()` took the rail, because AppLayout
  // renders it before <main>. So an assertion whose comment claimed to check
  // the wiki page tree was satisfied by the console's primary navigation, and
  // would pass on a blank wiki page. Asserting a page ROW is what proves the
  // tree actually bound to the seeded project; "Runtime Model" is a sibling
  // Grove page and occurs exactly once in the DOM (measured, not assumed).
  await expect(page.getByRole("navigation", { name: "Products" })).toBeVisible();
  await expect(page.getByText("Runtime Model", { exact: true })).toBeVisible();
  // Match the TOC by its landmark role, not by text: the narrow-viewport
  // trigger button carries the same "On this page" label, so a text match
  // resolves to two elements and trips strict mode. The role query names
  // `WikiToc`'s own `<nav aria-label="On this page">` and nothing else.
  await expect(page.getByRole("navigation", { name: "On this page" })).toBeVisible();

  // The overview body has one ```mermaid flowchart per the seed contract —
  // wait for the rendered diagram so the shot never races the async load.
  //
  // Assert the SVG POSITIVELY. This replaced
  // `expect(getByText("Rendering diagram…")).toHaveCount(0)`, which was
  // satisfied by the diagram never mounting at all: body markdown lost, the
  // block unmounted, or a render that threw (MermaidBlock paints an error
  // branch, also not the placeholder) all show zero placeholders. "No
  // placeholder" is not "a diagram".
  //
  // Scoped to the zoom button because MermaidBlock injects the SVG inside it,
  // and `.first()` because that subtree carries more than one <svg> node
  // (measured: 1 zoom button, 2 nested svgs) — a count here would pin an
  // internal detail of mermaid's output rather than the fact we care about.
  await expect(page.locator('button[title="Click to zoom"] svg').first()).toBeVisible();

  await demo.capturePage("wikiOverview");
});
