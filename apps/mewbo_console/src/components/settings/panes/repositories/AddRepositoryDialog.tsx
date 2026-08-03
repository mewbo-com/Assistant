/**
 * AddRepositoryDialog — register a repository with Mewbo.
 *
 * **Registering is a no-op, and the copy has to say so.** The button this
 * replaces navigated out of Settings into the wiki onboarding wizard, which
 * cloned the repository and started a paid multi-minute indexing run. Here the
 * whole submit is one POST that normalizes a URL, validates it and persists a
 * record. Nothing clones, nothing indexes, no model runs. Each product opts in
 * afterwards from the row's own menu.
 *
 * Built on the `GitCredentialDialog` template (shadcn `Dialog` + react-hook-form
 * + zod), so the surface has one form idiom rather than two.
 *
 * **It grows no second credential form.** A repository already covered by a
 * stored credential says so; one that is not offers to open the EXISTING
 * `GitCredentialDialog` seeded with the canonical slug, after the record
 * exists. That ordering is deliberate: the server owns slug normalization, so
 * handing the credential form a slug the server just minted is the only way the
 * scope cannot disagree with the repository it is meant to reach.
 */
import { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Loader2, Plus } from "lucide-react";

import { hostOf, matchCredential } from "../../../../api/git";
import {
  isRepositoryError,
  type RepositoryDTO,
} from "../../../../api/repositories";
import { useGitCredentials } from "../../../../hooks/useGitCredentials";
import { useCreateRepository } from "../../../../hooks/useRepositories";
import { ErrorAlert } from "../../../ErrorAlert";
import { Button } from "../../../ui/button";
import { Input } from "../../../ui/input";
import { Switch } from "../../../ui/switch";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../../ui/dialog";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "../../../ui/form";
import { useWikiPlatforms } from "../../../wiki/api/hooks";
import { registryPlatformFromUrl } from "../../../wiki/configure-wizard/wizardState";
import { parseSlug, slugFromRepoUrl } from "../../../wiki/slug";

const repositorySchema = z.object({
  repoUrl: z.string().trim().min(1, "Repository URL is required"),
  defaultBranch: z.string().trim().optional(),
  name: z.string().trim().optional(),
});

type RepositoryValues = z.infer<typeof repositorySchema>;

const EMPTY_FORM: RepositoryValues = { repoUrl: "", defaultBranch: "", name: "" };

/** Codes that belong on the URL field rather than in the dialog's banner. */
const FIELD_PINNED_CODES = ["repository_exists", "invalid_repo_url"] as const;

export interface AddRepositoryDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * The record the server created, plus whether the operator asked to store a
   * credential for it. The pane opens the shared credential dialog on `true`;
   * this component never renders one itself.
   */
  onRegistered: (repository: RepositoryDTO, addCredential: boolean) => void;
}

