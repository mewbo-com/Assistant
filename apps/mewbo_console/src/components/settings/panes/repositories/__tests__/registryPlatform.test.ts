/**
 * `registryPlatformFromUrl` against the SHARED expectation table.
 *
 * The preview beside "will be registered as" is a claim about a value the
 * server is about to persist and render back, so the only acceptable answers
 * are the server's answer or silence. This suite reads
 * `tests/fixtures/repository_identity_cases.json` — the same file the Python
 * suite asserts `PLATFORM_HOSTS` against — so the two languages are pinned to
 * one table rather than to two copies that agree today.
 *
 * The catalogue is built from the fixture's `platformHosts` block rather than
 * hand-written here, for the same reason: a host added to core and to the
 * fixture reaches this test automatically, and one added to only one of them
 * fails it.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

import {
  detectPlatformFromUrl,
  registryPlatformFromUrl,
} from "../../../../wiki/configure-wizard/wizardState";
import { slugFromRepoUrl } from "../../../../wiki/slug";
import type { Platform } from "../../../../wiki/api/types";

// Same `process.cwd()` anchor `src/__tests__/timelineParity.test.ts` uses for
// the other shared fixture — vitest runs from the console package root.
const FIXTURE = resolve(process.cwd(), "../../tests/fixtures/repository_identity_cases.json");

interface Case {
  id: string;
  input: string;
  parsed: { slug: string; host: string; platform: Platform["id"] } | null;
}

const doc = JSON.parse(readFileSync(FIXTURE, "utf8")) as {
  platformHosts: Record<string, Platform["id"]>;
  cases: Case[];
};

/** The `/v1/wiki/platforms` payload, minus the presentation fields nothing here reads. */
const CATALOGUE: Platform[] = (() => {
  const hosts = new Map<Platform["id"], string[]>();
  for (const [host, platform] of Object.entries(doc.platformHosts)) {
    hosts.set(platform, [...(hosts.get(platform) ?? []), host]);
  }
  const ids: Platform["id"][] = ["github", "gitlab", "bitbucket", "gitea", "azure", "git"];
  return ids.map(
    (id) =>
      ({
        id,
        name: id,
        mono: id,
        color: "#000000",
        short: id,
        hosts: hosts.get(id) ?? [],
        tokenLabel: "",
        tokenScope: "",
        tokenUrl: null,
        tokenSteps: [],
      }) as Platform,
  );
})();

const CASES = doc.cases;

describe("registryPlatformFromUrl", () => {
  test("the fixture actually carries cases (a silent empty table would pass everything)", () => {
    expect(CASES.length).toBeGreaterThan(10);
    expect(Object.keys(doc.platformHosts).length).toBeGreaterThan(0);
  });

  // THE property: whenever the dialog shows a preview at all — which it does
  // only once `slugFromRepoUrl` resolves — the platform it names must be the
  // one the server will store.
  const previewable = CASES.filter(
    (c): c is Case & { parsed: NonNullable<Case["parsed"]> } =>
      c.parsed !== null && slugFromRepoUrl(c.input) !== null,
  );

  test("every previewable case is covered (guards against the filter emptying)", () => {
    expect(previewable.length).toBeGreaterThan(5);
  });

  test.each(previewable.map((c) => [c.id, c] as const))(
    "%s previews exactly what the server stores",
    (_id, testCase) => {
      expect(registryPlatformFromUrl(testCase.input, CATALOGUE)).toBe(testCase.parsed.platform);
    },
  );

  // The weaker guarantee for everything else: never contradict the server. An
  // input the console cannot place yields `null`, which renders as no platform
  // at all — an absent claim, not a wrong one.
  test.each(CASES.map((c) => [c.id, c] as const))("%s never contradicts the server", (_id, testCase) => {
    const previewed = registryPlatformFromUrl(testCase.input, CATALOGUE);
    if (previewed !== null && testCase.parsed !== null) {
      expect(previewed).toBe(testCase.parsed.platform);
    }
  });

  test("a self-hosted host is reported as generic git, never guessed", () => {
    for (const url of [
      "https://git.example.com/acme/beacon",
      "https://gitea.example.com/acme/beacon",
      "https://gitlab.example.com/acme/beacon",
    ]) {
      expect(registryPlatformFromUrl(url, CATALOGUE)).toBe("git");
    }
  });

  test("an empty catalogue names nothing rather than guessing", () => {
    expect(registryPlatformFromUrl("https://github.com/acme/beacon", [])).toBe("git");
  });

  test("no host at all is null, so the preview stays silent", () => {
    expect(registryPlatformFromUrl("", CATALOGUE)).toBeNull();
    expect(registryPlatformFromUrl("   ", CATALOGUE)).toBeNull();
  });
});

describe("detectPlatformFromUrl (the wizard's guess) is deliberately different", () => {
  test("still guesses gitea for a git. prefix, which is why it must not back a preview", () => {
    expect(detectPlatformFromUrl("https://git.example.com/acme/beacon", CATALOGUE)).toBe("gitea");
    expect(registryPlatformFromUrl("https://git.example.com/acme/beacon", CATALOGUE)).toBe("git");
  });

  test("agrees with the registry rule on every catalogued host", () => {
    for (const [host, platform] of Object.entries(doc.platformHosts)) {
      const url = `https://${host}/acme/beacon`;
      expect(detectPlatformFromUrl(url, CATALOGUE)).toBe(platform);
      expect(registryPlatformFromUrl(url, CATALOGUE)).toBe(platform);
    }
  });

  test("an unparseable URL still falls back to github, as it always has", () => {
    expect(detectPlatformFromUrl("not a url", CATALOGUE)).toBe("github");
  });
});
