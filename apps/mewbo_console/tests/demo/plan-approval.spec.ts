import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot planApproval (mewbo-console-03-plan-approval.jpg) — the plan-mode
 * approval card, replacing a legacy hand-capture whose prose carried an
 * internal project codename.
 *
 * The seeded session (`console-poc.json` -> `demo-scoped-api-keys-plan`) holds
 * TWO plan revisions: revision 1 with a matching `plan_decision` (folds to
 * "Rejected") and revision 2 with none, which is what leaves the live
 * "Awaiting approval" card the shot is actually about. Both plan bodies are
 * invented feature prose.
 *
 * Anchoring note: the pending card's title is "Plan (revision 2)" — a bare
 * /Plan/ also matches the rejected card, the composer's plan-mode toggle, AND
 * (unrelated to this session) another seeded session's title, "Plan a
 * birthday dinner menu", which the rail renders alongside every session
 * page — so every locator here is role-scoped to the card's own toggle
 * BUTTON, never a bare text search. That scoping is also what keeps the
 * pending-card locator strict-mode-safe regardless of expand state: rev 2's
 * own markdown body renders an H2 reading "...implementation plan (revision
 * 2)", a case-insensitive substring hit for a plain `getByText` once
 * expanded — the accessible NAME of the toggle button is unaffected, since
 * the expandable body is a sibling of the button, not a descendant of it.
 */
test.use({ viewport: { width: 1400, height: 1000 } });

test("planApproval — rejected revision + pending revision 2", async ({ page, demo }) => {
  await demo.openSession(SEED.session.planTitle);

  // Exactly TWO plan cards (rejected rev 1 + pending rev 2) — the session
  // proposes no third revision, so a stray extra proposal (or a decision
  // that failed to fold onto its revision, leaving a duplicate pending card)
  // must fail this assertion instead of passing silently. Scoped to the
  // status-labelled toggle buttons `PlanCard` itself renders, which is what
  // keeps this from also counting the rail's unrelated "Plan a birthday
  // dinner menu" session row or the composer's plan-mode toggle.
  const planCards = page.getByRole("button", {
    name: /Rejected|Awaiting approval|Approved/,
  });
  await expect(planCards).toHaveCount(2);

  // The rejected card proves the decision FOLD ran (a mismatched revision would
  // silently leave it pending — the failure mode the bundle validator guards).
  await expect(page.getByText("Rejected").first()).toBeVisible();

  // The pending card is the subject of the shot.
  const pending = page.getByRole("button", { name: "Plan (revision 2)" });
  await expect(pending).toBeVisible();
  await expect(page.getByText("Awaiting approval").first()).toBeVisible();

  // Both cards mount COLLAPSED. The shot is worth more with the pending plan's
  // body on screen (that is what an approver actually reads), so expand it and
  // wait on a heading from the seeded markdown — proving the body rendered
  // rather than that the chevron merely rotated.
  await pending.click();
  await expect(page.getByText("Scoped API keys", { exact: false }).first()).toBeVisible();

  // openSession leaves the pointer over the clicked landing row; park it so no
  // card renders a :hover state in the still (widget-shot trap, same cause).
  await page.mouse.move(0, 0);

  await demo.capturePage("planApproval");
});
