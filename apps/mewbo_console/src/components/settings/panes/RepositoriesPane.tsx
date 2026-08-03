/**
 * RepositoriesPane — every repository Mewbo knows about, one row each.
 *
 * **It reads the registry, not the wiki.** Wiki `Project` rows are only ever
 * written at index finalize, so sourcing rows from `useWikiProjects()` would
 * mean a repository that had never been indexed could not appear at all, and
 * the table could never answer the question it exists to answer. It reads
 * `/v1/git/repositories`, which is the superset: a repository is there
 * because someone registered it, and a wiki-indexed one is there too.
 *
 * **Registering is a no-op; every consumer opts in explicitly.** The primary
 * action opens `AddRepositoryDialog`, which POSTs one record and closes. It does
 * not navigate, does not open a wizard, and starts no run. The row menu is where
 * a product is switched on, with labels that say which one ("Generate wiki", not
 * "Add"), because the button this replaced navigated out of Settings into the
 * wiki wizard and began a paid multi-minute indexing run.
 *
 * **The usage column is what makes removing a repository an informed decision.**
 * Each row states, from the server-computed `usage`, whether a wiki exists,
 * whether agentic tasks have a workspace for it, and which stored credential
 * reaches it. So the two destructive actions can stay honestly separate:
 * "Delete wiki index" destroys documentation, "Remove from Mewbo" forgets the
 * repository and leaves everything else standing.
 *
 * **The list self-heals, so deregistering an indexed repository is temporary.**
 * `GET /v1/git/repositories` adopts any wiki-indexed project missing from the
 * registry as `origin:"wiki"`, and DELETE deregisters only, so a repository that
 * still has a wiki comes back on the next read. That is server behaviour we
 * cannot hide, so the deregister confirm states it and points at "Delete wiki
 * index" instead: a confirm that promised the row would disappear would be
 * lying, and the reappearance would read as a bug.
 *
 * The one wiki read that remains is enrichment, never truth: `useWikiProjects`
 * supplies `landingPageId` so an indexed row can deep-link into its wiki. Which
 * repositories exist comes from the registry and whether one is indexed comes
 * from `usage.wiki`; if that query fails or the deployment ships no graph extra,
 * the name simply stops being a link.
 *
 * Chrome-agnostic per the pane contract: bare `<SettingsCard>`, zero props, no
 * page width or padding of its own.
 */
import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useLocation } from "wouter";
import { toast } from "sonner";
import {
  ChevronLeft,
  ChevronRight,
  FolderGit2,
  Loader2,
  Plus,
  Search,
  Trash2,
  Unlink,
} from "lucide-react";

import { validateGitCredential, type GitCredentialSummary } from "../../../api/git";
import { useGitCredentials } from "../../../hooks/useGitCredentials";
import {
  useCheckoutRepository,
  useDeleteRepository,
  useRepositories,
} from "../../../hooks/useRepositories";
import { ErrorAlert } from "../../ErrorAlert";
import { Button } from "../../ui/button";
import { Input } from "../../ui/input";
import { ConfirmDialog } from "../../ui/confirm-dialog";
import { Table, TableBody, TableHead, TableHeader, TableRow } from "../../ui/table";
import {
  GitCredentialDialog,
  type GitCredentialDialogState,
} from "../../git-credentials/GitCredentialDialog";
import { ProjectSettingsDialog } from "../../wiki/ProjectSettingsDialog";
import {
  useDeleteProject,
  useRequestWikiRefresh,
  useWikiProjects,
} from "../../wiki/api/hooks";
import { buildHref } from "../../wiki/router";
import { SettingsCard } from "../SettingsCard";
import { AddRepositoryDialog } from "./repositories/AddRepositoryDialog";
import { RepositoryRow } from "./repositories/repositoryRows";
import type { RepositoryRowAction } from "./repositories/RepositoryRowMenu";
import { RepositoryTableRow } from "./repositories/RepositoryTableRow";

