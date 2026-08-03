/**
 * RepositoryRowMenu — the per-row `⋯` overflow for the repository registry.
 *
 * Every item is an EXPLICIT opt-in, and the labels carry the whole point of
 * this surface: registering a repository does nothing on its own, so each
 * product's action says which product it starts. "Generate wiki" is the worked
 * example — the button it replaces said "Add", which read as "add a repository"
 * while actually launching a paid multi-minute indexing run.
 *
 * Two destructive items sit here and they are deliberately never the same
 * button. "Delete wiki index" destroys generated documentation and leaves the
 * repository registered; "Remove from Mewbo" deregisters the repository and
 * leaves the wiki, the managed project and the stored credential alone. A
 * single "delete" would have to pick one meaning and be wrong about the other,
 * so the menu offers both and the confirm copy states what each one destroys.
 *
 * Every item here is backed by an endpoint that already exists; nothing is
 * stubbed. That is why the menu changes shape with the row rather than
 * disabling items: a deployment with no graph extra can never generate a wiki,
 * so it is offered no wiki actions at all, and a repository already covered by
 * a stored credential offers validate where an uncovered one does not.
 *
 * The menu decides only WHICH items a row can show. What each item DOES is the
 * pane's business — items report a narrow `RepositoryRowAction` to one
 * dispatcher, so a new action is a compile error until it is handled.
 */
import {
  BookOpen,
  FolderGit2,
  KeyRound,
  MoreHorizontal,
  RefreshCw,
  ShieldCheck,
  Settings2,
  Sparkles,
  Trash2,
  Unlink,
} from "lucide-react";

import { Button } from "../../../ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../../../ui/dropdown-menu";
import type { RepositoryRow } from "./repositoryRows";

export type RepositoryRowAction =
  | "open-wiki"
  | "generate-wiki"
  | "reindex"
  | "wiki-settings"
  | "set-up-tasks"
  | "credential"
  | "credential-validate"
  | "delete-index"
  | "deregister";

export interface RepositoryRowMenuProps {
  row: RepositoryRow;
  onAction: (action: RepositoryRowAction) => void;
  /** A generated wiki has a landing page to deep-link into. */
  canOpenWiki?: boolean;
  /** A mutation is in flight for this row: the menu opens but nothing fires. */
  busy?: boolean;
}

const DESTRUCTIVE_ITEM_CLS =
  "text-[hsl(var(--destructive-text))] focus:text-[hsl(var(--destructive-text))]";

export function RepositoryRowMenu({
  row,
  onAction,
  canOpenWiki = false,
  busy = false,
}: RepositoryRowMenuProps) {
  const wikiState = row.wikiState();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          aria-label={`Actions for ${row.displayName()}`}
          title={`Actions for ${row.displayName()}`}
        >
          <MoreHorizontal className="w-4 h-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[14rem]">
        {wikiState === "not-indexed" && (
          <DropdownMenuItem disabled={busy} onSelect={() => onAction("generate-wiki")}>
            <Sparkles />
            Generate wiki
          </DropdownMenuItem>
        )}

        {wikiState === "indexed" && (
          <>
            {canOpenWiki && (
              <DropdownMenuItem onSelect={() => onAction("open-wiki")}>
                <BookOpen />
                Open wiki
              </DropdownMenuItem>
            )}
            <DropdownMenuItem disabled={busy} onSelect={() => onAction("reindex")}>
              <RefreshCw />
              Re-index wiki
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => onAction("wiki-settings")}>
              <Settings2 />
              Wiki settings
            </DropdownMenuItem>
          </>
        )}

        {wikiState !== "unavailable" && <DropdownMenuSeparator />}

        {/*
          Offered only while there is nothing to use: a repository that already
          has a checkout reports it in the tasks column, and a second item that
          did nothing would be worse than no item. Labelled "Set up for tasks"
          rather than "Use in tasks" BECAUSE IT CLONES — "use" describes
          switching to something that already exists, and this one spends
          network, disk and a credential and can run for minutes. It shares the
          verb with the composer's own row so one vocabulary covers both places
          a checkout can be started from.
        */}
        {!row.tasks && (
          <DropdownMenuItem disabled={busy} onSelect={() => onAction("set-up-tasks")}>
            <FolderGit2 />
            Set up for tasks
          </DropdownMenuItem>
        )}

        <DropdownMenuItem onSelect={() => onAction("credential")}>
          <KeyRound />
          Manage credential
        </DropdownMenuItem>
        {row.credential && (
          <DropdownMenuItem
            disabled={busy}
            onSelect={() => onAction("credential-validate")}
          >
            <ShieldCheck />
            Validate credential
          </DropdownMenuItem>
        )}

        <DropdownMenuSeparator />

        {wikiState === "indexed" && (
          <DropdownMenuItem
            className={DESTRUCTIVE_ITEM_CLS}
            onSelect={() => onAction("delete-index")}
          >
            <Trash2 />
            Delete wiki index
          </DropdownMenuItem>
        )}
        <DropdownMenuItem
          className={DESTRUCTIVE_ITEM_CLS}
          onSelect={() => onAction("deregister")}
        >
          <Unlink />
          Remove from Mewbo
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
