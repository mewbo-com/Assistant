/**
 * Frontmatter shape shared by wiki pages.
 *
 * `getPage()` (`api/client.ts`) returns `WikiPage` pre-parsed (frontmatter
 * split, TOC derived) from the server, so nothing here re-parses raw `.md`
 * source.
 */

import type { TocEntry } from "./types";

export interface PageFrontmatter {
  title: string;
  slug: string;
  relevantSources?: Array<{ path: string; lines?: string }>;
  sources?: Array<{ path: string; lines?: string }>;
  tocOverride?: TocEntry[];
}
