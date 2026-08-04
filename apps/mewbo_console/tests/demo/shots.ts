import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * The ONE module that knows where captured images land and what the seeded data
 * is called. Specs import from here; nothing else hardcodes a path or a title.
 */

// __dirname equivalent for this ESM package ("type":"module"):
//   apps/mewbo_console/tests/demo/
const HERE = dirname(fileURLToPath(import.meta.url));
// Four up: tests/demo -> tests -> mewbo_console -> apps -> <repo root>, then into
// the docs asset SOURCE dir. Resolved absolutely so the run is invariant to the
// cwd Playwright was launched from.
//
// ⚠️ This is `img-src`, NOT `img`. A capture is a SOURCE; the file the docs
// reference is derived from it by `mewbo_demo_framer` (`make demo-frame`),
// which composites full-window captures onto a 16:9 wallpaper canvas. Point
// this back at `img` and a raw capture overwrites its own published artifact,
// silently reverting the frame on whichever shots that run touched.
const IMG_DIR = resolve(HERE, "../../../../docs/assets/img-src");

export type ShotName =
  // console
  | "front"
  | "tasks"
  | "shellLog"
  | "fileReadLog"
  | "fileEdit"
  | "widgets"
  // wiki
  | "wikiLanding"
  | "wikiOverview"
  | "wikiIndexSource"
  | "wikiIndexGeneration"
  | "wikiIndexScope"
  | "wikiBadge"
  | "wikiQna"
  | "wikiIndexingProgress"
  // agentic search
  | "searchLanding"
  | "searchResults"
  | "searchAgentTrace"
  // settings + plan mode
  | "settingsModels"
  | "settingsSecurity"
  // Two Settings facets whose files carry CONSOLE numbering, because they
  // replace hand-captures of the retired standalone `/plugins` and `/projects`
  // pages. The routes now redirect into `/settings?facet=`; the basenames stay
  // so no docs reference moves.
  | "settingsPlugins"
  | "settingsProjects"
  | "planApproval"
  // ask-user questions (the human-in-the-loop card, docs/features-builtin-tools.md)
  | "askUserQuestion";

/**
 * Overwrite the docs images IN PLACE: same basenames and extensions the docs
 * `<img>` / video-poster tags already reference (verified in docs/). Console
 * 01/02/07 and the wiki indexing-progress shot are PNG; every other capture
 * keeps the JPEG the hand-authored original used (docs render these <=720px
 * wide, and JPEG-at-90 is byte-deterministic given an identical raster + the
 * pinned Playwright encoder — the same property the console closeups already
 * rely on).
 */
export const SHOTS: Record<ShotName, { file: string; type: "png" | "jpeg" }> = {
  front: { file: "mewbo-console-01-front.png", type: "png" },
  tasks: { file: "mewbo-console-02-tasks.png", type: "png" },
  shellLog: { file: "mewbo-console-shell-log.jpg", type: "jpeg" },
  fileReadLog: { file: "mewbo-console-file-read-log.jpg", type: "jpeg" },
  fileEdit: { file: "mewbo-console-04-file-edit.jpg", type: "jpeg" },
  widgets: { file: "mewbo-console-07-widgets.png", type: "png" },
  wikiLanding: { file: "mewbo-wiki-01-landing.jpg", type: "jpeg" },
  wikiOverview: { file: "mewbo-wiki-02-overview.jpg", type: "jpeg" },
  wikiIndexSource: { file: "mewbo-wiki-04-index-source.jpg", type: "jpeg" },
  wikiIndexGeneration: { file: "mewbo-wiki-05-index-generation.jpg", type: "jpeg" },
  wikiIndexScope: { file: "mewbo-wiki-06-index-scope.jpg", type: "jpeg" },
  wikiBadge: { file: "mewbo-wiki-07-badge.jpg", type: "jpeg" },
  wikiQna: { file: "mewbo-wiki-08-qna.jpg", type: "jpeg" },
  wikiIndexingProgress: {
    file: "mewbo-wiki-09-indexing-progress.png",
    type: "png",
  },
  searchLanding: { file: "mewbo-search-01-landing.jpg", type: "jpeg" },
  searchResults: { file: "mewbo-search-02-results.jpg", type: "jpeg" },
  searchAgentTrace: { file: "mewbo-search-04-agent-trace.jpg", type: "jpeg" },
  settingsModels: { file: "mewbo-settings-01-models.jpg", type: "jpeg" },
  settingsSecurity: { file: "mewbo-settings-02-security.jpg", type: "jpeg" },
  settingsPlugins: { file: "mewbo-console-05-plugins.png", type: "png" },
  settingsProjects: { file: "mewbo-console-06-projects.png", type: "png" },
  planApproval: { file: "mewbo-console-03-plan-approval.jpg", type: "jpeg" },
  askUserQuestion: { file: "mewbo-console-ask-user-log.jpg", type: "jpeg" },
};

