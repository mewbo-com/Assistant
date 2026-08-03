import { useEffect, useMemo, useState } from 'react';
import {
  AppWindow,
  ArrowLeft,
  Blocks,
  BookOpen,
  ChevronDown,
  ChevronUp,
  Circle,
  FolderOpen,
  GitBranch,
  GitFork,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  RotateCcw,
  Sliders,
  Trash2,
  Zap,
} from 'lucide-react';
import { ProjectSummary, SkillSummary, ToolScope } from '../api/client';
import { CreateWorktreeInput, SessionTarget, WorktreeSummary } from '../types';
import { useApps } from '../hooks/useApps';
import { useWikiProjects } from './wiki/api/hooks';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';
import {
  MEWBO_BRANCH_PREFIX,
  defaultMewboBranchName,
} from '../utils/branchName';
import {
  useCheckoutRepository,
  useRepositories,
} from '../hooks/useRepositories';
import {
  AUTO_PROJECT,
  AUTO_PROJECT_DESCRIPTION,
  AUTO_PROJECT_LABEL,
  MANAGED_PREFIX,
  ProjectLabel,
} from '../utils/projectLabel';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from './ui/command';

export type McpStatus = 'active' | 'disabled' | 'error';

export type McpOption = {
  id: string;
  name: string;
  active: boolean;
  enabled: boolean;
  count?: number;
  status: McpStatus;
  scope?: ToolScope;
};

type ConfigMenuProps = {
  // Data
  mcpOptions: McpOption[];
  skills: SkillSummary[];
  projects: ProjectSummary[];
  // Active state
  activeProject: string | null;
  activeSkill: string | null;
  // Loading/error per category
  mcpLoading: boolean;
  mcpError: string | null;
  skillsLoading: boolean;
  skillsError: string | null;
  projectsLoading: boolean;
  projectsError: string | null;
  // Refresh callbacks
  onRefreshMcp: () => void;
  onRefreshSkills: () => void;
  onRefreshProjects: () => void;
  // Selection callbacks
  onToggleMcp: (id: string) => void;
  onSelectProject: (name: string | null) => void;
  onSelectSkill: (name: string | null) => void;
  onResetAll: () => void;
  /**
   * Offer the "Wiki & Apps" tab inside the Project drill-in. HOME mode only,
   * and the caller decides — a target routes session CREATION to a product's
   * own get-or-create endpoint, so there is nothing for it to do once a session
   * exists, and the server refuses to retarget a bound one. Shipping the
   * control in detail mode would offer a pick that can only fail.
   */
  targetsEnabled?: boolean;
  /** The chosen target, held BESIDE `activeProject` and never inside it. */
  activeTarget?: SessionTarget | null;
  onSelectTarget?: (target: SessionTarget | null) => void;
  /**
   * True when the durable session spec refuses a project override (a
   * purpose-bound session whose `editable.project` is not `true` — mirrors
   * the composer's locked-tool-ceiling gate, `InputBar`'s `toolBindingLocked`).
   * The Project panel stays browsable, but a pick routes through
   * `onRebindProject` instead of the local-only `onSelectProject`, since the
   * server would otherwise silently drop it. The "Temporary directory" row
   * hides in this state — the rebind route only ever BINDS, so there is no
   * unbind pick to offer.
   *
   * The **Auto** row deliberately does NOT hide with it. What disqualifies
   * "Temporary directory" is a wire shape, not a policy: its pick is `null`,
   * and the rebind route has no representation for that. `auto` is a real,
   * non-empty project key — a sibling of the `managed:<uuid>` grammar that the
   * same `PUT .../project` accepts — so that reason simply does not apply.
   * Hiding it anyway would make the lock asymmetric in the worst direction: a
   * session created in auto mode whose agent has since switched into a concrete
   * project would find every project pickable EXCEPT the one that restores the
   * behaviour it was created for. Whether a given bound session may go back to
   * auto is the SERVER's call — it validates the rebind and refuses what it
   * won't take, and `projectRebindError` surfaces that inline. Offering a pick
   * the server may refuse beats hiding one it would have accepted.
   */
  projectLocked?: boolean;
  /** A rebind PUT is in flight — guards against firing a second one. */
  projectRebinding?: boolean;
  /** The last rebind attempt's error, if any. */
  projectRebindError?: string | null;
  /** Durable rebind for a locked session's project (`PUT .../project`). Only
   *  meaningful, and only ever called, while `projectLocked` is true; the
   *  server requires a non-empty name (`null` has no route to reach it once
   *  the "Temporary directory" row is hidden while locked). */
  onRebindProject?: (name: string) => void;
  // Branch / worktree (optional — if ``gitRepo`` is false the git rows hide)
  gitRepo?: boolean;
  branches?: string[];
  currentBranch?: string | null;
  /**
   * Branches already checked out by the parent repo or another worktree —
   * ``git worktree add`` will refuse them. Surfaced so the "reuse existing
   * branch" path can disable them up-front instead of failing on POST.
   */
  branchesInUse?: string[];
  worktrees?: WorktreeSummary[];
  activeBranch?: string | null;
  activeWorktree?: string | null;
  gitLoading?: boolean;
  gitMutating?: boolean;
  gitError?: string | null;
  /** Read-only mode for in-session composer — picks are surfaced for
   * context but disabled. Worktrees can never be deleted from this surface. */
  gitReadOnly?: boolean;
  onSelectBranch?: (branch: string | null) => void;
  onSelectWorktree?: (worktreeId: string | null) => void;
  /**
   * Create a worktree. The structured payload makes the two modes
   * explicit: ``{branch}`` reuses an existing branch; ``{branch, base}``
   * creates a fresh branch from <base> via ``git worktree add -b``.
   */
  onCreateWorktree?: (input: CreateWorktreeInput) => void;
  onDeleteWorktree?: (worktreeId: string) => void;
  onRefreshGit?: () => void;
  // Popover control
  open: boolean;
  onToggleOpen: () => void;
  direction?: 'up' | 'down';
  compact?: boolean;
  /** When true, the trigger button is non-interactive (used while a session is
   * running — selections are locked while the agent steers the existing context). */
  disabled?: boolean;
};

/** Drill-in views. `root` is the summary list; the rest are focused panels. */
type View = 'root' | 'project' | 'branch' | 'worktree' | 'skills' | 'mcps';

const DOT_CLASSES: Record<McpStatus, string> = {
  active: 'fill-[hsl(var(--success))] text-[hsl(var(--success))]',
  disabled: 'fill-[hsl(var(--warning))] text-[hsl(var(--warning))]',
  error: 'fill-[hsl(var(--destructive))] text-[hsl(var(--destructive-text))]',
};

/** Selection key for a project entry — managed projects use a `managed:` prefix. */
function projectKey(p: ProjectSummary): string {
  return p.source === 'managed' && p.project_id
    ? `${MANAGED_PREFIX}${p.project_id}`
    : p.name;
}

