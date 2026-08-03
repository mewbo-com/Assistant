/**
 * `parseSlug` / `slugFromRepoUrl` against the SHARED expectation table.
 *
 * `tests/fixtures/repository_identity_cases.json` is the same fixture the
 * Python suite (`tests/test_repositories.py`) asserts `RepositoryRef` against
 * and `registryPlatform.test.ts` reads for the platform half — so the slug
 * value itself is pinned here rather than only exercised indirectly through
 * a platform assertion. The `gitlab-subgroup` case is THE regression this
 * guards: owner/repo are the LAST TWO segments, not the first two — a
 * first-two read silently drops the real repo name and reads the subgroup as
 * the repo instead.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

import { parseSlug, slugFromRepoUrl } from "./slug";

const FIXTURE = resolve(process.cwd(), "../../tests/fixtures/repository_identity_cases.json");

interface Case {
  id: string;
  input: string;
  parsed: { slug: string; host: string; namespace: string[]; owner: string; repo: string } | null;
}

const doc = JSON.parse(readFileSync(FIXTURE, "utf8")) as { cases: Case[] };
const CASES = doc.cases;

describe("slugFromRepoUrl", () => {
  test("the fixture actually carries cases (a silent empty table would pass everything)", () => {
    expect(CASES.length).toBeGreaterThan(10);
  });

  // `slugFromRepoUrl` only reads URL-shaped input (`new URL(...)`), unlike the
  // backend's `RepositoryRef.from_url`, which also accepts an scp-style remote
  // or a bare slug. Those shapes fall through `new URL` (an scp remote reads
  // as a scheme with no host; a bare slug has no scheme at all) and the
  // function declines rather than guess — the console has no caller that
  // hands it anything but a pasted URL. These four ids are the expected
  // "declines to answer" cases.
  const NOT_A_URL_IDS = new Set([
    "scp-style",
    "scp-style-no-user",
    "gitlab-subgroup-slug",
    "bare-slug",
  ]);

  test.each(CASES.map((c) => [c.id, c] as const))("%s matches the server's slug", (id, testCase) => {
    if (NOT_A_URL_IDS.has(id)) {
      expect(slugFromRepoUrl(testCase.input)).toBeNull();
      return;
    }
    expect(slugFromRepoUrl(testCase.input)).toBe(testCase.parsed?.slug ?? null);
  });
});

describe("parseSlug", () => {
  const parsedCases = CASES.filter(
    (c): c is Case & { parsed: NonNullable<Case["parsed"]> } => c.parsed !== null,
  );

  test.each(parsedCases.map((c) => [c.id, c] as const))(
    "%s round-trips through slugFromRepoUrl's output",
    (_id, testCase) => {
      const { slug, owner, repo, host, namespace } = testCase.parsed;
      const parsed = parseSlug(slug);
      expect(parsed).not.toBeNull();
      expect(parsed?.owner).toBe(owner);
      expect(parsed?.repo).toBe(repo);
      expect(parsed?.host).toBe([host, ...namespace].join("/"));
    },
  );
});