export function AddRepositoryDialog({
  open,
  onOpenChange,
  onRegistered,
}: AddRepositoryDialogProps) {
  const form = useForm<RepositoryValues>({
    resolver: zodResolver(repositorySchema),
    defaultValues: EMPTY_FORM,
  });
  const [addCredential, setAddCredential] = useState(false);

  const createM = useCreateRepository();
  const { credentials } = useGitCredentials(open);
  const platformsQuery = useWikiPlatforms();

  // Seed a clean form every time the dialog opens, so a cancelled attempt never
  // leaks its half-typed URL into the next one.
  useEffect(() => {
    if (open) {
      form.reset(EMPTY_FORM);
      setAddCredential(false);
      createM.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const repoUrl = form.watch("repoUrl");
  const trimmedUrl = repoUrl.trim();

  // The canonical identity the server will derive, previewed live. `null` while
  // the URL is not yet a full one, which is the honest answer: the client does
  // not get to guess a slug the server has not normalized.
  const slug = useMemo(
    () => (trimmedUrl ? slugFromRepoUrl(trimmedUrl) : null),
    [trimmedUrl],
  );
  // The repo name alone, as the display-name placeholder. `parseSlug` is the
  // console's one slug parser; a second one lived on the row model until it
  // was found to disagree with this one about nested groups.
  const repoName = useMemo(
    () => (slug ? (parseSlug(slug)?.repo ?? null) : null),
    [slug],
  );
  // The platform the server will record, previewed by name rather than id.
  //
  // `registryPlatformFromUrl`, NOT the wizard's `detectPlatformFromUrl`: the
  // wizard guesses from the hostname (a `git.` prefix reads as Gitea) because
  // its answer only pre-selects a tile the user can click away from. This one
  // sits next to "will be registered as", so it is a claim about a value the
  // server is about to persist, and the server's rule is exact host or
  // dot-subdomain with no guessing. Saying "on Gitea" for a self-hosted
  // `git.example.com` that gets stored as `git` is the disagreement this
  // avoids. Resolving the id back through the catalogue keeps it quiet rather
  // than confident when there is no catalogue to match against.
  const platformName = useMemo(() => {
    if (!trimmedUrl) return null;
    const platforms = platformsQuery.data ?? [];
    const detected = registryPlatformFromUrl(trimmedUrl, platforms);
    return platforms.find((entry) => entry.id === detected)?.name ?? null;
  }, [trimmedUrl, platformsQuery.data]);

  // A credential that already reaches this repository. The same read-only hint
  // the wiki wizard shows, through the same one client-side matcher and the
  // same `slugFromRepoUrl`/`hostOf` pair, so all three call sites ask the
  // question identically.
  //
  // The host comes from the URL, never from the slug: `parseSlug` folds a
  // nested group's middle segments into its `host` (`gitlab.com/group`), which
  // is the wiki's "everything before owner/repo" and not the DNS host a
  // host-scoped credential is filed under. `hostOf` mirrors the backend.
  const coveringCredential = useMemo(
    () =>
      slug ? matchCredential(credentials, { slug, host: hostOf(trimmedUrl) }) : null,
    [credentials, slug, trimmedUrl],
  );

  const handleSubmit = form.handleSubmit((values) => {
    createM.mutate(
      {
        repoUrl: values.repoUrl.trim(),
        ...(values.defaultBranch?.trim()
          ? { defaultBranch: values.defaultBranch.trim() }
          : {}),
        ...(values.name?.trim() ? { name: values.name.trim() } : {}),
      },
      {
        onSuccess: (repository) => {
          onRegistered(repository, addCredential && !coveringCredential);
          form.reset(EMPTY_FORM);
          setAddCredential(false);
          onOpenChange(false);
        },
        onError: (error) => {
          for (const code of FIELD_PINNED_CODES) {
            if (isRepositoryError(error, code)) {
              form.setError("repoUrl", {
                message:
                  error.reason ||
                  (code === "repository_exists"
                    ? "Mewbo already knows about this repository."
                    : "That does not look like a repository URL."),
              });
              return;
            }
          }
        },
      },
    );
  });

  // A pinned rejection already reads under the field; repeating it in the
  // banner would state the same problem twice.
  const bannerError =
    createM.error &&
    !FIELD_PINNED_CODES.some((code) => isRepositoryError(createM.error, code))
      ? createM.error
      : null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) form.reset(EMPTY_FORM);
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Add repository</DialogTitle>
          <DialogDescription>
            Registering a repository records it and nothing else. Nothing is cloned, nothing is
            indexed, and no model runs. Once it is here you can generate a wiki for it or point
            agentic tasks at it, one product at a time.
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={handleSubmit} className="space-y-4">
            <FormField
              control={form.control}
              name="repoUrl"
              render={({ field }) => (
                <FormItem className="space-y-1.5">
                  <FormLabel>Repository URL</FormLabel>
                  <FormControl>
                    <Input
                      {...field}
                      type="text"
                      placeholder="https://github.com/owner/repo"
                      disabled={createM.isPending}
                      autoComplete="off"
                      spellCheck={false}
                      className="font-mono"
                    />
                  </FormControl>
                  <IdentityPreview
                    hasInput={Boolean(trimmedUrl)}
                    slug={slug}
                    platformName={platformName}
                  />
                  <FormMessage />
                </FormItem>
              )}
            />

            <div className="grid gap-4 sm:grid-cols-2">
              <FormField
                control={form.control}
                name="defaultBranch"
                render={({ field }) => (
                  <FormItem className="space-y-1.5">
                    <FormLabel>Default branch (optional)</FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        type="text"
                        placeholder="main"
                        disabled={createM.isPending}
                        autoComplete="off"
                        spellCheck={false}
                        className="font-mono"
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="name"
                render={({ field }) => (
                  <FormItem className="space-y-1.5">
                    <FormLabel>Display name (optional)</FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        type="text"
                        placeholder={repoName || "Repository name"}
                        disabled={createM.isPending}
                        autoComplete="off"
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <CredentialSection
              coveringScope={coveringCredential?.scope ?? null}
              enabled={Boolean(slug) && !createM.isPending}
              addCredential={addCredential}
              onAddCredentialChange={setAddCredential}
            />

            {bannerError && (
              <ErrorAlert error={bannerError} fallback="Failed to register the repository" />
            )}

            <DialogFooter>
              <Button
                type="button"
                variant="ghost"
                size="md"
                onClick={() => onOpenChange(false)}
                disabled={createM.isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                variant="primary"
                size="md"
                disabled={createM.isPending}
                leadingIcon={
                  createM.isPending ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <Plus className="w-4 h-4" />
                  )
                }
              >
                {createM.isPending ? "Registering…" : "Register repository"}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * The canonical `host/owner/repo` Mewbo will file this under, plus the platform
 * it detected. Both are previews of a server decision, so the line stays quiet
 * about anything it cannot derive rather than guessing.
 */
function IdentityPreview({
  hasInput,
  slug,
  platformName,
}: {
  hasInput: boolean;
  slug: string | null;
  platformName: string | null;
}) {
  if (!hasInput) {
    return (
      <p className="text-xs text-[hsl(var(--muted-foreground))]">
        Any git host works. Mewbo files the repository under its canonical{" "}
        <code className="font-mono text-[hsl(var(--foreground))]">host/owner/repo</code> identity.
      </p>
    );
  }

  if (!slug) {
    return (
      <p className="text-xs text-[hsl(var(--muted-foreground))]">
        Enter the full URL, including the host and the owner, to see the identity this will be
        filed under.
      </p>
    );
  }

  return (
    <p className="flex flex-wrap items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))]">
      <span>Will be registered as</span>
      <code className="font-mono text-[hsl(var(--foreground))]">{slug}</code>
      {platformName && <span>on {platformName}</span>}
    </p>
  );
}

/**
 * Credential coverage for the repository being registered: a read-only hint
 * when a stored credential already reaches it, and otherwise an opt-in that
 * hands off to the shared credential dialog once the record exists.
 */
function CredentialSection({
  coveringScope,
  enabled,
  addCredential,
  onAddCredentialChange,
}: {
  coveringScope: string | null;
  enabled: boolean;
  addCredential: boolean;
  onAddCredentialChange: (next: boolean) => void;
}) {
  if (coveringScope) {
    return (
      <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/40 px-3 py-2">
        <p className="text-xs text-[hsl(var(--muted-foreground))]">
          A stored credential scoped to{" "}
          <code className="font-mono text-[hsl(var(--foreground))]">{coveringScope}</code> already
          reaches this repository, so nothing else is needed to clone it.
        </p>
      </div>
    );
  }

  return (
    <div className="flex items-start justify-between gap-4 rounded-md border border-[hsl(var(--border))] px-3 py-2.5">
      <div className="min-w-0">
        <p className="text-sm text-[hsl(var(--foreground))]">Store a credential for it</p>
        <p className="mt-0.5 text-xs text-[hsl(var(--muted-foreground))]">
          No stored credential covers this repository yet. Turn this on to open the credential form
          once the repository is registered. Public repositories do not need one.
        </p>
      </div>
      <Switch
        checked={addCredential}
        onCheckedChange={onAddCredentialChange}
        disabled={!enabled}
        aria-label="Store a credential for this repository"
      />
    </div>
  );
}