// Visual overrides to restore the dense pre-migration look on top of shadcn's
// roomier desktop-command-palette defaults. Behavior (search, keyboard nav,
// selection state) still comes from cmdk via shadcn — only typography/spacing
// is being re-themed here. Exported so the sibling ModelSelector popover reads
// as one family (DRY — one dense command vocabulary for both composer menus).
//
// Two type steps only, and they mean different things. `text-xs` is the prose
// register: a row's label AND its secondary line are the same size, separated
// by colour, because a one-step size drop is too weak to carry hierarchy and
// only costs legibility. `text-2xs` is the micro register — uppercase group
// headings, inline badge pills, count pips — where the shape of the element
// already says "this is chrome" and the smaller size is density, not rank.
// The filter box deliberately sets NO font size. `ui/command.tsx` already gives
// its `<input>` the responsive iOS floor (`text-field md:text-sm` — 16px on
// narrow viewports, 13px from `md:` up), and `cn()` is last-wins, so ANY size
// passed here would replace that base step and drop the floor on phones. The
// primitive owns the type; a caller passes layout — restating a size here
// would do so silently, while reading as a harmless density tweak. That is
// the whole argument for not restating type at a call site.
//
// The height still has to follow the same breakpoint, or the 16px text is
// cramped: 32px clears a 16px line box, 28px keeps the dense desktop look.
// `py-1` is load-bearing against the vendored `py-3`, which would eat a fixed
// height from the inside.
export const COMMAND_INPUT_CLS = 'h-8 md:h-7 py-1';
export const COMMAND_EMPTY_CLS = 'py-3 text-xs text-[hsl(var(--muted-foreground))]';
export const COMMAND_GROUP_CLS =
  'p-0 [&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-2xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-[hsl(var(--muted-foreground))]';
// Two-line item (e.g., project name + description). Override shadcn's
// `gap-2 items-center` (single-line) and `text-sm px-2 py-1.5` (roomy) with
// `flex-col items-start gap-0 text-xs px-3 py-2` (dense vertical stack).
export const COMMAND_ITEM_TWO_LINE_CLS =
  'flex-col items-start gap-0 px-3 py-2 text-xs rounded-none';
// Single-line item (e.g., "Default", "Temporary directory", "None").
export const COMMAND_ITEM_SINGLE_LINE_CLS = 'px-3 py-2 text-xs rounded-none';

function StatusDot({ status, active }: { status: McpStatus; active: boolean }) {
  if (status === 'active' && !active) {
    return <Circle className="w-2 h-2 shrink-0 fill-[hsl(var(--muted))] text-[hsl(var(--border))]" />;
  }
  return <Circle className={`w-2 h-2 shrink-0 ${DOT_CLASSES[status]}`} />;
}

export function RefreshIcon({ onRefresh, label }: { onRefresh?: () => void; label: string }) {
  if (!onRefresh) return null;
  return (
    <button
      onClick={onRefresh}
      aria-label={label}
      className="p-0.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
      type="button"
    >
      <RefreshCw className="w-3 h-3" />
    </button>
  );
}

export function ErrorBanner({ error }: { error?: string | null }) {
  if (!error) return null;
  return (
    <div className="px-3 py-2 text-xs text-[hsl(var(--destructive-text))] border-b border-[hsl(var(--border))] bg-[hsl(var(--destructive)/0.05)]">
      {error}
    </div>
  );
}

/** Back-arrow header shared by every drill-in panel. */
function SubHeader({ title, onBack }: { title: string; onBack: () => void }) {
  return (
    <div className="flex items-center gap-1.5 px-2 py-1.5 border-b border-[hsl(var(--border))] shrink-0">
      <button
        type="button"
        onClick={onBack}
        aria-label="Back"
        className="p-1 rounded hover:bg-[hsl(var(--accent))] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
      >
        <ArrowLeft className="w-3.5 h-3.5" />
      </button>
      <span className="text-xs font-medium text-[hsl(var(--foreground))]">{title}</span>
    </div>
  );
}

