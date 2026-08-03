import type { Locator, Page } from "@playwright/test";
import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot askUserQuestion (mewbo-console-ask-user-log.jpg) — the ask-user question
 * card, in all three states the tool can leave it in.
 *
 * The seeded session (`console-poc.json` -> `demo-ingest-queue-cutover`) holds
 * THREE `user_question` groups: "Backfill depth" carries a matching
 * `user_question_answered` with outcome `timed_out` (the run stopped waiting, so
 * the card reads "Still open" and stays a live form), "First wave" is
 * `multi_select` with a `notes_placeholder` (the group-level notes box), and
 * "Fallback window" is bounded by `timeout_seconds` (the wait hint). All three
 * question bodies are invented feature prose.
 *
 * Anchoring notes:
 *  - "First wave" appears BOTH in its header chip and inside its own question
 *    text ("...in the first wave?"), and `getByText` is a case-insensitive
 *    SUBSTRING match, so locators go through the per-question `<fieldset>`
 *    rather than the chip. Both matches live in the same fieldset, so the filter
 *    still resolves to exactly one element.
 *  - The wait hint is suppressed the moment a run stops waiting, which is why
 *    only the bounded PENDING card can prove it rendered.
 *
 * Framing: this is a CLOSEUP of the conversation column, like the shell/file-read
 * card shots the same docs page carries, not a full console still. The column is
 * capped at 820px by the console itself, so a full-viewport capture would spend
 * most of its pixels on chrome and shrink 11px option descriptions past reading
 * at the 720px the docs render at. The tall viewport is staging, not framing: the
 * whole column has to be on screen at once, since a crop can only ever clip the
 * page raster inside the viewport.
 */
test.use({ viewport: { width: 1400, height: 2000 } });

/**
 * Fail if an element is not wholly inside the captured viewport. `toBeVisible`
 * only proves a non-empty box, not that the element is on screen — and the
 * conversation pane pins itself to the latest turn, so a seed that grows by one
 * option silently scrolls the first card's header out of the frame (it did:
 * "Still open" was the first casualty). A clipped card is a wrong screenshot
 * with a green test.
 */
async function expectWithinViewport(page: Page, target: Locator, label: string) {
  const box = await target.boundingBox();
  if (!box) throw new Error(`${label}: no bounding box`);
  const viewport = page.viewportSize();
  if (!viewport) throw new Error(`${label}: no viewport size`);
  expect(box.y, `${label} is clipped at the top`).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height, `${label} is clipped at the bottom`).toBeLessThanOrEqual(
    viewport.height,
  );
}

test("askUserQuestion — timed-out, multi-select and bounded cards", async ({ page, demo }) => {
  await demo.openSession(SEED.session.askUserTitle);

  // Close the workspace pane: this session has no tool steps, so it renders an
  // empty instrument panel. Closing it re-centres the conversation column, which
  // is what the crop below is aligned on.
  await page.getByRole("button", { name: "Close panel" }).click();

  // The console's own conversation column (max-w-[820px]) IS the crop target —
  // same shape-fragment idiom as `DemoHelper.logsPane()`. If that cap ever
  // changes, this shot fails loudly instead of capturing a wrong frame.
  const column = page.locator('div[class*="max-w-[820px]"]');

  const group = (header: string) =>
    page.locator("fieldset").filter({ hasText: header });
  const timedOut = group(SEED.questionHeaders.timedOut);
  const multiSelect = group(SEED.questionHeaders.multiSelect);
  const bounded = group(SEED.questionHeaders.bounded);

  await expect(timedOut).toBeVisible();
  await expect(multiSelect).toBeVisible();
  await expect(bounded).toBeVisible();

  // The timed-out card is the frame worth having: it reads as a bug until the
  // copy says a late answer arrives as a new message. Assert BOTH halves — the
  // badge that says the question is still open, and the sentence that says why
  // the form below it is still live.
  const stillOpen = page.getByText("Still open");
  await expect(stillOpen).toBeVisible();
  await expect(page.getByText(/You can still answer/)).toBeVisible();
  await expect(timedOut.locator('input[type="radio"]')).toHaveCount(3);
  // The COUNT alone (3 radios) is satisfied by any 3 options — a seed that
  // relabels one while keeping the count still passes. Assert the seeded
  // labels themselves, scoped to this group's own fieldset.
  await expect(timedOut.getByText("Last 24 hours", { exact: false })).toBeVisible();
  await expect(timedOut.getByText("Last 7 days", { exact: false })).toBeVisible();
  await expect(timedOut.getByText("Skip the backfill", { exact: false })).toBeVisible();

  // Multi-select renders checkboxes (single-select renders radios), and the
  // notes box exists only because the group supplied a placeholder for it.
  await expect(multiSelect.locator('input[type="checkbox"]')).toHaveCount(3);
  await expect(multiSelect.getByText("Checkout events", { exact: false })).toBeVisible();
  await expect(multiSelect.getByText("Inventory sync", { exact: false })).toBeVisible();
  await expect(multiSelect.getByText("Email dispatcher", { exact: false })).toBeVisible();
  await expect(page.getByPlaceholder(SEED.questionNotesPlaceholder)).toBeVisible();

  // The bounded group is the one still pending, so it is the only card that can
  // show how long the run will wait.
  await expect(bounded.locator('input[type="radio"]')).toHaveCount(2);
  await expect(bounded.getByText("48 hours", { exact: false })).toBeVisible();
  await expect(bounded.getByText("One week", { exact: false })).toBeVisible();
  await expect(page.getByText(/waits up to 30m/)).toBeVisible();
  await expect(page.getByText("Awaiting your answer")).toHaveCount(2);

  // The closing turn is what makes the frame make sense: it says the run
  // proceeded on the timed-out question and named the two decisions it will not
  // guess. It is also the bottom of the crop.
  await expect(page.getByText(/Two decisions are still yours/)).toBeVisible();

  // The crop can only clip what the viewport already holds, so the column must
  // be on screen whole: the user turn, all three cards, and the closing turn.
  await expectWithinViewport(page, column, "the conversation column");

  // openSession leaves the pointer over the clicked landing row; park it so no
  // card renders a :hover state in the still (widget-shot trap, same cause).
  await page.mouse.move(0, 0);

  // The column carries `pb-32` so the live composer never covers the last turn.
  // The composer is not in the crop, so that clearance is 128px of dead space at
  // the bottom of the artifact — drop it for the capture. Layout-mutating on
  // purpose, same licence `settle()`'s capture stylesheet takes when it hides the
  // TurnScroller rail: a still carries neither a composer nor a scroll position.
  await column.evaluate((el) => {
    (el as HTMLElement).style.paddingBottom = "0px";
  });

  await demo.captureElement("askUserQuestion", column);
});
