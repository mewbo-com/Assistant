/**
 * `openTargetSession`'s `requestNew` option must reach the wire under each
 * product's OWN casing — `newSession` (camelCase) on the wiki route,
 * `new_session` (snake_case) on the apps route — decided by team-lead
 * coordination (the wiki client already returns `{sessionId, created}`
 * camelCase; the apps client returns the raw snake_case `{session_id,
 * created}`). This drives the REAL `openProjectSession`/`openAppSession`
 * (unmocked) and asserts on the actual `fetch` body, since a mock at the
 * client-function boundary (see `composerTargets.test.tsx`) can't catch a
 * casing slip inside the function itself.
 *
 * vitest runs WITHOUT globals → explicit cleanup is unnecessary here (no DOM,
 * no RTL), but `vi.restoreAllMocks()` still resets the fetch stub.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { openProjectSession } from "../components/wiki/api/client";
import { openAppSession } from "../api/apps";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("wire casing per product route", () => {
  it("wiki route sends camelCase newSession when requestNew is set", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ sessionId: "s-wiki", created: true }));

    await openProjectSession("git.example.com/acme/beacon", { requestNew: true });

    const [, init] = fetchMock.mock.calls[0] as [RequestInfo, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ newSession: true });
  });

  it("wiki route sends no body by default (the gallery card 'open' shape)", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ sessionId: "s-wiki", created: false }));

    await openProjectSession("git.example.com/acme/beacon");

    const [, init] = fetchMock.mock.calls[0] as [RequestInfo, RequestInit];
    expect(init.body).toBeUndefined();
  });

  it("apps route sends snake_case new_session when requestNew is set", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ session_id: "s-app", created: true }));

    await openAppSession("app-ledger", { requestNew: true });

    const [, init] = fetchMock.mock.calls[0] as [RequestInfo, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ new_session: true });
  });

  it("apps route sends no body by default (the app header 'open' shape)", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ session_id: "s-app", created: false }));

    await openAppSession("app-ledger");

    const [, init] = fetchMock.mock.calls[0] as [RequestInfo, RequestInit];
    expect(init.body).toBeUndefined();
  });
});
