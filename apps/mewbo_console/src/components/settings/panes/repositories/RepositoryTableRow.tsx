/**
 * One row of the repository registry table, and the usage chips inside it.
 *
 * Presentation only: every fact it renders is already decided by
 * `RepositoryRow`, and every action it offers is reported upward as a
 * `RepositoryRowAction` for the pane's one dispatcher to run. It lives beside
 * `RepositoryRowMenu` because the two together are the row.
 *
 * The usage chips are the reason the table exists. Each states what one product
 * is currently doing with the repository, so removing it can be an informed
 * decision rather than a guess.
 */
import type { ReactNode } from "react";
import { BookOpen, KeyRound, ListChecks } from "lucide-react";

import { cn } from "../../../../lib/utils";
import { TableCell, TableRow } from "../../../ui/table";
import { RepositoryRowMenu, type RepositoryRowAction } from "./RepositoryRowMenu";
import type { RepositoryRow } from "./repositoryRows";

/**
 * One usage fact. `active` means a product is actually using this repository,
 * which is the only distinction the eye needs while scanning a column of these;
 * "not yet" is a neutral state, never a warning.
 */
function UsageChip({
  icon,
  label,
  active,
  title,
}: {
  icon: ReactNode;
  label: string;
  active: boolean;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-2xs leading-none",
        active
          ? "border-[hsl(var(--success))]/25 bg-[hsl(var(--success))]/10 text-[hsl(var(--foreground))]"
          : "border-[hsl(var(--border))] bg-[hsl(var(--muted))]/40 text-[hsl(var(--muted-foreground))]",
      )}
    >
      {icon}
      {label}
    </span>
  );
}

/** The wiki chip's copy, which has three states rather than two. */
function wikiChipLabel(row: RepositoryRow): string {
  switch (row.wikiState()) {
    case "unavailable":
      return "Wiki unavailable";
    case "indexed":
      return row.indexedLabel() ?? "Wiki generated";
    case "not-indexed":
      return "No wiki yet";
  }
}

export interface RepositoryTableRowProps {
  row: RepositoryRow;
  /** A mutation is in flight for this row: the menu opens but nothing fires. */
  busy: boolean;
  /** Where the name links to, or null when no generated wiki has a landing page. */
  wikiHref: string | null;
  onNavigate: (href: string) => void;
  onAction: (action: RepositoryRowAction) => void;
}

export function RepositoryTableRow({
  row,
  busy,
  wikiHref: href,
  onNavigate,
  onAction,
}: RepositoryTableRowProps) {
  const name = row.displayName();
  const indexed = row.wikiState() === "indexed";

  return (
    <TableRow>
      <TableCell>
        <div className="min-w-0">
          {href ? (
            // A real href keeps middle-click and "copy link" working; the
            // click handler keeps the in-app navigation from reloading the SPA.
            <a
              href={href}
              className="text-sm text-[hsl(var(--foreground))] hover:text-[hsl(var(--primary))] hover:underline"
              onClick={(event) => {
                if (event.metaKey || event.ctrlKey || event.shiftKey) return;
                event.preventDefault();
                onNavigate(href);
              }}
            >
              {name}
            </a>
          ) : (
            <span className="text-sm text-[hsl(var(--foreground))]">{name}</span>
          )}
          <p className="text-xs text-[hsl(var(--muted-foreground))]">{row.ownerLabel()}</p>
        </div>
      </TableCell>

      <TableCell>
        <div className="flex flex-wrap items-center gap-1.5">
          <UsageChip
            icon={<BookOpen className="w-3 h-3" />}
            label={wikiChipLabel(row)}
            active={indexed}
            title={
              indexed
                ? [row.indexedTitle(), row.pages > 0 ? `${row.pages} pages` : ""]
                    .filter(Boolean)
                    .join(" · ")
                : row.wikiState() === "unavailable"
                  ? "This deployment cannot generate wikis"
                  : "No wiki has been generated for this repository"
            }
          />
          <UsageChip
            icon={<ListChecks className="w-3 h-3" />}
            label={row.tasksLabel() ?? "Tasks not set up"}
            active={row.tasks !== null}
            title={row.tasksTitle()}
          />
          <UsageChip
            icon={<KeyRound className="w-3 h-3" />}
            label={row.credential ? row.credential.scope : "Server git config"}
            active={row.credential !== null}
            title={
              row.credential
                ? row.credential.scopeType === "host"
                  ? "A credential shared by every repository on this host"
                  : "A credential pinned to this repository"
                : "No stored credential covers this repository, so the server's git configuration is used"
            }
          />
        </div>
      </TableCell>

      <TableCell className="text-right">
        <RepositoryRowMenu
          row={row}
          busy={busy}
          canOpenWiki={href !== null}
          onAction={onAction}
        />
      </TableCell>
    </TableRow>
  );
}
