/**
 * Product-level repository registry — `/v1/git/repositories`.
 *
 * A repository is an object Mewbo knows about, not a job it has run. Registering
 * one is a no-op: normalize the URL, validate it, persist the record. Nothing is
 * cloned, nothing is indexed, no model is called. Consumers opt in afterwards
 * and explicitly, and each one reports back through `usage` so a single row can
 * answer "what is this repository actually wired into?".
 *
 * That inversion is the whole point of the surface. The wiki writes its
 * `Project` rows only at index finalize, so a list built from them can only ever
 * show repositories that already survived a paid multi-minute run. The registry
 * is the superset: a repository appears the moment someone registers it, and a
 * wiki-indexed repository appears in it too (`origin: "wiki"`).
 *
 * It sits under `/v1/git` beside the credential registry rather than under
 * `/v1/wiki`, for the same two reasons: the wiki is only one consumer, and both
 * nginx (`docker/nginx-console.conf`, `location /v1/git/`) and the vite dev
 * proxy (`vite.config.ts`) already forward that prefix. An unproxied path is not
 * a 404 here, it falls through to the SPA catch-all and answers 200 with
 * `index.html`, so the client parses HTML as JSON and reports a nonsense error.
 *
 * Mirrors the `api/git.ts` / `api/triggers.ts` seam: typed DTOs over the shared
 * `httpBase` quartet, never a third fetch wrapper.
 */
import { API_BASE } from "./client";
import type { GitCredentialScopeType } from "./git";
import {
  apiFetch,
  withBase as sharedWithBase,
  authHeaders,
  jsonHeaders,
  readError,
  readJson,
} from "./httpBase";

// ---------------------------------------------------------------------------
// Wire shapes — closed unions mirror the backend 1:1 so a `Record<…>`
// presentation map stays exhaustive and `tsc` flags contract drift.
// ---------------------------------------------------------------------------

/** Git platform, same vocabulary as the wiki `Platform["id"]` catalogue. */
export type RepositoryPlatform =
  | "github"
  | "gitlab"
  | "bitbucket"
  | "gitea"
  | "azure"
  | "git";

/** Who put the record here: a person, or a consumer that registered on demand. */
export type RepositoryOrigin = "manual" | "wiki" | "task";

/**
 * Wiki coverage. `null` means the graph extra is absent from this deployment,
 * which is NOT the same as "not indexed" — the difference decides whether the
 * UI offers to generate a wiki or explains that it cannot.
 */
export interface RepositoryWikiUsage {
  indexed: boolean;
  indexedAt: string | null;
  /** Page count, or null when the record predates the counter. */
  pages: number | null;
}

/**
 * The managed project an agentic task would run in, when one is set up. Only
 * `projectId` is guaranteed; the name and path are whatever the project store
 * happens to hold, so a consumer must have a label to fall back to.
 */
export interface RepositoryTasksUsage {
  projectId: string;
  name: string | null;
  path: string | null;
}

/**
 * The stored credential that reaches this repository, resolved SERVER-side by
 * the one credential chain. Only the identity is returned, never the secret.
 * Prefer this over a client-side `matchCredential` guess wherever it is present:
 * only the backend walks the real chain.
 */
export interface RepositoryCredentialUsage {
  scope: string;
  scopeType: GitCredentialScopeType;
}

/** What every consumer reports about one repository. */
export interface RepositoryUsage {
  wiki: RepositoryWikiUsage | null;
  tasks: RepositoryTasksUsage | null;
  credential: RepositoryCredentialUsage | null;
}

/** One row of `GET /v1/git/repositories`. */
export interface RepositoryDTO {
  /** Canonical identity `host/owner/repo`, server-owned and immutable. */
  slug: string;
  host: string;
  owner: string;
  repo: string;
  repoUrl: string;
  platform: RepositoryPlatform;
  defaultBranch: string | null;
  /** Operator-chosen display name; falls back to `repo` when absent. */
  name: string | null;
  description: string | null;
  origin: RepositoryOrigin;
  createdAt: string;
  updatedAt: string;
  usage: RepositoryUsage;
}

/**
 * Body for `POST /v1/git/repositories`. `slug`, `platform` and `usage` are
 * server-owned: sending one is a 400 rather than a silent no-op, because the
 * request models forbid extra keys.
 */
export interface RepositoryCreateInput {
  repoUrl: string;
  defaultBranch?: string;
  name?: string;
  description?: string;
}

/** Body for `PATCH /v1/git/repositories/<slug>` — the three mutable fields. */
export interface RepositoryPatchInput {
  defaultBranch?: string | null;
  name?: string | null;
  description?: string | null;
}

// ---------------------------------------------------------------------------
// Typed errors — the `/v1/git` envelope is `{error:{code,reason}}`
// ---------------------------------------------------------------------------

/** Error codes this surface answers with. */
export type RepositoryErrorCode =
  | "invalid_repo_url"
  /** Malformed body, or a server-owned field the client tried to set. */
  | "invalid_request"
  | "repository_exists"
  | "repository_not_found"
  /** The deployment cannot check anything out — the git extra is not installed. */
  | "checkout_unavailable"
  /** The clone itself failed; `reason` carries git's own (redacted) stderr. */
  | "checkout_failed";

/**
 * A rejection carrying its machine code, so a caller can pin the message to the
 * field that caused it (a duplicate belongs on the URL input, not in a banner)
 * instead of re-parsing the envelope. `httpBase.readError` would otherwise
 * stringify the whole `{error:{…}}` body into `Error.message`, which reads as
 * raw JSON to the user.
 */
