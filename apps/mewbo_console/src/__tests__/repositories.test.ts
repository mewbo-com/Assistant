/**
 * `api/repositories.ts` — the wire-shape guarantees the UI cannot check for
 * itself.
 *
 * The URL-shape tests are the ones that earned their place. The routes are
 * Flask `<path:slug>` converters and a slug CONTAINS slashes, so an identity
 * encoded whole (`github.com%2Facme%2Fbeacon`, the convention the sibling
 * credential routes use) resolves or 404s depending on how the fronting proxy
 * normalizes escaped separators. Nothing in a component test would catch that,
 * and every per-slug call on the surface depends on it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createRepository,
  deleteRepository,
  getRepository,
  isRepositoryError,
  listRepositories,
  patchRepository,
  RepositoryError,
} from "../api/repositories";

const SLUG = "github.com/acme/beacon";

let fetchMock: ReturnType<typeof vi.fn>;

/** Reply with *body* at *status*, as the API would. */
function reply(status: number, body: unknown): Response {
  return new Response(body === undefined ? "" : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The path (no origin) of the single request the call under test issued. */
function requestedPath(): string {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const url = String(fetchMock.mock.calls[0][0]);
  return url.startsWith("http") ? new URL(url).pathname : url;
}

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("per-slug URL shape", () => {
  it("spreads the slug across real path segments, never a %2F-encoded blob", async () => {
    fetchMock.mockResolvedValue(reply(200, { slug: SLUG }));
    await getRepository(SLUG);

    expect(requestedPath()).toBe("/v1/git/repositories/github.com/acme/beacon");
    expect(requestedPath()).not.toContain("%2F");
  });

  it("uses the same shape for PATCH and DELETE", async () => {
    fetchMock.mockResolvedValue(reply(200, { slug: SLUG }));
    await patchRepository(SLUG, { description: null });
    expect(requestedPath()).toBe("/v1/git/repositories/github.com/acme/beacon");

    fetchMock.mockClear();
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await deleteRepository(SLUG);
    expect(requestedPath()).toBe("/v1/git/repositories/github.com/acme/beacon");
  });

  it("still escapes characters inside a segment", async () => {
    fetchMock.mockResolvedValue(reply(200, { slug: "x" }));
    await getRepository("git.example.com/acme/my repo");

    expect(requestedPath()).toContain("/acme/my%20repo");
  });

  it("keeps a nested group's extra segments as segments", async () => {
    fetchMock.mockResolvedValue(reply(200, { slug: "x" }));
    await getRepository("git.example.com/group/subgroup/repo");

    expect(requestedPath()).toBe(
      "/v1/git/repositories/git.example.com/group/subgroup/repo",
    );
  });
});

describe("request bodies", () => {
  it("POSTs the body verbatim and reads back the created record", async () => {
    fetchMock.mockResolvedValue(reply(201, { slug: SLUG, repo: "beacon" }));

    const created = await createRepository({ repoUrl: "https://github.com/acme/beacon" });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      repoUrl: "https://github.com/acme/beacon",
    });
    expect(created.slug).toBe(SLUG);
  });

  it("PATCHes an explicit null through, since null clears and omitted leaves alone", async () => {
    fetchMock.mockResolvedValue(reply(200, { slug: SLUG }));

    await patchRepository(SLUG, { description: null });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ description: null });
  });
});

describe("error envelope", () => {
  it("maps {error:{code,reason}} to a typed RepositoryError", async () => {
    fetchMock.mockResolvedValue(
      reply(409, {
        error: {
          code: "repository_exists",
          reason: "Already registered.",
          retryable: false,
        },
      }),
    );

    const err = await createRepository({ repoUrl: "https://github.com/acme/beacon" }).catch(
      (e: unknown) => e,
    );

    expect(err).toBeInstanceOf(RepositoryError);
    expect(isRepositoryError(err, "repository_exists")).toBe(true);
    expect(isRepositoryError(err, "invalid_repo_url")).toBe(false);
    // The message is the human reason, never the stringified envelope.
    expect((err as RepositoryError).message).toBe("Already registered.");
    expect((err as RepositoryError).status).toBe(409);
  });

  it("carries invalid_request through, so a server-owned field is not mistaken for a URL problem", async () => {
    fetchMock.mockResolvedValue(
      reply(400, {
        error: { code: "invalid_request", reason: "'platform' is server-owned." },
      }),
    );

    const err = await createRepository({ repoUrl: "https://github.com/acme/beacon" }).catch(
      (e: unknown) => e,
    );

    expect(isRepositoryError(err, "invalid_request")).toBe(true);
  });

  it("falls back to the shared reader for a body that is not the envelope", async () => {
    fetchMock.mockResolvedValue(reply(500, { message: "upstream exploded" }));

    const err = await getRepository(SLUG).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(RepositoryError);
    expect((err as Error).message).toBe("upstream exploded");
  });
});

describe("list and delete", () => {
  it("unwraps the {repositories: […]} envelope and tolerates its absence", async () => {
    fetchMock.mockResolvedValue(reply(200, { repositories: [{ slug: SLUG }] }));
    expect(await listRepositories()).toHaveLength(1);

    fetchMock.mockClear();
    fetchMock.mockResolvedValue(reply(200, {}));
    expect(await listRepositories()).toEqual([]);
  });

  it("treats a 204 and a 404 alike: the record is gone either way", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(deleteRepository(SLUG)).resolves.toBeUndefined();

    fetchMock.mockClear();
    fetchMock.mockResolvedValue(
      reply(404, { error: { code: "repository_not_found", reason: "gone" } }),
    );
    await expect(deleteRepository(SLUG)).resolves.toBeUndefined();
  });
});