export function shotPath(name: ShotName): string {
  return resolve(IMG_DIR, SHOTS[name].file);
}

// High enough that syntax-highlight edges stay crisp; matches the source
// rasters' apparent quality. (docs render these at <=720px CSS width anyway.)
export const JPEG_QUALITY = 90;

/**
 * The canonical seeded data this harness captures against (owned by the
 * seeder). These strings are the contract with the seeder: the
 * flows anchor on them, so a title/command/path change is the intended
 * "the demo is stale" signal (script breakage == staleness).
 */
export const SEED = {
  session: {
    // The session whose detail view backs shot 02 and whose two tool steps back
    // the shell-log / file-read-log closeups.
    authRefactorTitle: "Refactor the auth middleware",
    infraReportTitle: "Weekly infra health report",
    dinnerTitle: "Plan a birthday dinner menu",
    // The session whose rendered stlite widget backs the widgets shot (07).
    widgetTitle: "Trending acme repositories",
    // The rank-#1 repo in the widget's seeded data.json — the bare bar COUNT
    // (below) only proves 6 cards exist, not that they name the right
    // repositories, so the widget flow also anchors on this text.
    widgetTopRepo: "acme/dns-adblock",
    // The two-revision plan session backing the plan-approval shot (03).
    planTitle: "Scope API keys to one workspace",
    // The three-question-group session backing the ask-user shot.
    askUserTitle: "Cut over the ingest queue",
  },
  // The ask-user session's question headers, in transcript order: a group whose
  // run timed out, a multi-select group with a notes box, and a bounded group
  // still awaiting an answer. Each is the `header` chip of a seeded
  // `user_question` group in `console-poc.json`.
  questionHeaders: {
    timedOut: "Backfill depth",
    multiSelect: "First wave",
    bounded: "Fallback window",
  },
  // The seeded `notes_placeholder` — its presence is what makes the console
  // render the group-level notes box at all.
  questionNotesPlaceholder:
    "Anything about producer owners or freeze windows I should know?",
  // Shell tool step inside the auth-refactor session.
  shellCommand: "npm test",
  // File-read tool step inside the auth-refactor session.
  readPath: "src/middleware/auth.ts",
  readFile: "auth.ts",
  // Issued API keys backing the Security settings shot. These labels are the
  // seed↔flow contract (bundle `api_keys[].label`); they are invented demo
  // strings and must never carry a real deployment's key naming.
  apiKeyLabels: ["laptop-cli", "ci-pipeline (nightly docs build)"],
  // The fictional proxy base rendered by the Models settings shot — mirrors
  // `demo/configs/app.json` -> llm.api_base. Never a real internal host.
  llmApiBase: "https://llm.example.com/v1",
  // The Plugins facet's two lists. Unlike every other entry here these are NOT
  // bundle-seeded: `demo/plugins/` is a fixture install cache mounted read-only
  // into the api (`docker-compose.demo.yml`), shaped exactly like the one
  // `install_plugin` writes. So this is the seed<->flow contract with that
  // fixture's `installed_plugins.json` and its marketplace catalog clone.
  //
  // The two lists are DISJOINT on purpose: a marketplace row whose plugin is
  // already installed renders no Install button, so an overlap would silently
  // change the Install-button count the flow anchors on.
  plugins: {
    installed: [
      "superpowers",
      "feature-dev",
      "code-simplifier",
      "code-review",
      "claude-md-management",
      "playwright",
    ],
    marketplace: [
      "agent-sdk-dev",
      "claude-code-setup",
      "frontend-design",
      "hookify",
      "plugin-dev",
      "skill-creator",
    ],
  },
  // Managed projects backing the Workspace settings shot (`console-poc.json`
  // -> `projects`). The last entry is a WORKTREE, and its rendered name is not
  // authored anywhere: the seeder derives it from core's own worktree rules, so
  // the row is titled after the branch. Spelled here as the store renders it.
  managedProjects: [
    "ledger-sync",
    "shipment-router",
    "storefront-checkout",
    "depot-telemetry",
    "mewbo/main-9f2c1a",
  ],
} as const;