const PAGE_SIZE = 10;

const REPOSITORIES_HELP = [
  "Every repository Mewbo knows about, and what each product is currently doing with it.",
  "",
  "A repository is registered once and then used by whichever products you point at it. " +
    "Registering one here records where it lives and nothing more: it is not cloned, it is not " +
    "indexed, and no model runs, so it is instant and costs nothing. Each product opts in " +
    "afterwards from the row menu, and the usage column says which ones already have. " +
    "Generating a wiki is the step that clones the repository and writes documentation for it. " +
    "The credential is how Mewbo authenticates: a credential pinned to one repository wins over " +
    "a credential shared by every repository on the same host, and a repository covered by " +
    "neither falls back to the git configuration on the server. Removing a repository makes " +
    "Mewbo forget the repository itself, and leaves any wiki, workspace or credential exactly " +
    "where they are.",
].join("\n");

/**
 * Seed for "add a credential pinned to this repository". The dialog reads
 * scope, kind and username off `cred` to prefill its form, and in add mode
 * leaves the scope field editable, so a synthetic summary is how a row hands
 * the operator its own slug without them retyping it. Nothing else on the
 * shape is read back out.
 */
function credentialSeedFor(slug: string): GitCredentialSummary {
  return {
    scope: slug,
    scopeType: "repo",
    kind: "token",
    username: null,
    valueHint: "",
    updatedAt: null,
  };
}

