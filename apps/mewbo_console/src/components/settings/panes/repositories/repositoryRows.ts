/**
 * RepositoryRow — the pure model behind the repository registry table.
 *
 * This class is a view model, not a join: the registry answers host, owner,
 * repo and resolved credential usage server-side (`RepositoryDTO` carries
 * `host`, `owner`, `repo` and a resolved `usage.credential`), so this class
 * only holds presentation decisions that must not be re-derived per
 * component, and the collection behaviour the table needs.
 *
 * The important thing it encodes is that **"no wiki" has two meanings**.
 * `usage.wiki === null` means this deployment has no graph extra installed, so
 * no wiki can ever be generated; `usage.wiki.indexed === false` means one can be
 * generated and nobody has yet. Collapsing them into a single em dash is how a
 * table quietly offers an action that cannot work, so `wikiState()` keeps them
 * apart and every consumer branches on it.
 *
 * No React, no network, no formatting the caller can't override. Derived data
 * scattered through JSX is the easiest thing on this surface to get wrong.
 */

import type {
  RepositoryCredentialUsage,
  RepositoryDTO,
  RepositoryPlatform,
  RepositoryTasksUsage,
} from "../../../../api/repositories";
import { RelativeTime } from "../../../../utils/relativeTime";

/**
 * Where a repository stands with the wiki. `unavailable` is a deployment fact
 * (no graph extra), the other two are per-repository state.
 */
export type WikiState = "unavailable" | "not-indexed" | "indexed";

/** One visible slice of the filtered row list. */
export interface RepositoryPage {
  rows: RepositoryRow[];
  /** Clamped page index — a filter that shrinks the list strands the caller past the end. */
  page: number;
  pageCount: number;
  /** 1-based inclusive bounds of the slice, for "Showing 1 to 10 of 23". */
  from: number;
  to: number;
  total: number;
}

export class RepositoryRow {
  readonly slug: string;
  readonly host: string;
  readonly owner: string;
  readonly repo: string;
  /**
   * The canonical URL the registry recorded. Also what a credential probe
   * names, because the validate endpoint wants a repository to reach even when
   * the credential under test is scoped to the whole host.
   */
  readonly repoUrl: string;
  readonly platform: RepositoryPlatform;
  /** Operator-chosen display name, or null when the repo name stands alone. */
  readonly name: string | null;
  /** False when the deployment ships no graph extra: no wiki is possible at all. */
  readonly wikiAvailable: boolean;
  readonly indexed: boolean;
  /** Last index timestamp; null when absent or unparseable (never indexed). */
  readonly indexedAt: string | null;
  readonly pages: number;
  readonly tasks: RepositoryTasksUsage | null;
  /** Server-resolved credential identity, never a client-side guess. */
  readonly credential: RepositoryCredentialUsage | null;

  /** Pre-lowercased search haystack — the filter runs on every keystroke. */
  private readonly _haystack: string;

  private constructor(dto: RepositoryDTO) {
    this.slug = dto.slug;
    this.host = dto.host;
    this.owner = dto.owner;
    this.repo = dto.repo;
    this.repoUrl = dto.repoUrl;
    this.platform = dto.platform;
    this.name = dto.name;

    const wiki = dto.usage.wiki;
    this.wikiAvailable = wiki !== null;
    this.indexed = wiki?.indexed ?? false;
    // An unparseable stamp is indistinguishable from an absent one to a reader,
    // and both mean "we can't say when" — collapse them to null so the column
    // renders its placeholder instead of echoing raw wire text.
    this.indexedAt =
      wiki?.indexedAt && !Number.isNaN(RelativeTime.parse(wiki.indexedAt))
        ? wiki.indexedAt
        : null;
    // `pages` is nullable on the wire (a record written before the counter
    // existed). Zero and unknown both mean "nothing to state", and the column
    // only renders a count above zero, so collapsing them is honest here.
    this.pages = wiki?.pages ?? 0;
    this.tasks = dto.usage.tasks;
    this.credential = dto.usage.credential;

    this._haystack = [dto.name, dto.repo, dto.owner, dto.host, dto.slug]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
  }

