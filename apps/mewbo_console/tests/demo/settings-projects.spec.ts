import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot settingsProjects (mewbo-console-06-projects.png) — Settings › Workspace,
 * the managed-projects pane.
 *
 * This replaces a hand-capture of the retired standalone `/projects` page and
 * its deleted top NavBar. Managed projects are a pane of the Workspace facet
 * now (`/projects` redirects to `?facet=workspace`), so the shot has to show
 * the NavRail as the only primary navigation.
 *
 * Every row is seeded (`console-poc.json` -> `projects`) through core's own
 * `VirtualProject`, with explicit ids and T0-relative stamps, because
 * `create_project` mints a uuid4 off the wall clock and would break byte
 * stability. The last row is a WORKTREE: its id, path, name and description are
 * all derived by the seeder from core's worktree rules rather than authored, so
 * it renders exactly what the product would write.
 *
 * ## What the worktrees panel shows, and why only one project has one
 * `ProjectCard` mounts `WorktreesPanel` for every non-worktree project, and it
 * renders either a real worktree list or "Not a git repository, so worktrees
 * are unavailable." — the answer to a live `GET /v_projects/<id>/branches`
 * against the project's path on disk.
 *
 * `demo/init-git-fixture.sh` makes `shipment-router` ALONE a real repository
 * with a real worktree, at the exact path and branch the seeder stored, so
 * that card shows the populated panel while the other three keep the honest
 * unavailable line. The mix is deliberate: the pane's own description promises
 * "the git worktrees it opens for parallel sessions", and a shot whose seeded
 * worktree row sat beneath a parent claiming worktrees were unavailable
 * contradicted it. Showing BOTH states documents more than either alone.
 * Nothing is stubbed — drop the fixture and the shot honestly returns to four
 * unavailable lines.
 *
 * ## Framing
 * 1400x1326 is the midpoint of the gap between the managed-projects card (ends
 * 1318) and the schema-driven `projects` section below it (starts 1334), so the
 * capture holds the whole pane — all five rows, no row cut — and stops before
 * that section's "No entries configured" empty state, which would read as an
 * unpopulated surface in a docs still.
 *
 * The height is measured, not chosen, so re-measure it rather than nudging it;
 * it has already moved twice for reasons nudging would never have found. It was
 * 1161 while the shell rendered a persistent "Settings are read-only" alert
 * above every facet, roughly 106px tall. It became 1055 when that alert was
 * removed from the product. It is 1326 now because the git fixture gives
 * `shipment-router` a populated worktrees panel, growing that one card by
 * ~271px. Anything that changes a card's height, or what sits above the pane,
 * moves this number.
 */
test.use({ viewport: { width: 1400, height: 1326 } });

test("settingsProjects — managed workspaces + worktrees", async ({ page, demo }) => {
  await page.goto("/settings?facet=workspace");

  // `SettingsCard` renders a `<section aria-labelledby>`, so the pane is a
  // `region` named by its title. Anchoring on the card (not the "Workspace"
  // facet heading) also keeps this clear of the schema section named
  // "Projects" that renders below it.
  const managed = page.getByRole("region", { name: "Managed projects" });
  await expect(managed).toBeVisible();

  // Cardinality before membership. A pane that renders EXTRA rows satisfies
  // every toBeVisible below, so the count is the only assertion that can see
  // contamination — and this pane is exactly where it would show up, since
  // `/api/projects` returns the union of config-defined and managed entries and
  // only the managed ones belong here.
  //
  // Counted off `ProjectCard`'s per-row delete button, whose accessible name
  // carries the project name. Preferring an affordance that already exists to
  // adding a testid for the capture.
  const rows = page.getByRole("button", { name: /^Delete project / });
  await expect(rows).toHaveCount(SEED.managedProjects.length);

  // Membership: naming every row proves these are the seeded workspaces and not
  // whatever the api had lying around. A bundle rename is meant to break this.
  for (const name of SEED.managedProjects) {
    await expect(
      page.getByRole("button", { name: `Delete project ${name}` }),
    ).toBeVisible();
  }

  // The two things the docs page promises of this shot are a description and a
  // working directory per row. Both are what a bare `/api/projects` payload
  // would drop first, so pin one of each rather than trusting the row count.
  await expect(
    page.getByText("Nightly reconciliation between the billing ledger", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("/workspaces/acme/ledger-sync", { exact: true }),
  ).toBeVisible();

  // The worktrees panel, both halves. If `init-git-fixture.sh` fails, the api
  // answers `git_repo: false` for every project and the pane silently reverts
  // to four unavailable lines — a shot that still passes every assertion above
  // while losing the capability it exists to document. Pin both counts.
  await expect(
    page.getByRole("button", { name: "Delete worktree mewbo/main-9f2c1a" }),
  ).toHaveCount(1);
  await expect(
    page.getByText("Not a git repository, so worktrees are unavailable."),
  ).toHaveCount(3);

  // `is_clean` returns false for a path that does not exist, which renders an
  // amber "Uncommitted" badge on the row. Its absence is what proves the
  // worktree is a real checkout rather than a store record pointing at nothing.
  await expect(page.getByText("Uncommitted", { exact: true })).toHaveCount(0);

  // The empty state and the populated list are mutually exclusive branches of
  // the pane, and the empty one is what a failed seed renders — a plausible,
  // fully-laid-out card that no count assertion above would catch if the seed
  // wrote nothing AND the count expectation were ever relaxed. Assert it is
  // absent, so "screenshot of nothing" can only ship past a red run.
  await expect(page.getByText("No managed projects yet.")).toBeHidden();

  await page.mouse.move(0, 0);
  await demo.capturePage("settingsProjects");
});
