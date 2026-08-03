import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot 04-file-edit — an element (locator) screenshot of the single expanded
 * file-edit tool card (DiffCard) inside the auth-refactor session: the edit to
 * src/middleware/auth.ts with its unified diff shown. Docs alt: "a file edit
 * shown as a unified diff". The reference raster is an EXPANDED card (it ends in
 * a "Collapse" footer), so expand a large diff before capturing.
 *
 * SEED.readFile ('auth.ts') is the same basename the read card uses; the DiffCard
 * renders it in the file header, and its success accent (border-l-agent-4)
 * distinguishes it from the read card (agent-1) and the shell card (border-l-[3px]).
 */
test.use({ viewport: { width: 1650, height: 1191 } });

test("file-edit — expanded diff card", async ({ demo }) => {
  await demo.openSession(SEED.session.authRefactorTitle);
  await demo.ensureLogsTab();

  const card = demo.card("border-l-agent-4", SEED.readFile);
  await expect(card).toBeVisible();

  await demo.expandDiff(card);

  // expandDiff proves the "Click to expand" footer went away, not that the
  // diff BODY it was hiding actually rendered — a truncated/empty body would
  // satisfy that alone. Assert content from the FIRST hunk and — since the
  // seeded diff spans two hunks with a `@@ -15,5 +26,11 @@` second header —
  // content near the END of the second hunk too, so a diff that expanded but
  // stopped rendering partway through still fails this. The bare substring
  // `apiKeyUser` is a strict-mode trap: it appears in BOTH the hunk-1
  // function declaration and the hunk-2 call site (`const service =
  // apiKeyUser(apiKey);`) — verified against a live render, not assumed —
  // so anchor on the full hunk-1 declaration line, which only that line
  // matches.
  await expect(card.getByText("function apiKeyUser(apiKey: string)")).toBeVisible();
  await expect(card.getByText("req.user = service")).toBeVisible();

  await demo.captureElement("fileEdit", card);
});
