/**
 * Wiki-internal route helpers. The wiki section lives under `/wiki/*`; sub-
 * routes are encoded into the same URL space so they're shareable and the
 * browser back button works.
 *
 *   /wiki                          → LandingScreen (project gallery)
 *   /wiki/configure?url=...&repo=host/owner/repo
 *                                  → ConfigureWizard
 *   /wiki/repo?slug=owner/name     → WelcomeScreen ("not indexed")
 *   /wiki/indexing?jobId=...       → IndexingScreen
 *   /wiki/p/:pageId                → WikiScreen
 *   /wiki/qa?q=...&page=...&model=...
 *                                  → QAScreen
 *   /wiki/graph?slug=...           → KnowledgeGraph3DScreen
 *   /wiki/outline?slug=...         → GraphOutlineScreen
 */

import { useLocation } from "wouter";

import type { Platform } from "./api/types";

export type PlatformId = Platform["id"];

export type WikiRoute =
  | { kind: "landing" }
  /**
   * `repo` names an already-registered repository (`host/owner/repo`) whose
   * stored URL, platform and default branch seed the wizard. It exists so a
   * settings row can offer "Generate wiki" without making the operator retype
   * a URL the product already holds. Registering a repository stays inert:
   * this route is how a user OPTS IN to indexing one, never a side effect of
   * having registered it.
   */
  | { kind: "configure"; url?: string; repo?: string }
  | { kind: "welcome"; slug?: string; platform?: PlatformId }
  | { kind: "indexing"; jobId?: string; slug?: string; platform?: PlatformId }
  | { kind: "page"; pageId: string; slug?: string; platform?: PlatformId }
  | {
      kind: "qa";
      question: string;
      pageId: string;
      slug?: string;
      model?: string;
      /**
       * Persisted answer id. When present the QA screen renders the saved
       * answer (``GET /v1/wiki/qa/<id>``) instead of POSTing a fresh run, so a
       * refresh / shared link is idempotent and never re-invokes the LLM.
       */
      answer?: string;
      platform?: PlatformId;
    }
  | { kind: "graph"; slug?: string; platform?: PlatformId }
  /**
   * The textual channel over the same graph payload — a focusable, announceable
   * outline. Its own route rather than a panel inside `graph` because the
   * spatial renderer blocks the main thread on a repo of any size, and a panel
   * sharing that mount would inherit the freeze.
   */
  | { kind: "outline"; slug?: string; platform?: PlatformId };

const PLATFORM_IDS: readonly PlatformId[] = [
  "github",
  "gitlab",
  "bitbucket",
  "gitea",
  "azure",
  "git",
];

function parsePlatform(value: string | null): PlatformId | undefined {
  if (!value) return undefined;
  return (PLATFORM_IDS as readonly string[]).includes(value)
    ? (value as PlatformId)
    : undefined;
}

export function parseWikiRoute(path: string, queryString: string): WikiRoute {
  const params = new URLSearchParams(queryString);
  const tail = path.replace(/^\/wiki/, "") || "/";

  if (tail === "/" || tail === "") return { kind: "landing" };

  if (tail.startsWith("/configure")) {
    return {
      kind: "configure",
      url: params.get("url") || undefined,
      repo: params.get("repo") || undefined,
    };
  }
  if (tail.startsWith("/repo")) {
    return {
      kind: "welcome",
      slug: params.get("slug") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  if (tail.startsWith("/indexing")) {
    return {
      kind: "indexing",
      jobId: params.get("jobId") || undefined,
      slug: params.get("slug") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  if (tail.startsWith("/graph")) {
    return {
      kind: "graph",
      slug: params.get("slug") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  if (tail.startsWith("/outline")) {
    return {
      kind: "outline",
      slug: params.get("slug") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  if (tail.startsWith("/qa")) {
    return {
      kind: "qa",
      question: params.get("q") || "",
      pageId: params.get("page") || "core",
      slug: params.get("slug") || undefined,
      model: params.get("model") || undefined,
      answer: params.get("answer") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  const pageMatch = tail.match(/^\/p\/(.+)$/);
  if (pageMatch) {
    return {
      kind: "page",
      pageId: decodeURIComponent(pageMatch[1]),
      slug: params.get("slug") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  return { kind: "landing" };
}

function appendPlatform(params: URLSearchParams, platform?: PlatformId): void {
  if (platform) params.set("platform", platform);
}

export function buildHref(route: WikiRoute): string {
  switch (route.kind) {
    case "landing":
      return "/wiki";
    case "configure": {
      const params = new URLSearchParams();
      if (route.url) params.set("url", route.url);
      if (route.repo) params.set("repo", route.repo);
      const qs = params.toString();
      return `/wiki/configure${qs ? `?${qs}` : ""}`;
    }
    case "welcome": {
      const params = new URLSearchParams();
      if (route.slug) params.set("slug", route.slug);
      appendPlatform(params, route.platform);
      const qs = params.toString();
      return `/wiki/repo${qs ? `?${qs}` : ""}`;
    }
    case "indexing": {
      const params = new URLSearchParams();
      if (route.jobId) params.set("jobId", route.jobId);
      if (route.slug) params.set("slug", route.slug);
      appendPlatform(params, route.platform);
      const qs = params.toString();
      return `/wiki/indexing${qs ? `?${qs}` : ""}`;
    }
    case "page": {
      const params = new URLSearchParams();
      if (route.slug) params.set("slug", route.slug);
      appendPlatform(params, route.platform);
      const qs = params.toString();
      return `/wiki/p/${encodeURIComponent(route.pageId)}${qs ? `?${qs}` : ""}`;
    }
    case "qa": {
      const params = new URLSearchParams();
      params.set("q", route.question);
      params.set("page", route.pageId);
      if (route.slug) params.set("slug", route.slug);
      if (route.model) params.set("model", route.model);
      if (route.answer) params.set("answer", route.answer);
      appendPlatform(params, route.platform);
      return `/wiki/qa?${params.toString()}`;
    }
    case "graph": {
      const params = new URLSearchParams();
      if (route.slug) params.set("slug", route.slug);
      appendPlatform(params, route.platform);
      const qs = params.toString();
      return `/wiki/graph${qs ? `?${qs}` : ""}`;
    }
    case "outline": {
      const params = new URLSearchParams();
      if (route.slug) params.set("slug", route.slug);
      appendPlatform(params, route.platform);
      const qs = params.toString();
      return `/wiki/outline${qs ? `?${qs}` : ""}`;
    }
  }
}

/**
 * Read the current wiki route. wouter normalises pathname only, so this
 * pulls the search string from `window.location` (the router proxies the
 * History API so the value is fresh).
 */
export function useWikiRoute(): WikiRoute {
  const [pathname] = useLocation();
  const search = typeof window !== "undefined" ? window.location.search : "";
  return parseWikiRoute(pathname, search);
}
