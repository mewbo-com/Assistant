/**
 * Canonical wiki identity = ``host/owner/repo``, or ``owner/repo`` when no
 * host was recorded. All helpers here parse and compose around that single
 * shape so the rest of the app never resorts to platform-name fallbacks or
 * hard-coded host tables.
 */

/**
 * Fallback slug for screens that render before a real one is known (a deep
 * link with no ``slug`` param, a dev/demo route). This repo's own slug,
 * since the console is its own worked example.
 */
export const DEFAULT_WIKI_SLUG = "bearlike/Assistant";

export interface ParsedSlug {
  /** DNS host (``github.com``, ``git.example.com``) — absent on a
   *  two-segment slug with no host recorded. */
  host?: string;
  owner: string;
  repo: string;
}

/**
 * Parse a slug into its components.
 *
 * - ``host/owner/repo`` → fully qualified (3+ segments; intermediate
 *   segments are folded into the host, supporting paths like
 *   ``gitlab.example.io/group/subgroup/repo`` if ever needed by joining
 *   everything before the last two segments back together for ``host``).
 * - ``owner/repo`` → no host; ``host`` is ``undefined``.
 *
 * Returns ``null`` when the input doesn't have at least owner + repo.
 */
export function parseSlug(slug: string): ParsedSlug | null {
  const parts = slug.split("/").filter(Boolean);
  if (parts.length < 2) return null;
  const repo = parts[parts.length - 1].replace(/\.git$/, "");
  const owner = parts[parts.length - 2];
  if (parts.length === 2) return { owner, repo };
  return {
    host: parts.slice(0, -2).join("/"),
    owner,
    repo,
  };
}

/**
 * Build the canonical fully-qualified slug from a repo URL. We use the
 * URL's host + path (never a guess from the platform name).
 *
 * - Input: ``https://git.example.com/bearlike/Grove``
 * - Output: ``git.example.com/bearlike/Grove``
 *
 * Owner/repo are the LAST TWO path segments, mirroring {@link parseSlug} and
 * the backend's ``RepositoryRef.split_remote``/``from_parts`` — any segments
 * between the host and them fold into the slug unchanged (a GitLab subgroup,
 * ``gitlab.com/group/subgroup/repo``). Taking the FIRST two segments instead
 * would read the subgroup as the repo and silently drop the real one.
 *
 * Returns ``null`` when the URL doesn't carry both an owner and repo.
 */
export function slugFromRepoUrl(url: string): string | null {
  try {
    const u = new URL(url.trim());
    // A host is not optional. `new URL` accepts an scp-style remote
    // (`git.example.com:acme/beacon.git`) by reading the part before the colon
    // as a SCHEME, leaving `hostname` empty — composing a host-less
    // `/acme/beacon` from that is not a valid slug: the server files the same
    // remote under `git.example.com/acme/beacon`, so a preview built on the
    // host-less form would contradict what got stored. Returning null makes
    // the caller say it cannot tell yet, which is true.
    if (!u.hostname) return null;
    const parts = u.pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
    if (parts.length < 2) return null;
    const repo = parts[parts.length - 1].replace(/\.git$/, "");
    const owner = parts[parts.length - 2];
    const namespace = parts.slice(0, -2);
    return [u.hostname, ...namespace, owner, repo].join("/");
  } catch {
    return null;
  }
}

/**
 * Compose the canonical external repo URL from slug or persisted repoUrl.
 *
 * Prefers the persisted ``repoUrl`` (handles non-default protocols,
 * trailing paths, etc.) and only falls back to ``https://{host}/{owner}/{repo}``
 * when needed. Returns ``undefined`` for slugs without a host — never
 * fabricate a github.com link.
 */
export function canonicalRepoUrl(
  slug: string,
  repoUrl?: string,
): string | undefined {
  if (repoUrl) return repoUrl;
  const parsed = parseSlug(slug);
  if (!parsed || !parsed.host) return undefined;
  return `https://${parsed.host}/${parsed.owner}/${parsed.repo}`;
}

/**
 * Short display label — what users typically expect to see when scanning
 * a list of repos. ``owner/repo`` (two segments), with host shown
 * separately as a subtitle so the eye doesn't have to parse the URL.
 */
export function shortSlug(slug: string): string {
  const parsed = parseSlug(slug);
  if (!parsed) return slug;
  return `${parsed.owner}/${parsed.repo}`;
}
