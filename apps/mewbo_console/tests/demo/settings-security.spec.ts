import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot settingsSecurity (mewbo-settings-02-security.jpg) — Settings › Security
 * & Access: the configured-secrets roll-up plus the issued API keys.
 *
 * This replaces a legacy hand-capture that printed a live deployment's key ids
 * and labels (internal host naming + CI topology). Both rows here are seeded
 * (`console-poc.json` -> `api_keys`) with invented ids and labels, and their
 * stored hashes are salted so a seeded key can never authenticate anything.
 *
 * Determinism note: the issued-keys list is only byte-stable because the seeder
 * writes FIXED ids and T0-relative `created_at` values — `create_key` would
 * mint a fresh uuid and wall-clock stamp per seed. See
 * `DemoSeeder._seed_api_key` for why that is a written record, not a contract
 * call.
 *
 * ## Framing
 * The docstring above promises the issued keys, so the frame has to contain
 * them. At the previous 1400x1000 it did not: the issued-keys card starts at
 * 928 and its two rows sit at 1010 and 1085, so BOTH were below the fold while
 * every assertion here stayed green — `toBeVisible()` proves a non-empty box,
 * never that the element is on screen. The artifact showed a card heading with
 * no keys under it.
 *
 * 1400x1189 is the midpoint of the gap between that card (ends 1177) and the
 * "View as JSON" disclosure below it (starts 1201), so the whole card is in
 * frame and the disclosure is not. Midpoint rather than the tightest fit,
 * because it leaves the most room for a small reflow in either direction.
 *
 * The height alone would not have prevented this and will not prevent the next
 * one; `demo.expectWithinViewport` below is what makes the failure loud.
 */
test.use({ viewport: { width: 1400, height: 1189 } });

test("settingsSecurity — secrets roll-up + issued keys", async ({ page, demo }) => {
  await page.goto("/settings?facet=security");

  // SecretsSummary and ApiKeysView are separate lazy panes that resolve
  // independently; wait for BOTH or the capture can freeze one mid-Suspense.
  await expect(page.getByText("Configured secrets")).toBeVisible();

  // A seeded key row proves the store round-trip (seed -> GET /api/keys ->
  // pane), not merely that the card shell mounted.
  for (const label of SEED.apiKeyLabels) {
    await expect(page.getByText(label, { exact: false })).toBeVisible();
  }

  // Membership alone ("these two labels are present") says nothing about a
  // THIRD key row rendering unnoticed — this shot exists precisely because a
  // legacy hand-capture leaked a real deployment's issued-key ids, so an
  // extra row here is the exact defect class to catch. Pin the count.
  await expect(
    page.locator(
      'div[class*="rounded-lg"][class*="bg-[hsl(var(--background))]"][class*="px-4"][class*="py-3"]',
    ),
  ).toHaveCount(SEED.apiKeyLabels.length);

  // The secrets roll-up must show real is-set state, not an all-"not set"
  // skeleton — `llm.api_key` is configured in demo/configs/app.json.
  await expect(page.getByText("llm.api_key")).toBeVisible();

  // The assertions above all passed at the old viewport height while the key
  // rows sat entirely below the fold. Gate the two things this shot exists to
  // show on being ON SCREEN, so a layout change ahead of them fails the run
  // instead of quietly cropping them back out.
  await demo.expectWithinViewport(
    page.getByRole("region", { name: "Configured secrets" }),
    "the secrets roll-up",
  );
  for (const label of SEED.apiKeyLabels) {
    await demo.expectWithinViewport(
      page.getByText(label, { exact: false }),
      `the ${label} key row`,
    );
  }

  await demo.capturePage("settingsSecurity");
});