export class RepositoryError extends Error {
  readonly code: string;
  readonly reason: string;
  readonly status: number;

  constructor(code: string, reason: string, status: number) {
    super(reason || `Request failed: ${status}`);
    this.name = "RepositoryError";
    this.code = code;
    this.reason = reason;
    this.status = status;
  }
}

/** True when *err* is a {@link RepositoryError} carrying *code*. */
export function isRepositoryError(
  err: unknown,
  code: RepositoryErrorCode,
): err is RepositoryError {
  return err instanceof RepositoryError && err.code === code;
}

function withBase(path: string): string {
  return sharedWithBase(API_BASE, path);
}

/**
 * Encode a slug for the URL, **per segment**, so its separators stay real
 * slashes: `/v1/git/repositories/github.com/acme/beacon`.
 *
 * This deliberately differs from `api/git.ts:encodeScope`, which encodes a
 * credential scope whole. The repository routes are `<path:slug>` converters
 * that expect the identity spread across path segments, and a `%2F`-encoded
 * slug survives or dies depending on how the fronting proxy normalizes escaped
 * separators, which is not a thing to leave to chance on every deployment.
 * Encoding each segment individually still escapes anything unusual inside a
 * segment without touching the separators the route matches on.
 */
function encodeSlug(slug: string): string {
  return slug.split("/").map(encodeURIComponent).join("/");
}

/**
 * Read a `Response`, mapping the `/v1/git` error envelope to a typed
 * {@link RepositoryError} and anything else to the shared `readError` message.
 */
async function readRepositoryResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response
      .clone()
      .json()
      .catch(() => null);
    const err = (body as { error?: { code?: string; reason?: string } } | null)
      ?.error;
    if (err?.code) {
      throw new RepositoryError(err.code, err.reason ?? "", response.status);
    }
    throw await readError(response);
  }
  return readJson<T>(response);
}

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

/** GET /v1/git/repositories — every repository Mewbo knows about. */
export async function listRepositories(
  signal?: AbortSignal,
): Promise<RepositoryDTO[]> {
  const response = await apiFetch(withBase("/v1/git/repositories"), {
    headers: authHeaders(),
    signal,
  });
  const data = await readJson<{ repositories?: RepositoryDTO[] }>(response);
  return data.repositories ?? [];
}

/** GET /v1/git/repositories/<slug> — one record, 404 when unregistered. */
export async function getRepository(
  slug: string,
  signal?: AbortSignal,
): Promise<RepositoryDTO> {
  const response = await apiFetch(
    withBase(`/v1/git/repositories/${encodeSlug(slug)}`),
    { headers: authHeaders(), signal },
  );
  return readRepositoryResponse<RepositoryDTO>(response);
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

/**
 * POST /v1/git/repositories — register a repository. Normalize, validate,
 * persist. This clones nothing and indexes nothing.
 */
export async function createRepository(
  input: RepositoryCreateInput,
  signal?: AbortSignal,
): Promise<RepositoryDTO> {
  const response = await apiFetch(withBase("/v1/git/repositories"), {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(input),
    signal,
  });
  return readRepositoryResponse<RepositoryDTO>(response);
}

/** PATCH /v1/git/repositories/<slug> — edit the display fields only. */
export async function patchRepository(
  slug: string,
  patch: RepositoryPatchInput,
  signal?: AbortSignal,
): Promise<RepositoryDTO> {
  const response = await apiFetch(
    withBase(`/v1/git/repositories/${encodeSlug(slug)}`),
    {
      method: "PATCH",
      headers: jsonHeaders(),
      body: JSON.stringify(patch),
      signal,
    },
  );
  return readRepositoryResponse<RepositoryDTO>(response);
}

/**
 * POST /v1/git/repositories/<slug>/checkout — clone the repository into a
 * managed project an agentic task can run in.
 *
 * The one action that spends anything. Registering is inert by design, so a
 * registered repository has no checkout until someone asks for this — never as a
 * side effect of opening a picker or sending a message. It is SYNCHRONOUS and
 * server-bounded (a clone, not a job), so a caller must show a pending state:
 * expect seconds on a small repository and up to a few minutes on a large one.
 *
 * Idempotent. A repository that already has a checkout answers 200 with the
 * existing binding instead of cloning twice, so a double-click costs nothing.
 * Resolves with the fresh record, whose `usage.tasks.projectId` is the project
 * id a session anchors to.
 */
export async function checkoutRepository(
  slug: string,
  signal?: AbortSignal,
): Promise<RepositoryDTO> {
  const response = await apiFetch(
    withBase(`/v1/git/repositories/${encodeSlug(slug)}/checkout`),
    { method: "POST", headers: jsonHeaders(), signal },
  );
  return readRepositoryResponse<RepositoryDTO>(response);
}

/**
 * DELETE /v1/git/repositories/<slug> — deregister. This removes the registry
 * record only: a generated wiki, a managed project and a stored credential all
 * survive it and are deleted through their own surfaces. A 404 means the record
 * is already gone, which satisfies the caller's intent either way.
 */
export async function deleteRepository(
  slug: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await apiFetch(
    withBase(`/v1/git/repositories/${encodeSlug(slug)}`),
    { method: "DELETE", headers: authHeaders(), signal },
  );
  if (response.ok || response.status === 404) return;
  await readRepositoryResponse<unknown>(response);
}