export function ConfigMenu({
  mcpOptions,
  skills,
  projects,
  activeProject,
  activeSkill,
  mcpLoading,
  mcpError,
  skillsLoading,
  skillsError,
  projectsLoading,
  projectsError,
  onRefreshMcp,
  onRefreshSkills,
  onRefreshProjects,
  onToggleMcp,
  onSelectProject,
  onSelectSkill,
  onResetAll,
  targetsEnabled = false,
  activeTarget = null,
  onSelectTarget,
  projectLocked = false,
  projectRebinding = false,
  projectRebindError = null,
  onRebindProject,
  gitRepo = false,
  branches = [],
  currentBranch = null,
  branchesInUse = [],
  worktrees = [],
  activeBranch = null,
  activeWorktree = null,
  gitLoading = false,
  gitMutating = false,
  gitError = null,
  gitReadOnly = false,
  onSelectBranch,
  onSelectWorktree,
  onCreateWorktree,
  onDeleteWorktree,
  onRefreshGit,
  open,
  onToggleOpen,
  direction = 'up',
  compact = false,
  disabled = false,
}: ConfigMenuProps) {
  const [view, setView] = useState<View>('root');
  const showGit = gitRepo;

  // Reset to root when the menu closes.
  useEffect(() => {
    if (!open) setView('root');
  }, [open]);

  // Don't leave the user stranded on a panel whose surface just disappeared
  // (e.g. they switched from a git project to "Temporary directory").
  useEffect(() => {
    if (!showGit && (view === 'branch' || view === 'worktree')) {
      setView('root');
    }
  }, [showGit, view]);

  const activeMcpCount = mcpOptions.filter((m) => m.active).length;
  const branchIsNonDefault =
    showGit &&
    activeBranch !== null &&
    currentBranch !== null &&
    activeBranch !== currentBranch;
  // A target counts here for the same reason a project does: it is a deliberate
  // selection the composer is carrying, and the count is what tells a user
  // "something is set" without opening the menu. It is NOT added on top of the
  // project — the two are alternatives at submit time, and double-counting them
  // would report two scopes where the session gets one.
  const showTarget = targetsEnabled && activeTarget !== null;
  const totalActive =
    (showTarget || activeProject ? 1 : 0) +
    (activeSkill ? 1 : 0) +
    activeMcpCount +
    (branchIsNonDefault ? 1 : 0) +
    (activeWorktree ? 1 : 0);
  const hasAnyActive = totalActive > 0;

  // Naming a session's project is a LOOKUP, not a string copy — the console
  // persists it as `managed:<uuid>`. `ProjectLabel` is the one resolver for
  // that; hand-rolling a `projects.find` beside it instead falls through to
  // printing the raw key whenever the list doesn't hold the id (cold cache, or
  // a project the worktree reaper has since deleted) and names a WORKTREE by
  // its own project name — which IS its branch, so the Project row would read
  // identically to the Worktree row below it and the parent repo would appear
  // nowhere. Built once per `projects` identity, the same way `HomeView`
  // builds the session list's.
  const projectResolver = useMemo(() => new ProjectLabel(projects), [projects]);
  const projectLabel = useMemo(() => {
    if (!activeProject) return 'Temporary directory';
    // A `managed:<uuid>` only becomes a name once the list lands, and
    // `useProjects()` sets no `staleTime`, so first paint is a cache miss.
    // Say nothing for that beat rather than publish a key the user will read
    // as the project's actual name. A plain configured name needs no lookup,
    // so it is never held back.
    if (projectsLoading && activeProject.startsWith(MANAGED_PREFIX)) return 'Loading...';
    return (
      projectResolver.resolve({
        project: activeProject,
        branch: activeBranch ?? undefined,
      }).label ?? 'Temporary directory'
    );
  }, [activeProject, activeBranch, projectsLoading, projectResolver]);
  // What the Project drill-in currently RESOLVES to. A target wins over the
  // project label because a targeted submit never spends the project scope at
  // all — the session comes from the product's endpoint already bound. Showing
  // the project there would name a directory the run will not use.
  const drillInLabel = showTarget ? (activeTarget as SessionTarget).label : projectLabel;
  const skillLabel = activeSkill ? `/${activeSkill}` : 'None';
  const mcpLabel = activeMcpCount > 0 ? `${activeMcpCount} active` : 'None';
  const branchLabel = (() => {
    if (!showGit) return 'Not a git repo';
    if (activeBranch) return activeBranch;
    if (currentBranch) return currentBranch;
    return 'Detached HEAD';
  })();
  // Match by project_id only when it is non-null on both sides — user-
  // created worktrees expose ``project_id: null`` and would otherwise
  // collide with an unset ``activeWorktree`` and look "selected".
  const activeWorktreeEntry =
    activeWorktree !== null
      ? worktrees.find((w) => w.project_id === activeWorktree)
      : undefined;
  const worktreeLabel = (() => {
    if (!showGit) return '—';
    if (activeWorktreeEntry) return activeWorktreeEntry.branch;
    return 'Parent repo';
  })();
  // Registered repositories, fetched here rather than threaded through
  // ``InputBar``. The SELECTION contract is what must not grow, and it does not:
  // a repository resolves to a managed project and is chosen through the very
  // same ``onSelectProject(managed:<id>)`` call a managed project uses. The list
  // behind it is this panel's own concern, and TanStack shares one cache by
  // query key, so reading it here is not a second fetch.
  //
  // Gated on ``open`` so a composer that is never opened costs no request.
  const { repositories } = useRepositories(open);
  const checkout = useCheckoutRepository();
  // Which repository is mid-clone. Held locally rather than read off the
  // mutation, because ``isPending`` says only that A checkout is running: with
  // one shared mutation it would spin every uncheckedout row at once.
  const [checkingOut, setCheckingOut] = useState<string | null>(null);
  // A checkout the user ASKED for and that failed is theirs to hear about, so it
  // banners. A failure to LIST repositories is not: the group is an enrichment
  // of a picker that must keep working, so it degrades to absent (the hook has
  // already logged it) exactly as the server's own usage projection does.
  const [checkoutError, setCheckoutError] = useState<string | null>(null);

  /**
   * Check *slug* out, then select the project it produced.
   *
   * Never fires from rendering or from opening this menu — only from the
   * labelled row the user clicks. The selection is driven off the RESPONSE
   * rather than a refetch, so the project id is the server's, never a guess.
   */
  const setUpRepository = async (slug: string) => {
    if (checkingOut) return;
    setCheckingOut(slug);
    setCheckoutError(null);
    try {
      const record = await checkout.mutateAsync(slug);
      const projectId = record.usage.tasks?.projectId;
      if (!projectId) {
        // A 2xx whose projection cannot see the checkout it just made. Refusing
        // to select beats anchoring a session to a project id we invented.
        setCheckoutError(
          'The checkout finished but Mewbo could not link it to a project. Open Settings, Repositories to check on it.',
        );
        return;
      }
      handlePickProject(`${MANAGED_PREFIX}${projectId}`);
    } catch (err) {
      setCheckoutError(err instanceof Error ? err.message : 'The checkout failed.');
    } finally {
      setCheckingOut(null);
    }
  };

  // Project groupings
  const configProjects = projects.filter((p) => p.source !== 'managed');
  // A repository's checkout IS a managed project, so it would otherwise list
  // twice. It belongs under Repositories, where it is named by the repository
  // it holds rather than by the folder the project store minted for it.
  const repositoryProjectIds = new Set(
    repositories.map((r) => r.usage.tasks?.projectId).filter(Boolean),
  );
  const managedProjects = projects.filter(
    (p) => p.source === 'managed' && !repositoryProjectIds.has(p.project_id ?? ''),
  );

  // MCP scope groups (worst-status-wins indicator already computed by caller)
  const pluginOptions = mcpOptions.filter((o) => o.scope === 'plugin');
  const globalOptions = mcpOptions.filter((o) => o.scope !== 'project' && o.scope !== 'plugin');
  const projectOptions = mcpOptions.filter((o) => o.scope === 'project');
  const mcpSections: { label: string; items: McpOption[] }[] = [
    { label: 'Global', items: globalOptions },
    { label: 'Plugin', items: pluginOptions },
    { label: 'Project', items: projectOptions },
  ].filter((s) => s.items.length > 0);

  const back = () => setView('root');

  /**
   * The one place a project pick lands. While the session is locked the
   * server refuses the local-only path (`SessionSpec.field_editable`), so a
   * pick has to travel through the durable rebind instead — and, unlike an
   * ordinary selection, it stays on the panel (no `back()`) so a pending
   * rebind or its error is visible rather than hidden behind a closed popover.
   * `name === null` (the "Temporary directory" pick) is unreachable here while
   * locked — that row is hidden below, since the rebind route only ever BINDS.
   * `AUTO_PROJECT` is NOT null and IS reachable while locked, on purpose: it is
   * a bind like any other. See the `projectLocked` prop doc for why.
   */
  const handlePickProject = (name: string | null) => {
    if (projectLocked) {
      if (projectRebinding || name === null) return;
      onRebindProject?.(name);
      return;
    }
    onSelectProject(name);
    back();
  };

  /**
   * The Projects tab's body — the picker that scopes WHERE a session runs.
   *
   * A function rather than a hoisted element so the JSX is built only for the
   * branch that renders it, and so the two mount sites (tabbed in home mode,
   * bare in detail mode) share one implementation instead of a copy that drifts.
   */
  const renderProjectPicker = () => (
            <Command className="flex-1 min-h-0">
              <div className="flex items-center justify-between pr-2">
                <CommandInput placeholder="Filter projects..." className={COMMAND_INPUT_CLS} />
                <RefreshIcon onRefresh={onRefreshProjects} label="Refresh projects" />
              </div>
              {projectLocked && (
                <div className="flex items-center gap-1.5 px-3 py-2 text-xs text-[hsl(var(--muted-foreground))] border-b border-[hsl(var(--border))]">
                  {projectRebinding ? (
                    <>
                      <Loader2 className="w-3 h-3 shrink-0 animate-spin" />
                      <span>Rebinding the session's project…</span>
                    </>
                  ) : (
                    <>
                      <Lock className="w-3 h-3 shrink-0 text-[hsl(var(--primary-text))]" />
                      <span>
                        This session is bound to a project. Picking one below rebinds it.
                      </span>
                    </>
                  )}
                </div>
              )}
              <ErrorBanner error={projectsError} />
              <ErrorBanner error={checkoutError} />
              <ErrorBanner error={projectLocked ? projectRebindError : null} />
              <CommandList className="max-h-[300px]">
                <CommandEmpty className={COMMAND_EMPTY_CLS}>
                  {projectsLoading ? 'Loading...' : 'No matches.'}
                </CommandEmpty>
                {/* The rebind route only ever BINDS — there is no unbind verb, since
                    clearing a project would null the session's cwd and send the
                    next turn into an empty temp dir. So this pick hides entirely
                    while locked, rather than offering a choice the server refuses. */}
                {!projectLocked && (
                  <CommandItem
                    value="__temp__ Temporary directory"
                    onSelect={() => handlePickProject(null)}
                    className={`${COMMAND_ITEM_SINGLE_LINE_CLS} ${!activeProject ? 'font-medium' : ''}`}
                  >
                    Temporary directory
                  </CommandItem>
                )}
                {/* Auto-select. Two lines, like every project row, because the
                    second one carries the whole point: the AGENT chooses, and
                    Mewbo is not guessing on the user's behalf. Rendered
                    unconditionally — see the `projectLocked` prop doc for why it
                    survives a lock that hides "Temporary directory". */}
                <CommandItem
                  value={`__auto__ ${AUTO_PROJECT_LABEL} agent picks the project itself`}
                  onSelect={() => handlePickProject(AUTO_PROJECT)}
                  className={`${COMMAND_ITEM_TWO_LINE_CLS} ${
                    ProjectLabel.isAuto(activeProject) ? 'font-medium' : ''
                  }`}
                >
                  <span>{AUTO_PROJECT_LABEL}</span>
                  {/* Authored copy, not user data: it wraps rather than
                      truncating, which is what makes it readable without a
                      tooltip. A project's own description keeps `truncate`
                      because its length is unbounded. */}
                  <span className="mt-0.5 w-full whitespace-normal text-xs leading-snug text-[hsl(var(--muted-foreground))]">
                    {AUTO_PROJECT_DESCRIPTION}
                  </span>
                </CommandItem>
                {configProjects.length > 0 && (
                  <CommandGroup heading="Configured" className={COMMAND_GROUP_CLS}>
                    {configProjects.map((project) => {
                      const key = projectKey(project);
                      return (
                        <CommandItem
                          key={key}
                          value={`${project.name} ${project.description ?? ''}`}
                          onSelect={() => handlePickProject(key)}
                          className={`${COMMAND_ITEM_TWO_LINE_CLS} ${activeProject === key ? 'font-medium' : ''}`}
                        >
                          <span>{project.name}</span>
                          {project.description && (
                            <span className="text-xs text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                              {project.description}
                            </span>
                          )}
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                )}
                {/*
                  Registered repositories. A row is EITHER a normal selection
                  (the repository already has a checkout) or the labelled
                  set-up control (it does not) — the row itself is the control
                  rather than a nested button, because a `<button>` inside a
                  `role="option"` has no keyboard path in and cmdk would fire
                  both handlers on one click. The label says which it is before
                  it is clicked, so the clone is never a surprise.
                */}
                {repositories.length > 0 && (
                  <CommandGroup heading="Repositories" className={COMMAND_GROUP_CLS}>
                    {repositories.map((repository) => {
                      const projectId = repository.usage.tasks?.projectId;
                      const key = projectId ? `${MANAGED_PREFIX}${projectId}` : null;
                      const pending = checkingOut === repository.slug;
                      const busy = checkingOut !== null;
                      return (
                        <CommandItem
                          key={repository.slug}
                          value={`${repository.name ?? repository.repo} ${repository.slug}`}
                          onSelect={() => {
                            if (key) {
                              handlePickProject(key);
                              return;
                            }
                            void setUpRepository(repository.slug);
                          }}
                          className={`${COMMAND_ITEM_TWO_LINE_CLS} ${
                            key && activeProject === key ? 'font-medium' : ''
                          } ${busy && !pending ? 'opacity-50' : ''}`}
                        >
                          <span className="flex w-full items-center justify-between gap-2">
                            <span className="truncate">
                              {repository.name ?? repository.repo}
                            </span>
                            {!key && (
                              <span className="flex shrink-0 items-center gap-1 text-2xs text-[hsl(var(--muted-foreground))]">
                                {pending && <Loader2 className="w-3 h-3 animate-spin" />}
                                {pending ? 'Setting up' : 'Set up'}
                              </span>
                            )}
                          </span>
                          <span className="text-xs text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                            {key
                              ? repository.slug
                              : `${repository.slug}. No checkout yet.`}
                          </span>
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                )}
                {managedProjects.length > 0 && (
                  <CommandGroup heading="Managed" className={COMMAND_GROUP_CLS}>
                    {managedProjects.map((project) => {
                      const key = projectKey(project);
                      return (
                        <CommandItem
                          key={key}
                          value={`${project.name} ${project.description ?? ''}`}
                          onSelect={() => handlePickProject(key)}
                          className={`${COMMAND_ITEM_TWO_LINE_CLS} ${activeProject === key ? 'font-medium' : ''}`}
                        >
                          <span>{project.name}</span>
                          {project.description && (
                            <span className="text-xs text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                              {project.description}
                            </span>
                          )}
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                )}
              </CommandList>
            </Command>
  );

  return (
    <Popover
      open={disabled ? false : open}
      onOpenChange={(next) => {
        if (disabled) return;
        if (next !== open) onToggleOpen();
      }}
    >
      <PopoverTrigger asChild>
        <button
          aria-label={disabled ? 'Session config (locked while running)' : 'Configure session'}
          disabled={disabled}
          title={disabled ? 'Locked while the agent is running' : undefined}
          className={`flex items-center gap-1.5 ${compact ? 'h-7 px-1.5' : 'h-7 px-2.5'} rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-normal text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${open ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''} ${hasAnyActive ? 'text-[hsl(var(--foreground))]' : ''} disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-[hsl(var(--muted-foreground))]`}
        >
          <Sliders className={`w-3.5 h-3.5 ${hasAnyActive ? '' : 'opacity-50'}`} />
          {compact ? (
            <span className="text-2xs truncate max-w-[80px]">{drillInLabel}</span>
          ) : (
            <>
              <span className="truncate max-w-[160px]">{drillInLabel}</span>
              {hasAnyActive && (
                <span className="shrink-0 min-w-[16px] h-4 px-1 inline-flex items-center justify-center rounded-full bg-[hsl(var(--primary))]/15 text-2xs font-medium text-[hsl(var(--primary-text))]">
                  {totalActive}
                </span>
              )}
              {open ? (
                <ChevronUp className="w-3 h-3 opacity-50" />
              ) : (
                <ChevronDown className="w-3 h-3 opacity-50" />
              )}
            </>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent
        side={direction === 'up' ? 'top' : 'bottom'}
        align="start"
        className="w-80 max-h-[440px] p-0 flex flex-col overflow-hidden"
      >
        {/* Root: summary rows that drill into focused panels. */}
        {view === 'root' && (
          <div className="flex-1 min-h-0 overflow-y-auto">
            {/* Sanctioned identity accents, not decoration: the five root rows
                render side by side and each needs its own recognizable color
                so an "active" glance doesn't require reading the label —
                same rationale as AGENT_COLOR_CLASSES. Blue=Project,
                sky=Branch, violet=Worktree, amber=Skills, emerald=Integrations. */}
            <div className="py-1">
              {/* One row, two states. With a target chosen the row reports the
                  TARGET — its own product mark, its own word — because the
                  drill-in below it resolves to that and the project scope is
                  not what the run will use. Naming it "Project" while it holds
                  a wiki project would teach exactly the conflation the tabs
                  exist to prevent. */}
              <CategoryRow
                icon={
                  showTarget ? (
                    <TargetMark
                      kind={(activeTarget as SessionTarget).kind}
                      className="w-3.5 h-3.5 text-[hsl(var(--primary-text))]"
                    />
                  ) : projectLocked ? (
                    <Lock className="w-3.5 h-3.5 text-[hsl(var(--primary-text))]" />
                  ) : (
                    <FolderOpen className={`w-3.5 h-3.5 ${activeProject ? 'text-blue-500' : 'opacity-50'}`} />
                  )
                }
                label={showTarget ? 'Target' : 'Project'}
                value={drillInLabel}
                emphasized={showTarget || !!activeProject}
                onClick={() => setView('project')}
              />
              {showGit && (
                <CategoryRow
                  icon={
                    <GitBranch
                      className={`w-3.5 h-3.5 ${branchIsNonDefault ? 'text-sky-500' : 'opacity-50'}`}
                    />
                  }
                  label="Branch"
                  value={branchLabel}
                  emphasized={branchIsNonDefault}
                  onClick={() => setView('branch')}
                />
              )}
              {showGit && (
                <CategoryRow
                  icon={
                    <GitFork
                      className={`w-3.5 h-3.5 ${activeWorktree ? 'text-violet-500' : 'opacity-50'}`}
                    />
                  }
                  label="Worktree"
                  value={worktreeLabel}
                  emphasized={!!activeWorktree}
                  onClick={() => setView('worktree')}
                />
              )}
              <CategoryRow
                icon={<Zap className={`w-3.5 h-3.5 ${activeSkill ? 'text-amber-500' : 'opacity-50'}`} />}
                label="Skills"
                value={skillLabel}
                emphasized={!!activeSkill}
                onClick={() => setView('skills')}
              />
              <CategoryRow
                icon={<Blocks className={`w-3.5 h-3.5 ${activeMcpCount > 0 ? 'text-emerald-500' : 'opacity-50'}`} />}
                label="Integrations"
                value={mcpLabel}
                emphasized={activeMcpCount > 0}
                onClick={() => setView('mcps')}
              />
            </div>
            {hasAnyActive && (
              <>
                <div className="h-px bg-[hsl(var(--border))] mx-2 my-1" />
                <button
                  onClick={onResetAll}
                  type="button"
                  className="w-full text-left px-3 py-2 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] flex items-center gap-2 transition-colors"
                >
                  <RotateCcw className="w-3.5 h-3.5" />
                  <span>Reset to defaults</span>
                </button>
              </>
            )}
          </div>
        )}

        {/* Project panel. In home mode it carries a second tab, and the tab
            strip is the whole point of the design: a wiki project or an app is
            a TARGET, not a project source. A third `CommandGroup` beside
            Configured / Repositories / Managed would sit in a flat list of
            things that set `cwd` and read as a fourth one, which is precisely
            what it is not. */}
        {view === 'project' && (
          <div className="flex-1 min-h-0 flex flex-col">
            <SubHeader title="Project" onBack={back} />
            {targetsEnabled ? (
              <Tabs defaultValue="projects" className="flex-1 min-h-0 flex flex-col">
                <TabsList className="mx-3 mt-2 self-start">
                  <TabsTrigger value="projects">
                    <FolderOpen className="w-3 h-3" aria-hidden />
                    Projects
                  </TabsTrigger>
                  <TabsTrigger value="targets">
                    <BookOpen className="w-3 h-3" aria-hidden />
                    Wiki &amp; Apps
                  </TabsTrigger>
                </TabsList>
                <TabsContent value="projects" className="mt-1 flex-1 min-h-0 flex flex-col">
                  {renderProjectPicker()}
                </TabsContent>
                {/* Radix unmounts an inactive panel, so the two product reads
                    below cost nothing until this tab is opened — the same
                    "browsing is free" rule the Repositories group follows via
                    `useRepositories(open)`. */}
                <TabsContent value="targets" className="mt-1 flex-1 min-h-0 flex flex-col">
                  <TargetPicker
                    activeTarget={activeTarget}
                    onSelect={(target) => {
                      onSelectTarget?.(target);
                      back();
                    }}
                  />
                </TabsContent>
              </Tabs>
            ) : (
              renderProjectPicker()
            )}
          </div>
        )}

        {/* Branch panel — visible only when the active project is a git repo. */}
        {view === 'branch' && showGit && (
          <div className="flex-1 min-h-0 flex flex-col">
            <SubHeader title="Branch" onBack={back} />
            <Command className="flex-1 min-h-0">
              <div className="flex items-center justify-between pr-2">
                <CommandInput placeholder="Filter branches..." className={COMMAND_INPUT_CLS} />
                <RefreshIcon onRefresh={onRefreshGit} label="Refresh git state" />
              </div>
              <ErrorBanner error={gitError} />
              <CommandList className="max-h-[300px]">
                <CommandEmpty className={COMMAND_EMPTY_CLS}>
                  {gitLoading ? 'Loading...' : 'No matches.'}
                </CommandEmpty>
                {currentBranch && (
                  <CommandItem
                    value={`__current__ ${currentBranch}`}
                    onSelect={() => {
                      if (gitReadOnly) return;
                      onSelectBranch?.(currentBranch);
                      // Picking the current branch implies "no worktree"
                      // — back out of any active worktree selection so the
                      // session runs against the parent repo.
                      onSelectWorktree?.(null);
                      back();
                    }}
                    disabled={gitReadOnly}
                    className={`${COMMAND_ITEM_TWO_LINE_CLS} ${activeBranch === currentBranch || (!activeBranch && !branchIsNonDefault) ? 'font-medium' : ''}`}
                  >
                    <span className="flex items-center gap-2">
                      <GitBranch className="w-3 h-3" />
                      <span className="font-mono">{currentBranch}</span>
                      <span className="text-2xs text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1 py-0.5 rounded">
                        current
                      </span>
                    </span>
                  </CommandItem>
                )}
                <CommandGroup heading="Branches" className={COMMAND_GROUP_CLS}>
                  {branches
                    .filter((b) => b !== currentBranch)
                    .map((branch) => {
                      const matchedWorktree = worktrees.find((w) => w.branch === branch);
                      const isActive = activeBranch === branch;
                      return (
                        <CommandItem
                          key={branch}
                          value={branch}
                          onSelect={() => {
                            if (gitReadOnly) return;
                            onSelectBranch?.(branch);
                            // If the picked branch already has a managed
                            // worktree, lock onto it so the session uses
                            // that directory instead of the parent.
                            if (matchedWorktree && matchedWorktree.managed) {
                              onSelectWorktree?.(matchedWorktree.project_id);
                            } else {
                              onSelectWorktree?.(null);
                            }
                            back();
                          }}
                          disabled={gitReadOnly}
                          className={`${COMMAND_ITEM_TWO_LINE_CLS} ${isActive ? 'font-medium' : ''}`}
                        >
                          <span className="flex items-center gap-2 truncate">
                            <GitBranch className="w-3 h-3 shrink-0" />
                            <span className="font-mono truncate">{branch}</span>
                          </span>
                          {matchedWorktree && (
                            <span className="text-xs text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5 flex items-center gap-1">
                              <GitFork className="w-3 h-3" />
                              {matchedWorktree.managed ? 'Has managed worktree' : 'Has user worktree'}
                            </span>
                          )}
                        </CommandItem>
                      );
                    })}
                </CommandGroup>
              </CommandList>
            </Command>
          </div>
        )}

        {/* Worktree panel — visible only when the active project is a git repo. */}
        {view === 'worktree' && showGit && (
          <div className="flex-1 min-h-0 flex flex-col">
            <SubHeader title="Worktree" onBack={back} />
            <Command className="flex-1 min-h-0">
              <div className="flex items-center justify-between pr-2">
                <CommandInput placeholder="Filter worktrees..." className={COMMAND_INPUT_CLS} />
                <RefreshIcon onRefresh={onRefreshGit} label="Refresh worktrees" />
              </div>
              <ErrorBanner error={gitError} />
              <CommandList className="max-h-[300px]">
                <CommandEmpty className={COMMAND_EMPTY_CLS}>
                  {gitLoading ? 'Loading...' : 'No matches.'}
                </CommandEmpty>
                <CommandItem
                  value="__parent__ Parent repo"
                  onSelect={() => {
                    if (gitReadOnly) return;
                    onSelectWorktree?.(null);
                    // Snap branch back to parent's current to keep the two
                    // selectors in sync — picking "no worktree" is the
                    // same gesture as "use whatever's checked out".
                    onSelectBranch?.(currentBranch ?? null);
                    back();
                  }}
                  disabled={gitReadOnly}
                  className={`${COMMAND_ITEM_TWO_LINE_CLS} ${!activeWorktree ? 'font-medium' : ''}`}
                >
                  <span className="flex items-center gap-2">
                    <FolderOpen className="w-3 h-3" />
                    <span>Parent repo</span>
                  </span>
                  <span className="text-xs text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                    {currentBranch
                      ? `Run against ${currentBranch} in the parent working tree`
                      : 'Run against the parent working tree'}
                  </span>
                </CommandItem>
                {worktrees.length > 0 && (
                  <CommandGroup heading="Worktrees" className={COMMAND_GROUP_CLS}>
                    {worktrees.map((wt) => {
                      const id = wt.project_id ?? `unmanaged:${wt.branch}`;
                      const isActive =
                        activeWorktree !== null &&
                        wt.project_id !== null &&
                        activeWorktree === wt.project_id;
                      const selectable = !gitReadOnly && wt.managed;
                      return (
                        <CommandItem
                          key={id}
                          value={`${wt.branch} ${wt.path}`}
                          onSelect={() => {
                            if (!selectable || !wt.project_id) return;
                            onSelectBranch?.(wt.branch);
                            onSelectWorktree?.(wt.project_id);
                            back();
                          }}
                          disabled={!selectable}
                          className={`${COMMAND_ITEM_TWO_LINE_CLS} ${isActive ? 'font-medium' : ''}`}
                        >
                          <span className="flex items-center gap-2 truncate">
                            <GitFork className="w-3 h-3 shrink-0" />
                            <span className="font-mono truncate">{wt.branch}</span>
                            <span
                              className="text-2xs text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1 py-0.5 rounded shrink-0"
                              title={wt.managed ? 'Created by Mewbo' : 'Existing git worktree on disk'}
                            >
                              {wt.managed ? 'managed' : 'user'}
                            </span>
                            {wt.clean === false && (
                              <span
                                className="text-amber-400 text-2xs shrink-0"
                                title="Uncommitted changes or unpushed commits"
                              >
                                ●
                              </span>
                            )}
                            {!gitReadOnly && wt.managed && wt.project_id && onDeleteWorktree && (
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  onDeleteWorktree(wt.project_id as string);
                                }}
                                aria-label={`Delete worktree ${wt.branch}`}
                                className="ml-auto p-1 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                              >
                                <Trash2 className="w-3 h-3" />
                              </button>
                            )}
                          </span>
                          <span className="text-xs text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                            {wt.path}
                            {!wt.managed && ' (use git CLI to remove)'}
                          </span>
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                )}
                {!gitReadOnly && onCreateWorktree && branches.length > 0 && (
                  <CommandGroup heading="Create new worktree" className={COMMAND_GROUP_CLS}>
                    <NewWorktreeForm
                      branches={branches}
                      currentBranch={currentBranch}
                      branchesInUse={branchesInUse}
                      existingWorktreeBranches={worktrees.map((w) => w.branch)}
                      loading={gitMutating}
                      onSubmit={onCreateWorktree}
                    />
                  </CommandGroup>
                )}
              </CommandList>
            </Command>
          </div>
        )}

        {/* Skills panel */}
        {view === 'skills' && (
          <div className="flex-1 min-h-0 flex flex-col">
            <SubHeader title="Skills" onBack={back} />
            <Command className="flex-1 min-h-0">
              <div className="flex items-center justify-between pr-2">
                <CommandInput placeholder="Filter skills..." className={COMMAND_INPUT_CLS} />
                <RefreshIcon onRefresh={onRefreshSkills} label="Refresh skills" />
              </div>
              <ErrorBanner error={skillsError} />
              <CommandList className="max-h-[300px]">
                <CommandEmpty className={COMMAND_EMPTY_CLS}>
                  {skillsLoading ? 'Loading...' : 'No matches.'}
                </CommandEmpty>
                <CommandItem
                  value="__none__ None"
                  onSelect={() => { onSelectSkill(null); back(); }}
                  className={`${COMMAND_ITEM_SINGLE_LINE_CLS} ${!activeSkill ? 'font-medium' : ''}`}
                >
                  None
                </CommandItem>
                {skills.map((skill) => (
                  <CommandItem
                    key={skill.name}
                    value={`${skill.name} ${skill.description ?? ''}`}
                    onSelect={() => { onSelectSkill(skill.name); back(); }}
                    className={`${COMMAND_ITEM_TWO_LINE_CLS} ${activeSkill === skill.name ? 'font-medium' : ''}`}
                  >
                    <span className="flex items-center gap-1.5">
                      <span>/{skill.name}</span>
                      <span className="text-2xs text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1 py-0.5 rounded">
                        {skill.source}
                      </span>
                    </span>
                    <span className="text-xs text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                      {skill.description}
                    </span>
                  </CommandItem>
                ))}
              </CommandList>
            </Command>
          </div>
        )}

        {/* Integrations (MCPs) panel — multi-select, so do NOT auto-return to root on toggle. */}
        {view === 'mcps' && (
          <div className="flex-1 min-h-0 flex flex-col">
            <SubHeader title="Integrations" onBack={back} />
            <Command className="flex-1 min-h-0">
              <div className="flex items-center justify-between pr-2">
                <CommandInput placeholder="Filter integrations..." className={COMMAND_INPUT_CLS} />
                <RefreshIcon onRefresh={onRefreshMcp} label="Refresh MCP tools" />
              </div>
              <ErrorBanner error={mcpError} />
              <CommandList className="max-h-[300px]">
                <CommandEmpty className={COMMAND_EMPTY_CLS}>
                  {mcpLoading ? 'Loading...' : 'No matches.'}
                </CommandEmpty>
                {mcpSections.length === 0 && !mcpLoading && (
                  <div className="px-4 py-3 text-xs text-[hsl(var(--muted-foreground))] text-center">
                    No tools available.
                  </div>
                )}
                {mcpSections.map((section) => (
                  <CommandGroup key={section.label} heading={section.label} className={COMMAND_GROUP_CLS}>
                    {section.items.map((mcp) => (
                      <CommandItem
                        key={mcp.id}
                        value={mcp.name}
                        disabled={mcp.status === 'error'}
                        onSelect={() => onToggleMcp(mcp.id)}
                        // MCP rows are single-line (name + count badge + status dot on the right)
                        className={`${COMMAND_ITEM_SINGLE_LINE_CLS} justify-between ${mcp.enabled ? '' : 'text-[hsl(var(--muted-foreground))]'}`}
                      >
                        <span className="flex items-center gap-2 truncate flex-1">
                          <span className="truncate">{mcp.name}</span>
                          {mcp.scope && (
                            <span className="text-2xs text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1 py-0.5 rounded shrink-0">
                              {mcp.scope}
                            </span>
                          )}
                          {typeof mcp.count === 'number' && (
                            <span className="text-2xs text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded">
                              {mcp.count}
                            </span>
                          )}
                        </span>
                        <StatusDot status={mcp.status} active={mcp.active} />
                      </CommandItem>
                    ))}
                  </CommandGroup>
                ))}
              </CommandList>
            </Command>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}

/**
 * The product mark for a target kind. One mark per product, and it is the SAME
 * one the NavRail row uses (`nav-rail/products.ts`) — a wiki target and the Wiki
 * rail row must not teach two different glyphs. Spelled once here so the tab
 * strip, the rows and the root summary can never drift apart.
 */
function TargetMark({
  kind,
  className,
}: {
  kind: SessionTarget['kind'];
  className: string;
}) {
  const Icon = kind === 'wiki' ? BookOpen : AppWindow;
  return <Icon className={className} aria-hidden />;
}

/**
 * The "Wiki & Apps" tab — what a new session is ABOUT.
 *
 * Deliberately NOT a project source. Picking a row here does not add a key to
 * the session context; it routes creation to the product's own session
 * endpoint, requesting a genuinely NEW session (`{requestNew: true}` —
 * `App.handleCreateAndRun` is the caller that sets it), so the session arrives
 * already bound to that wiki project or app. That is why it is a tab rather
 * than a fourth `CommandGroup` in the picker next door: sitting in that flat
 * list it would read as another way to set `cwd`, which it is not.
 *
 * Mounted only while its tab is selected (Radix unmounts the inactive panel),
 * so neither read fires for a composer nobody opened.
 */
function TargetPicker({
  activeTarget,
  onSelect,
}: {
  activeTarget: SessionTarget | null;
  onSelect: (target: SessionTarget | null) => void;
}) {
  const wiki = useWikiProjects();
  const apps = useApps();
  const wikiProjects = wiki.data ?? [];
  // An archived app has no maintainer to open a session with, and the endpoint
  // would be a dead end. Filtered here rather than asked of the server, which
  // returns the gallery whole.
  const liveApps = (apps.data ?? []).filter((app) => app.status !== 'archived');
  const loading = wiki.isLoading || apps.isLoading;
  // A failure to LIST degrades to an absent group, exactly as the Repositories
  // group does: this tab is an enrichment of a composer that must keep working,
  // and the hooks have already logged it. Only a failure the user ASKED for
  // banners, and browsing is not one.
  return (
    <Command className="flex-1 min-h-0">
      <CommandInput placeholder="Filter wiki projects and apps..." className={COMMAND_INPUT_CLS} />
      <div className="px-3 py-2 text-xs leading-snug text-[hsl(var(--muted-foreground))] border-b border-[hsl(var(--border))]">
        Point this session at a wiki project or an app. It starts a new session
        for that product, so the project scope does not apply.
      </div>
      <CommandList className="max-h-[300px]">
        <CommandEmpty className={COMMAND_EMPTY_CLS}>
          {loading ? 'Loading...' : 'No matches.'}
        </CommandEmpty>
        <CommandItem
          value="__no_target__ None ordinary session"
          onSelect={() => onSelect(null)}
          className={`${COMMAND_ITEM_SINGLE_LINE_CLS} ${!activeTarget ? 'font-medium' : ''}`}
        >
          None
        </CommandItem>
        {wikiProjects.length > 0 && (
          <CommandGroup heading="Wiki" className={COMMAND_GROUP_CLS}>
            {wikiProjects.map((project) => {
              const selected =
                activeTarget?.kind === 'wiki' && activeTarget.slug === project.slug;
              return (
                <CommandItem
                  key={project.slug}
                  value={`${project.slug} ${project.desc ?? ''}`}
                  onSelect={() =>
                    onSelect({ kind: 'wiki', slug: project.slug, label: project.slug })
                  }
                  className={`${COMMAND_ITEM_TWO_LINE_CLS} ${selected ? 'font-medium' : ''}`}
                >
                  <span className="flex w-full items-center gap-2 truncate">
                    <TargetMark kind="wiki" className="w-3 h-3 shrink-0" />
                    <span className="truncate">{project.slug}</span>
                  </span>
                  {project.desc && (
                    <span className="text-xs text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                      {project.desc}
                    </span>
                  )}
                </CommandItem>
              );
            })}
          </CommandGroup>
        )}
        {liveApps.length > 0 && (
          <CommandGroup heading="Apps" className={COMMAND_GROUP_CLS}>
            {liveApps.map((app) => {
              const selected =
                activeTarget?.kind === 'app' && activeTarget.appId === app.app_id;
              return (
                <CommandItem
                  key={app.app_id}
                  value={`${app.title} ${app.summary ?? ''}`}
                  onSelect={() =>
                    onSelect({ kind: 'app', appId: app.app_id, label: app.title })
                  }
                  className={`${COMMAND_ITEM_TWO_LINE_CLS} ${selected ? 'font-medium' : ''}`}
                >
                  <span className="flex w-full items-center gap-2 truncate">
                    <TargetMark kind="app" className="w-3 h-3 shrink-0" />
                    <span className="truncate">{app.title}</span>
                  </span>
                  {app.summary && (
                    <span className="text-xs text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                      {app.summary}
                    </span>
                  )}
                </CommandItem>
              );
            })}
          </CommandGroup>
        )}
      </CommandList>
    </Command>
  );
}

function CategoryRow({
  icon,
  label,
  value,
  emphasized,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  emphasized: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      type="button"
      className="w-full flex items-center gap-2 px-3 py-2 text-xs text-left hover:bg-[hsl(var(--accent))] transition-colors"
    >
      <span className="shrink-0">{icon}</span>
      <span className="text-[hsl(var(--foreground))] w-20 shrink-0">{label}</span>
      <span
        className={`flex-1 truncate ${emphasized ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--muted-foreground))]'}`}
      >
        {value}
      </span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// NewWorktreeForm — explicit "create from base" flow.
//
// Two visible inputs:
//   1. Base branch — any existing branch (including ones currently checked
//      out elsewhere; that's exactly the case ``-b`` was designed for).
//   2. New branch name — pre-filled with ``mewbo/<base-slug>-<short-id>`` so
//      the result is always unique and obviously session-owned. Editable so
//      the user keeps full control and can pick a non-mewbo name (e.g. when
//      they intend the branch to outlive the worktree).
//
// A "use existing branch" toggle exposes the single-branch flow for
// the rarer case where the user has already manually created a free branch
// they want a worktree on. KISS: same form, just disables the "new name"
// field and submits without ``base``.
// ---------------------------------------------------------------------------

type NewWorktreeFormProps = {
  branches: string[];
  currentBranch: string | null;
  branchesInUse: string[];
  /** Branches that already back a managed worktree — disabled in "reuse" mode. */
  existingWorktreeBranches: string[];
  loading: boolean;
  onSubmit: (input: CreateWorktreeInput) => void;
};

function NewWorktreeForm({
  branches,
  currentBranch,
  branchesInUse,
  existingWorktreeBranches,
  loading,
  onSubmit,
}: NewWorktreeFormProps) {
  // Mode toggle. Default = "new" (the recommended workflow).
  const [mode, setMode] = useState<'new' | 'existing'>('new');

  // Base branch (used in both modes — in "existing" mode it IS the branch).
  const initialBase = currentBranch ?? branches[0] ?? '';
  const [base, setBase] = useState<string>(initialBase);

  // Name of the branch to create (only used in "new" mode).
  const [newBranch, setNewBranch] = useState<string>(() =>
    initialBase ? defaultMewboBranchName(initialBase) : '',
  );

  // Re-prefill the new-branch name whenever the base changes — the user
  // can still override afterwards. We don't overwrite if they've already
  // edited away from a mewbo/ default (heuristic: starts with the prefix).
  useEffect(() => {
    setNewBranch((cur) => {
      if (!base) return cur;
      if (!cur || cur.startsWith(MEWBO_BRANCH_PREFIX)) {
        return defaultMewboBranchName(base);
      }
      return cur;
    });
  }, [base]);

  const inUse = new Set(branchesInUse);
  const existingWtSet = new Set(existingWorktreeBranches);

  const canSubmit = (() => {
    if (loading) return false;
    if (!base) return false;
    if (mode === 'new') {
      const trimmed = newBranch.trim();
      if (!trimmed) return false;
      // The new branch must not already exist (git would refuse) — cheap
      // local check against the visible branch list.
      if (branches.includes(trimmed)) return false;
      return true;
    }
    // "existing" mode: the chosen branch must be free (not currently
    // checked out anywhere) and not already a managed worktree branch.
    if (inUse.has(base)) return false;
    if (existingWtSet.has(base)) return false;
    return true;
  })();

  const submit = () => {
    if (!canSubmit) return;
    if (mode === 'new') {
      onSubmit({ branch: newBranch.trim(), base });
    } else {
      onSubmit({ branch: base });
    }
  };

  // Tailwind shorthand reused for both inputs. These are bare `<select>`/
  // `<input>` elements rather than the vendored `ui/input.tsx`, so they inherit
  // no floor of their own — they carry the same `text-field md:text-sm` pair the
  // shadcn input does, or iOS zooms the viewport when either one takes focus.
  // Mono is earned here: both hold a git ref, which is machine text the user
  // copies and compares character by character.
  const inputCls =
    'w-full text-field md:text-sm rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1 font-mono';

  return (
    <div className="px-3 py-2 flex flex-col gap-2 text-xs">
      {/* Mode toggle: explicit + transparent, no surprise behaviour. */}
      <div className="flex items-center gap-1 text-2xs text-[hsl(var(--muted-foreground))]">
        <button
          type="button"
          onClick={() => setMode('new')}
          className={`px-2 py-0.5 rounded ${
            mode === 'new'
              ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))] font-medium'
              : 'hover:bg-[hsl(var(--accent))]'
          }`}
        >
          New branch from base
        </button>
        <button
          type="button"
          onClick={() => setMode('existing')}
          className={`px-2 py-0.5 rounded ${
            mode === 'existing'
              ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))] font-medium'
              : 'hover:bg-[hsl(var(--accent))]'
          }`}
        >
          Use existing branch
        </button>
      </div>

      {/* Base branch picker — in "existing" mode this IS the branch. */}
      <label className="flex flex-col gap-1">
        <span className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
          {mode === 'new' ? 'Base branch' : 'Branch'}
        </span>
        <select
          value={base}
          onChange={(e) => setBase(e.target.value)}
          disabled={loading || branches.length === 0}
          className={inputCls}
        >
          {branches.map((b) => {
            // In "existing" mode disable branches that can't back a worktree.
            const disabled =
              mode === 'existing' && (inUse.has(b) || existingWtSet.has(b));
            const suffix = inUse.has(b)
              ? ' (in use)'
              : existingWtSet.has(b)
                ? ' (worktree exists)'
                : b === currentBranch
                  ? ' (current)'
                  : '';
            return (
              <option key={b} value={b} disabled={disabled}>
                {b}
                {suffix}
              </option>
            );
          })}
        </select>
      </label>

      {/* New branch name — only visible in "new" mode. */}
      {mode === 'new' && (
        <label className="flex flex-col gap-1">
          <span className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
            New branch name
          </span>
          <div className="flex items-center gap-1">
            <input
              type="text"
              value={newBranch}
              onChange={(e) => setNewBranch(e.target.value)}
              placeholder="mewbo/feature-x-ab12cd"
              disabled={loading}
              className={inputCls}
            />
            <button
              type="button"
              onClick={() => setNewBranch(defaultMewboBranchName(base))}
              disabled={loading || !base}
              title="Generate a fresh mewbo/<base>-<id> name"
              className="shrink-0 px-1.5 py-1 rounded hover:bg-[hsl(var(--accent))] text-[hsl(var(--muted-foreground))]"
            >
              <RefreshCw className="w-3 h-3" />
            </button>
          </div>
          {newBranch && branches.includes(newBranch.trim()) && (
            <span className="text-xs text-amber-400">
              Branch already exists — pick a different name or switch to
              &ldquo;Use existing branch&rdquo;.
            </span>
          )}
        </label>
      )}

      {/* Transparency: spell out what the click is going to do. */}
      <p className="text-xs text-[hsl(var(--muted-foreground))] leading-snug">
        {mode === 'new' && base && newBranch.trim()
          ? `Will run: git worktree add -b ${newBranch.trim()} <path> ${base}`
          : mode === 'existing' && base
            ? `Will run: git worktree add <path> ${base}`
            : 'Pick a base branch to continue.'}
      </p>

      <button
        type="button"
        onClick={submit}
        disabled={!canSubmit}
        className="self-start inline-flex items-center gap-1.5 px-2 py-1 rounded border border-[hsl(var(--border))] bg-[hsl(var(--accent))] text-[hsl(var(--foreground))] disabled:opacity-50"
      >
        {loading ? (
          <Loader2 className="w-3 h-3 animate-spin" />
        ) : (
          <Plus className="w-3 h-3" />
        )}
        Create worktree
      </button>
    </div>
  );
}