  // ── Factories ───────────────────────────────────────────────────────

  static fromDto(dto: RepositoryDTO): RepositoryRow {
    return new RepositoryRow(dto);
  }

  /**
   * Build every row, ordered by the name the reader actually scans. Storage
   * order is not display order: the list arrives however the store enumerated
   * it, so the sort belongs here rather than in whichever component renders
   * next.
   */
  static build(repositories: RepositoryDTO[]): RepositoryRow[] {
    return repositories
      .map((dto) => RepositoryRow.fromDto(dto))
      .sort(
        (a, b) =>
          a.displayName().localeCompare(b.displayName()) ||
          a.owner.localeCompare(b.owner) ||
          a.host.localeCompare(b.host),
      );
  }

  // ── Row behaviour ───────────────────────────────────────────────────

  /** What the Name column shows: the operator's label, else the repo name. */
  displayName(): string {
    return this.name?.trim() || this.repo;
  }

  /** Does this row satisfy the search box? An empty query matches everything. */
  matches(query: string): boolean {
    const needle = query.trim().toLowerCase();
    if (!needle) return true;
    return this._haystack.includes(needle);
  }

  /** Where this repository stands with the wiki, as one closed token. */
  wikiState(): WikiState {
    if (!this.wikiAvailable) return "unavailable";
    return this.indexed ? "indexed" : "not-indexed";
  }

  /** "Indexed 6 days ago", or null when there is no usable timestamp. */
  indexedLabel(): string | null {
    return this.indexedAt ? `Indexed ${RelativeTime.format(this.indexedAt)}` : null;
  }

  /** Absolute timestamp for the relative label's tooltip. */
  indexedTitle(): string {
    return this.indexedAt ? RelativeTime.tooltip(this.indexedAt) : "";
  }

  /** Owner and host as one muted subtitle. */
  ownerLabel(): string {
    return `${this.owner} · ${this.host}`;
  }

  /**
   * What the tasks chip says, or null when no workspace is set up. Only
   * `projectId` is guaranteed on the wire, so this falls through name to path
   * to a bare statement that one exists: a chip whose label resolved to null
   * would render as an empty box that reads as a rendering bug rather than as
   * the "we know less about this one" it actually is.
   */
  tasksLabel(): string | null {
    if (!this.tasks) return null;
    return this.tasks.name?.trim() || this.tasks.path?.trim() || "Ready for tasks";
  }

  /** Tooltip for the tasks chip, which can only name a path when it has one. */
  tasksTitle(): string {
    if (!this.tasks) return "No workspace is set up for agentic tasks";
    return this.tasks.path
      ? `Agentic tasks run in ${this.tasks.path}`
      : "A workspace is set up for agentic tasks";
  }

  // ── Collection behaviour ────────────────────────────────────────────

  static filter(rows: RepositoryRow[], query: string): RepositoryRow[] {
    if (!query.trim()) return rows;
    return rows.filter((row) => row.matches(query));
  }

  /**
   * Cut *rows* into one page. `page` is clamped rather than trusted: a search
   * that shrinks the list below the caller's current page would otherwise
   * render an empty table with no way back.
   */
  static paginate(
    rows: RepositoryRow[],
    page: number,
    size: number,
  ): RepositoryPage {
    const total = rows.length;
    const pageCount = Math.max(1, Math.ceil(total / size));
    const clamped = Math.min(Math.max(page, 0), pageCount - 1);
    const start = clamped * size;
    const slice = rows.slice(start, start + size);
    return {
      rows: slice,
      page: clamped,
      pageCount,
      from: total === 0 ? 0 : start + 1,
      to: start + slice.length,
      total,
    };
  }
}