export function RepositoriesPane() {
  const [, navigate] = useLocation();
  const {
    repositories,
    loading: repositoriesLoading,
    error: repositoriesError,
  } = useRepositories();
  const {
    credentials,
    loading: credentialsLoading,
    error: credentialsError,
    refresh: refreshCredentials,
  } = useGitCredentials();

  // Enrichment only: the wiki landing page a row's name links to. Errors here
  // are deliberately swallowed — a failed enrichment costs a hyperlink, and
  // surfacing it as a banner would report the wiki being unavailable as though
  // the repository list had failed to load.
  const wikiProjects = useWikiProjects();
  const landingPages = useMemo(() => {
    const map = new Map<string, string>();
    for (const project of wikiProjects.data ?? []) {
      if (project.landingPageId) map.set(project.slug, project.landingPageId);
    }
    return map;
  }, [wikiProjects.data]);

  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [addOpen, setAddOpen] = useState(false);
  const [credentialDialog, setCredentialDialog] = useState<GitCredentialDialogState>(null);
  const [settingsSlug, setSettingsSlug] = useState<string | null>(null);
  const [deleteIndexTarget, setDeleteIndexTarget] = useState<RepositoryRow | null>(null);
  const [deregisterTarget, setDeregisterTarget] = useState<RepositoryRow | null>(null);

  const deleteIndexM = useDeleteProject();
  const deregisterM = useDeleteRepository();
  const checkoutM = useCheckoutRepository();
  const refreshM = useRequestWikiRefresh();
  const validateM = useMutation({
    mutationFn: (input: { slug: string; scope: string; repoUrl: string }) =>
      validateGitCredential(input.scope, input.repoUrl),
    // A probe that reaches the host and gets refused is a successful request
    // carrying a failed result, so the verdict is read off `ok`, not the throw.
    onSuccess: (result, input) => {
      if (result.ok) toast.success(`${input.scope} authenticated`, { description: result.detail });
      else toast.error(`${input.scope} could not authenticate`, { description: result.detail });
    },
    onError: (error) =>
      toast.error(
        `Could not check the credential: ${error instanceof Error ? error.message : String(error)}`,
      ),
  });

  const rows = useMemo(() => RepositoryRow.build(repositories), [repositories]);
  const filtered = useMemo(() => RepositoryRow.filter(rows, query), [rows, query]);
  const view = RepositoryRow.paginate(filtered, page, PAGE_SIZE);

  const loading = repositoriesLoading || credentialsLoading;
  const busySlug =
    (refreshM.isPending ? refreshM.variables?.slug : null) ??
    (validateM.isPending ? validateM.variables?.slug : null) ??
    (deregisterM.isPending ? deregisterM.variables : null) ??
    (checkoutM.isPending ? checkoutM.variables : null) ??
    null;

  /**
   * Open the shared credential dialog for one repository: edit when a stored
   * credential already covers it, add seeded with its slug otherwise. The
   * covering scope is the server's answer (`usage.credential`); the full record
   * is looked up in the credential list only to prefill kind and username.
   */
  const openCredentialFor = (row: RepositoryRow) => {
    const scope = row.credential?.scope;
    const stored = scope ? credentials.find((cred) => cred.scope === scope) : undefined;
    if (stored) setCredentialDialog({ mode: "edit", cred: stored });
    else setCredentialDialog({ mode: "add", cred: credentialSeedFor(row.slug) });
  };

  const runAction = (row: RepositoryRow, action: RepositoryRowAction) => {
    switch (action) {
      case "open-wiki": {
        const href = wikiHref(row, landingPages);
        if (href) navigate(href);
        return;
      }
      case "generate-wiki":
        // The wiki wizard is where an index is configured and started. This is
        // the ONE place in the pane that navigates, and it is behind a row
        // action labelled with what it does.
        //
        // Hand it the SLUG, not the url. The wizard seeds url, platform and
        // default branch from the registry record, and its seed deliberately
        // yields to an explicit `?url=` — so passing the url instead would
        // fill one field and silently drop the other two.
        navigate(buildHref({ kind: "configure", repo: row.slug }));
        return;
      case "reindex":
        // No mode: this row action is the ordinary "bring it up to date" verb,
        // so it keeps the default `auto`. The deliberate full rebuild lives on
        // the wiki's own index-status card, where its cost can be stated.
        refreshM.mutate({ slug: row.slug }, {
          onSuccess: () =>
            toast.success(`Re-index queued for ${row.displayName()}`, {
              description: "The wiki updates once the run finishes.",
            }),
          onError: (error) =>
            toast.error(
              `Could not queue a re-index: ${error instanceof Error ? error.message : String(error)}`,
            ),
        });
        return;
      case "wiki-settings":
        setSettingsSlug(row.slug);
        return;
      case "set-up-tasks":
        // No confirm dialog: a checkout is additive and idempotent, so the
        // worst a stray click costs is a wait. What it DOES cost is stated up
        // front, because a synchronous clone with no warning reads as a hung
        // menu. The row is busy for the duration (`busySlug`), and the pending
        // toast is dismissed by id when the mutation settles either way.
        toast.loading(`Cloning ${row.displayName()}`, {
          id: `checkout-${row.slug}`,
          description: "This can take a few minutes on a large repository.",
        });
        checkoutM.mutate(row.slug, {
          onSuccess: (record) =>
            toast.success(`${row.displayName()} is ready for tasks`, {
              id: `checkout-${row.slug}`,
              description: record.usage.tasks?.path
                ? `Checked out to ${record.usage.tasks.path}`
                : "Pick it in the composer's project menu.",
            }),
          onError: (error) =>
            toast.error(`Could not check out ${row.displayName()}`, {
              id: `checkout-${row.slug}`,
              description: error instanceof Error ? error.message : String(error),
            }),
        });
        return;
      case "credential":
        openCredentialFor(row);
        return;
      case "credential-validate":
        if (row.credential) {
          validateM.mutate({
            slug: row.slug,
            scope: row.credential.scope,
            repoUrl: row.repoUrl,
          });
        }
        return;
      case "delete-index":
        setDeleteIndexTarget(row);
        return;
      case "deregister":
        setDeregisterTarget(row);
        return;
    }
  };

  return (
    <div className="space-y-4">
      <SettingsCard
        id="settings-repositories"
        title="Repositories"
        description={REPOSITORIES_HELP}
        actions={
          <Button
            variant="primary"
            size="md"
            onClick={() => setAddOpen(true)}
            leadingIcon={<Plus className="w-4 h-4" />}
          >
            Add repository
          </Button>
        }
      >
        {repositoriesError && (
          <ErrorAlert
            error={repositoriesError}
            fallback="Failed to load repositories"
            className="mb-3"
          />
        )}
        {credentialsError && (
          <ErrorAlert
            error={credentialsError}
            fallback="Failed to load git credentials"
            className="mb-3"
          />
        )}
        {deleteIndexM.error && (
          <ErrorAlert
            error={deleteIndexM.error}
            fallback="Failed to delete the wiki index"
            className="mb-3"
          />
        )}
        {deregisterM.error && (
          <ErrorAlert
            error={deregisterM.error}
            fallback="Failed to remove the repository"
            className="mb-3"
          />
        )}

        {loading && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
          </div>
        )}

        {!loading && rows.length === 0 && (
          <div className="rounded-lg border border-dashed border-[hsl(var(--border))] px-4 py-6 text-center">
            <FolderGit2 className="w-6 h-6 mx-auto text-[hsl(var(--muted-foreground))] opacity-40" />
            <p className="mt-2 text-sm text-[hsl(var(--muted-foreground))]">
              No repositories yet.
            </p>
            <p className="mt-1 mx-auto max-w-prose text-xs text-[hsl(var(--muted-foreground))]">
              Add one to tell Mewbo where it lives. Registering takes a moment and starts nothing:
              each product then opts in on its own, so you decide when to generate a wiki for it or
              give agentic tasks a workspace on it. Private repositories need a credential before
              anything can clone them.
            </p>
          </div>
        )}

        {!loading && rows.length > 0 && (
          <>
            <div className="relative mb-3 max-w-sm">
              <Search
                aria-hidden
                className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-[hsl(var(--muted-foreground))]"
              />
              <Input
                type="search"
                value={query}
                aria-label="Search repositories"
                placeholder="Search repositories…"
                className="pl-8"
                onChange={(event) => {
                  setQuery(event.target.value);
                  // A narrower list can be shorter than the current page; land
                  // the reader on results rather than on an empty tail.
                  setPage(0);
                }}
              />
            </div>

            {filtered.length === 0 ? (
              <div className="rounded-lg border border-dashed border-[hsl(var(--border))] px-4 py-6 text-center">
                <p className="text-sm text-[hsl(var(--muted-foreground))]">
                  No repositories match that search.
                </p>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Used by</TableHead>
                    <TableHead className="w-10 text-right">
                      <span className="sr-only">Actions</span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {view.rows.map((row) => (
                    <RepositoryTableRow
                      key={row.slug}
                      row={row}
                      busy={row.slug === busySlug}
                      wikiHref={wikiHref(row, landingPages)}
                      onNavigate={navigate}
                      onAction={(action) => runAction(row, action)}
                    />
                  ))}
                </TableBody>
              </Table>
            )}

            {view.pageCount > 1 && (
              <div className="mt-3 flex items-center justify-between gap-3">
                <p className="text-xs text-[hsl(var(--muted-foreground))]">
                  Showing {view.from} to {view.to} of {view.total}
                </p>
                <div className="flex items-center gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    iconOnly
                    aria-label="Previous page"
                    title="Previous page"
                    disabled={view.page === 0}
                    onClick={() => setPage(view.page - 1)}
                  >
                    <ChevronLeft className="w-4 h-4" />
                  </Button>
                  <span className="text-xs text-[hsl(var(--muted-foreground))]">
                    Page {view.page + 1} of {view.pageCount}
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    iconOnly
                    aria-label="Next page"
                    title="Next page"
                    disabled={view.page >= view.pageCount - 1}
                    onClick={() => setPage(view.page + 1)}
                  >
                    <ChevronRight className="w-4 h-4" />
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </SettingsCard>

      <AddRepositoryDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        // The credential form opens only after the record exists, seeded with
        // the slug the SERVER normalized, so a scope can never disagree with
        // the repository it is supposed to reach.
        onRegistered={(repository, addCredential) => {
          toast.success(`${repository.slug} registered`, {
            description: "Nothing has been cloned or indexed yet.",
          });
          if (addCredential) {
            setCredentialDialog({ mode: "add", cred: credentialSeedFor(repository.slug) });
          }
        }}
      />

      <GitCredentialDialog
        state={credentialDialog}
        onClose={() => setCredentialDialog(null)}
        onSaved={refreshCredentials}
      />

      {/* The same dialog the wiki gallery and top bar open, mounted only while
          a row is actually being edited. */}
      {settingsSlug && (
        <ProjectSettingsDialog
          slug={settingsSlug}
          open
          onOpenChange={(open) => {
            if (!open) setSettingsSlug(null);
          }}
        />
      )}

      <ConfirmDialog
        open={deleteIndexTarget !== null}
        title="Delete wiki index?"
        description={
          <>
            This deletes the generated wiki for{" "}
            <span className="font-mono font-medium text-[hsl(var(--foreground))]">
              {deleteIndexTarget?.slug}
            </span>{" "}
            along with its indexing settings and any credential pinned to that one repository. A
            credential shared across the whole host is left alone. The repository stays registered
            with Mewbo, so you can generate the wiki again whenever you want.
          </>
        }
        confirmLabel="Delete wiki index"
        pendingLabel="Deleting…"
        confirmIcon={<Trash2 className="w-4 h-4" />}
        pending={deleteIndexM.isPending}
        onConfirm={() => {
          if (!deleteIndexTarget) return;
          deleteIndexM.mutate(deleteIndexTarget.slug, {
            // The server deletes the repo-scoped credential with the project,
            // so both the credential list and the registry's `usage` are stale
            // the moment the delete lands.
            onSuccess: () => refreshCredentials(),
            onSettled: () => setDeleteIndexTarget(null),
          });
        }}
        onCancel={() => setDeleteIndexTarget(null)}
      />

      <ConfirmDialog
        open={deregisterTarget !== null}
        title="Remove this repository from Mewbo?"
        description={
          <>
            Mewbo forgets{" "}
            <span className="font-mono font-medium text-[hsl(var(--foreground))]">
              {deregisterTarget?.slug}
            </span>
            . Nothing else is deleted: any workspace and any stored credential stay exactly as
            they are, and you can register the repository again at any time.
            {deregisterTarget?.wikiState() === "indexed" && (
              <>
                {" "}
                Its generated wiki is not deleted either, and because a repository with a wiki is
                added back to this list automatically, this one will reappear. Delete the wiki
                index first if you want it gone for good.
              </>
            )}
          </>
        }
        confirmLabel="Remove repository"
        pendingLabel="Removing…"
        confirmIcon={<Unlink className="w-4 h-4" />}
        pending={deregisterM.isPending}
        onConfirm={() => {
          if (!deregisterTarget) return;
          deregisterM.mutate(deregisterTarget.slug, {
            onSettled: () => setDeregisterTarget(null),
          });
        }}
        onCancel={() => setDeregisterTarget(null)}
      />
    </div>
  );
}

/**
 * Where a row's name links to, or null when no generated wiki has a landing
 * page for it. `landingPageId` is the canonical "enter this wiki" target; there
 * is no universal sentinel page id to fall back on.
 */
function wikiHref(row: RepositoryRow, landingPages: Map<string, string>): string | null {
  if (row.wikiState() !== "indexed") return null;
  const pageId = landingPages.get(row.slug);
  if (!pageId) return null;
  return buildHref({
    kind: "page",
    pageId,
    slug: row.slug,
    platform: row.platform,
  });
}
