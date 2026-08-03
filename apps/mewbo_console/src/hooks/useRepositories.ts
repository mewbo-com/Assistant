/**
 * TanStack Query hooks over the product-level repository registry
 * (`/v1/git/repositories`).
 *
 * One root key, `["repositories"]`, so a register or a deregister refreshes
 * every consumer of the list at once. `usage` is server-computed and moves
 * whenever another product acts on a repository (a wiki index finishes, a
 * credential is stored), so the list is invalidated after those writes too
 * rather than being patched optimistically from a guess about what the server
 * would have recomputed.
 *
 * Follows the `useGitCredentials` convention: a `refresh()` shim over
 * `invalidateQueries` so callers never need to learn the query API.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  checkoutRepository,
  createRepository,
  deleteRepository,
  getRepository,
  listRepositories,
  type RepositoryCreateInput,
  type RepositoryDTO,
} from "../api/repositories";
import { logApiError } from "../utils/errors";

export const REPOSITORIES_KEY = ["repositories"] as const;

/** Stable empty ref so consumers' memos don't churn before the first payload. */
const EMPTY: RepositoryDTO[] = [];

/**
 * Every registered repository. This is the superset the Settings table renders:
 * a repository is here because someone registered it, never because a run
 * happened to succeed.
 */
export function useRepositories(enabled = true) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: REPOSITORIES_KEY,
    queryFn: ({ signal }) => listRepositories(signal),
    staleTime: 30_000,
    enabled,
  });
  return {
    repositories: q.data ?? EMPTY,
    // `isLoading` rather than `isPending`: a disabled query stays pending
    // forever, which would pin the table in a permanent spinner.
    loading: q.isLoading,
    error: q.error ? logApiError("listRepositories", q.error) : null,
    refresh: () => qc.invalidateQueries({ queryKey: REPOSITORIES_KEY }),
  };
}

/**
 * One registered repository by slug, over `GET /v1/git/repositories/<slug>`.
 *
 * Keyed `["repositories", slug]` — the same prefix as the list, so a register or
 * deregister invalidates this alongside it and the two can never disagree. It is
 * a real per-slug read rather than a `select` over the cached list, because a
 * consumer usually arrives with a slug and no warm list (the wiki wizard opened
 * straight from a `?repo=` deep link), and narrowing an absent list would report
 * a registered repository as missing.
 *
 * **`retry: false` is load-bearing.** An unregistered slug is a legitimate
 * answer, not a transient fault, so a 404 must settle immediately instead of
 * being retried; the caller reads `repository === undefined` and moves on.
 */
export function useRepository(slug?: string | null, enabled = true) {
  const q = useQuery({
    queryKey: [...REPOSITORIES_KEY, slug ?? null] as const,
    queryFn: ({ signal }) => getRepository(slug as string, signal),
    staleTime: 30_000,
    enabled: enabled && Boolean(slug),
    retry: false,
  });
  return {
    repository: q.data,
    loading: q.isLoading && Boolean(slug),
    error: q.error ? logApiError("getRepository", q.error) : null,
  };
}

/**
 * Register a repository. The mutation resolves with the created record so the
 * caller can hand its canonical slug straight to the next step (seeding a
 * credential scope, say) without re-deriving one from the URL the user typed.
 */
export function useCreateRepository() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: RepositoryCreateInput) => createRepository(input),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: REPOSITORIES_KEY });
    },
  });
}

/**
 * Check a repository out: clone it into a managed project a task can run in.
 *
 * Deliberately a plain mutation with no optimistic update. The response carries
 * the server's recomputed `usage.tasks`, and the project id in it is the whole
 * point of the call — guessing one so a row could flip a moment sooner would
 * mean inventing the identifier the caller then anchors a session to.
 *
 * `mutateAsync` resolves with the fresh record, so a caller can select the
 * resulting project the instant the clone lands. The mutation's own `isPending`
 * is the pending state to render; it can run for minutes on a large repository,
 * and a checkout with no visible progress reads as a dead button.
 */
export function useCheckoutRepository() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => checkoutRepository(slug),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: REPOSITORIES_KEY });
      // A checkout CREATES a managed project, so `["projects"]` is genuinely
      // stale the moment this resolves. Without this the composer would anchor
      // a session to a project id its own project list has never seen, and
      // `ProjectLabel` would render the raw `managed:<uuid>` until something
      // else happened to refetch.
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });
}

/**
 * Deregister a repository: the registry record and nothing else. A generated
 * wiki, a managed project and a stored credential outlive it, which is why this
 * is a different action from deleting a wiki index and must never share a
 * button with one.
 */
export function useDeleteRepository() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => deleteRepository(slug),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: REPOSITORIES_KEY });
    },
  });
}
