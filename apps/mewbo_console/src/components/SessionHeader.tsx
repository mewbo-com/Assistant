import { useCallback, useMemo, useState } from "react";
import { useLocation } from "wouter";
import {
  AppWindow,
  Archive,
  ArrowLeft,
  Ban,
  BookOpen,
  Clock,
  Download,
  ExternalLink,
  GitFork,
  MoreHorizontal,
  Menu,
  RotateCcw,
  Share,
  Sparkles,
  Square,
} from "lucide-react";

import { SessionContext, SessionSummary, SessionUsage } from "../types";
import { ProjectLabel } from "../utils/projectLabel";
import { StatusBadge } from "./StatusBadge";
import { formatSessionTime } from "../utils/time";
import { ModelSummary } from "./ModelSummary";
import { ContextWindowBar } from "./ContextWindowBar";
import { DiffStats } from "./DiffStats";
import { RepoLink } from "./wiki/RepoLink";
import { useProjects } from "../hooks/useProjects";
import { useIsMobile } from "../hooks/useIsMobile";
import { cn } from "../utils/cn";
import { LangfuseIcon } from "./LangfuseIcon";
import { EditableTitle } from "./EditableTitle";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { Button } from "./ui/button";
import { useIdeStatus } from "../hooks/useIdeStatus";
import { useWebIdeEnabled } from "../hooks/useWebIdeEnabled";
import { extendIde, stopIde, IdeApiError, IdeInstance } from "../api/ide";
import { TerminateSessionDialog } from "./triggers/TerminateSessionDialog";
import { useRailControls } from "./nav-rail/railControls";
import { buildHref } from "./apps/router";
import { useWikiProjects, useWikiSessionLink } from "./wiki/api/hooks";
import { buildHref as buildWikiHref } from "./wiki/router";

/** Coder brand mark, inlined from simple-icons (slug: coder, MIT/CC0) so we
 * avoid pulling the full @icons-pack/react-simple-icons dependency for one
 * glyph used in the IDE capsule. */
function SiCoder({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      aria-hidden="true"
    >
      <path d="M14.862 6.67H24v10.663h-9.138zM6.945 15.304c-1.934 0-3.366-1.264-3.366-3.305s1.432-3.323 3.366-3.365c1.411-.03 2.787.99 2.878 2.543l3.472-.106c-.076-2.802-2.33-4.706-6.35-4.706S0 8.558 0 12c0 3.426 3.046 5.635 6.945 5.635 3.898 0 6.29-1.935 6.38-4.782l-3.472-.077c-.152 1.553-1.497 2.528-2.908 2.528Z" />
    </svg>
  );
}

function formatRemaining(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  if (seconds <= 0) return "0m";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) {
    return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  return `${Math.max(1, minutes)}m`;
}

// --- Subcomponents ----------------------------------------------------------
// Hoisted to MODULE scope (not declared inside `SessionHeader`'s body). A
// component declared inside a render function gets a brand-new function identity
// every render, so React unmounts+remounts the subtree on every unrelated
// re-render (e.g. `useWebIdeEnabled`'s `getConfig()` query settling) — silently
// discarding any Radix open-state (dropdown/popover) living inside. Stable
// module-scope identity lets React diff and update instead.

// --- Artifact jump ----------------------------------------------------------
// Jumps from a builder/maintainer session back to the artifact it exists to
// build. Two kinds:
//   - App builder/maintainer session (`session.context.app_id`, stamped by
//     `AppLifecycle._agent_session_context` — see `types.ts`) → AppJumpButton.
//   - Wiki-origin session (indexing or Q&A, `session.origin === "wiki"`) →
//     WikiJumpButton. Wiki sessions carry no project slug on `session.context`
//     (their context only ever advertises the `wiki` capability), so the link
//     is resolved server-side via `GET /v1/wiki/sessions/<id>`
//     (`useWikiSessionLink`), a reverse lookup over the same job/answer→session
//     mappings the wiki session-end hooks already maintain.
//     THE DESTINATION IS PER KIND, and conflating them was a real defect: an
//     indexing session belongs to a PROJECT, so it deep-links to that project's
//     `landingPageId` when the cached `useWikiProjects()` record has one — the
//     same routing the nav rail's own wiki rows use
//     (`nav-rail/sections.tsx:WikiSection`) — else falls back to the gallery,
//     never guessing a page id. A Q&A session belongs to ONE ANSWER, which is
//     separately addressable (`?answer=<id>`), and its user is typically
//     waiting on that answer — routing them to the project's front door
//     answered a question nobody asked. `WikiSessionLink` is a discriminated
//     union precisely so this branch cannot be skipped silently again.
// Both render ONLY when resolvable: absent for a plain/unresolved session,
// never disabled-with-tooltip.
interface AppJumpButtonProps {
  appId: string;
}

function AppJumpButton({ appId }: AppJumpButtonProps) {
  const [, setLocation] = useLocation();
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => setLocation(buildHref({ kind: "detail", appId }))}
      aria-label="Open app"
      title="Open the app this session builds">
      <AppWindow className="size-4" />
      <span className="hidden lg:inline">Open app</span>
    </Button>
  );
}

interface WikiJumpButtonProps {
  sessionId: string;
}

function WikiJumpButton({ sessionId }: WikiJumpButtonProps) {
  const [, setLocation] = useLocation();
  const { data: link } = useWikiSessionLink(sessionId);
  // Cache read, not a new fetch — the nav rail's WikiSection already keeps
  // this query warm (`["wiki","projects"]`). Reused here so the deep link
  // matches EXACTLY the rail row's own routing (`sections.tsx:WikiSection`):
  // a project's `landingPageId` wins, else fall back to the gallery — never
  // guess a page id beyond what the project record explicitly carries.
  // Called unconditionally (hook order) but read only on the `indexing` arm:
  // an answer's deep link is fully described by the link itself.
  const { data: projects = [] } = useWikiProjects();
  if (!link) return null;

  // `answerId` is checked despite being typed required: it arrives from a
  // server that may briefly be an older build than the console in front of it,
  // and routing to `?answer=undefined` would be worse than today's behaviour.
  if (link.kind === "qa" && link.answerId) {
    const answerHref = buildWikiHref({
      kind: "qa",
      question: link.question,
      pageId: link.fromPageId,
      slug: link.slug,
      answer: link.answerId,
    });
    return (
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setLocation(answerHref)}
        aria-label="Open answer"
        title="Open the wiki answer this session is generating">
        <Sparkles className="size-4" />
        <span className="hidden lg:inline">Open answer</span>
      </Button>
    );
  }

  // A RUNNING index goes to the indexing screen, not the project. Progress —
  // phase, counters, ETA — is rendered ONLY there and on the gallery's active
  // tile; the session transcript this button sits on has none of its own, so
  // routing a live run to the project answered a question its viewer wasn't
  // asking. Worse on a FIRST index, where the project has no `landingPageId`
  // yet and the fallback below lands on the gallery. Both guards are real:
  // `jobId` is absent against an older server, and a graph-only index is
  // sessionless — neither may route to `undefined`.
  // Narrowed on `kind` rather than on the fields: the `qa` arm above returns
  // only when it also has an `answerId`, so a Q&A link that lacks one still
  // reaches here and the union is genuinely not narrowed yet.
  if (link.kind === "indexing" && link.active && link.jobId) {
    const indexingHref = buildWikiHref({ kind: "indexing", jobId: link.jobId });
    return (
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setLocation(indexingHref)}
        aria-label="Open indexing progress"
        title="Open the indexing progress for this session">
        <BookOpen className="size-4" />
        <span className="hidden lg:inline">Indexing progress</span>
      </Button>
    );
  }

  const project = projects.find((p) => p.slug === link.slug);
  const href = project?.landingPageId
    ? buildWikiHref({
        kind: "page",
        pageId: project.landingPageId,
        slug: project.slug,
        platform: project.source,
      })
    : buildWikiHref({ kind: "landing" });
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => setLocation(href)}
      aria-label="Open wiki"
      title="Open the wiki project this session belongs to">
      <BookOpen className="size-4" />
      <span className="hidden lg:inline">Open wiki</span>
    </Button>
  );
}

// --- IDE capsule ----------------------------------------------------------
// Single compound control. Two states:
//   * off   → single neutral <Button> "Open in Coder" with the Coder icon
//   * ready → 3-cell capsule: [status+Open | Extend | Stop]

/**
 * Whether a session names something an IDE could mount against: a
 * configured/managed project, a wiki project it indexes/maintains
 * (`slug`), or a Mewbo App it builds/maintains (`app_id`). This is the
 * client-side half of a contract with the backend's session→IDE resolver
 * (`_resolve_session_project` and its wiki/app tiers): the server resolves
 * the SAME three keys to a mountable checkout, so widening one side without
 * the other means the capsule offers an IDE the server then refuses with a
 * 409. Named once here rather than inlined at the render gate because "this
 * session has an IDE mount target" is a real product rule, not just a
 * three-way `||`.
 *
 * This mirrors the server's MOUNTABLE set, not its AUTHORIZATION for the wiki
 * tier — those are two different questions there. A wiki maintainer session's
 * `slug` is what binds `SessionSpec.slug` (a mount target); what actually
 * authorizes opening an IDE against it is the server-stamped
 * `wiki:maintain:<slug>` tag, because `context` on a request is merged
 * verbatim and a caller could put any slug in it. The two are allowed to
 * differ ONLY in the direction where the server is stricter: this predicate
 * may show the capsule for a session the server then 409s (cosmetic — the
 * next poll or click surfaces the refusal), but must never be the thing that
 * decides whether an IDE is authorized. Do not "fix" the asymmetry by having
 * this read the tag — that would pull an authorization decision into the
 * client, which is exactly the drift this comment exists to head off.
 *
 * **Precedence is decided per FIELD and is deliberately NOT uniform** — this
 * is the same law `apps/mewbo_console/CLAUDE.md`'s render-gating section
 * states for `SessionDetailView`'s subject fields ("take the freshest" is
 * wrong half the time), found again here rather than invented here:
 *
 * - **`project` reads the LIVE context** (`liveContext`, i.e. `getLastContext`
 *   — the most-recent context event's payload, VERBATIM, never merged). An
 *   auto-select session's agent can switch projects mid-task, and only the
 *   live read reflects where the session IS now rather than where it started
 *   or has ever been.
 * - **`slug`/`app_id` read the MERGED snapshot** (`session.context`, built
 *   server-side by `merge_context_events` — `dict.update` folded forward
 *   across every context event in order, confirmed against
 *   `mewbo_core/session/session_store.py`, not assumed). These are set ONCE
 *   at session creation (`WikiMaintainerSession.open` /
 *   `AppLifecycle._agent_session_context`) and never restated by the
 *   wiki/app harness on later turns, unlike `project`, which the console
 *   composer resends every turn. Reading them off `liveContext` — the
 *   verbatim latest event — is wrong the moment a session has a second turn:
 *   the key silently drops out of what "live" means, and the capsule
 *   vanishes for a session that never stopped being app/wiki-bound. This is
 *   exactly the trap `AppJumpButton` above already sidesteps by reading
 *   `session.context.app_id`, not live context — unifying the two onto one
 *   source is what reintroduces this bug, not a simplification of it.
 */
function hasIdeMountTarget(session: SessionSummary, liveContext?: SessionContext): boolean {
  return Boolean(liveContext?.project || session.context?.slug || session.context?.app_id);
}

interface IdeCapsuleProps {
  projectLabel: string;
  ideInstance: IdeInstance | null;
  ideBusy: boolean;
  onOpen: () => void;
  onExtend: () => void;
  onStop: () => void;
}

function IdeCapsule({ projectLabel, ideInstance, ideBusy, onOpen, onExtend, onStop }: IdeCapsuleProps) {
  // Once an instance exists, `project_name` is the server's own resolver
  // naming what it actually mounted (a configured project, a wiki checkout, an
  // app staging dir) — prefer it over the client-computed `projectLabel`,
  // which only ever knows about `context.project` and would otherwise render
  // the generic "project" fallback for a wiki/app mount. Before creation there
  // is no server answer yet, so `projectLabel` (or its own fallback) is all
  // there is.
  const displayLabel = ideInstance?.project_name || projectLabel;

  if (ideInstance?.status !== "ready") {
    return (
      <Button
        variant="neutral"
        size="sm"
        onClick={onOpen}
        title={`Open ${displayLabel} in Coder`}
        leadingIcon={<SiCoder className="w-3.5 h-3.5" />}>
        <span className="hidden lg:inline">Open in Coder</span>
        <span className="lg:hidden">Coder</span>
      </Button>
    );
  }

  const remaining = formatRemaining(ideInstance.remaining_seconds);
  // Custom cell styling — cells share the parent's pill and border, so they
  // can't use the Button primitive directly. Left-border hairlines divide cells.
  const cellBase =
    "inline-flex items-center gap-1.5 px-2 h-full text-xs text-[hsl(var(--foreground))] transition-colors disabled:opacity-50 [&:not(:first-child)]:border-l [&:not(:first-child)]:border-[hsl(var(--border))]";

  return (
    <div
      className="inline-flex items-center h-7 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/60 shadow-sm overflow-hidden"
      title={`Coder is running for ${displayLabel}`}>
      <button
        type="button"
        onClick={onOpen}
        title={`Open ${displayLabel} in Coder`}
        className={cn(cellBase, "hover:bg-[hsl(var(--accent))]")}>
        <span className="relative inline-flex items-center justify-center">
          <SiCoder className="w-3.5 h-3.5 text-[hsl(var(--success))]" />
          <span className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-[hsl(var(--success))] ring-1 ring-[hsl(var(--muted))] animate-pulse" />
        </span>
        <span className="font-medium tabular-nums">
          <span className="hidden lg:inline">Coder · </span>
          {remaining}
          <span className="hidden md:inline"> left</span>
        </span>
      </button>
      <button
        type="button"
        onClick={onExtend}
        disabled={ideBusy}
        title="Extend lifetime by 1 hour"
        className={cn(cellBase, "hover:bg-[hsl(var(--accent))]")}>
        <Clock className="size-3.5" />
        <span className="hidden md:inline">+1h</span>
      </button>
      <button
        type="button"
        onClick={onStop}
        disabled={ideBusy}
        title="Stop and remove the IDE container"
        className={cn(cellBase, "hover:bg-[hsl(var(--destructive))]/10 hover:text-[hsl(var(--destructive-text))]")}>
        <Square className="size-3.5" />
      </button>
    </div>
  );
}

// --- Overflow menu --------------------------------------------------------
// Hosts the secondary session actions, split into two groups by a hairline:
//   session actions: Archive / Copy share link / Download export
//   external links:  Langfuse (optional) / Terminate (optional)
interface OverflowMenuProps {
  sessionId: string | undefined;
  isArchived: boolean;
  isTerminated: boolean;
  langfuseUrl?: string | null;
  onArchive?: (sessionId: string) => void;
  onUnarchive?: (sessionId: string) => void;
  onShare?: (sessionId: string) => void;
  onExport?: (sessionId: string) => void;
  onTerminateClick: () => void;
}

function OverflowMenu({
  sessionId,
  isArchived,
  isTerminated,
  langfuseUrl,
  onArchive,
  onUnarchive,
  onShare,
  onExport,
  onTerminateClick,
}: OverflowMenuProps) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" iconOnly aria-label="More actions">
          <MoreHorizontal className="w-3.5 h-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuItem
          onSelect={() => {
            if (!sessionId) return;
            if (isArchived) { onUnarchive?.(sessionId); }
            else { onArchive?.(sessionId); }
          }}>
          {isArchived ? <RotateCcw className="w-3.5 h-3.5 mr-2" /> : <Archive className="w-3.5 h-3.5 mr-2" />}
          {isArchived ? "Restore" : "Archive"}
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            if (!sessionId) return;
            onShare?.(sessionId);
          }}>
          <Share className="w-3.5 h-3.5 mr-2" />
          Copy share link
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            if (!sessionId) return;
            onExport?.(sessionId);
          }}>
          <Download className="w-3.5 h-3.5 mr-2" />
          Download export
        </DropdownMenuItem>
        {langfuseUrl &&
        <>
          <DropdownMenuSeparator />
          <DropdownMenuItem asChild>
            <a href={langfuseUrl} target="_blank" rel="noopener noreferrer">
              <LangfuseIcon className="w-3.5 h-3.5 mr-2" />
              Open in Langfuse
              <ExternalLink className="size-3.5 ml-auto text-[hsl(var(--muted-foreground))]" />
            </a>
          </DropdownMenuItem>
        </>
        }
        {sessionId && !isTerminated &&
        <>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onSelect={onTerminateClick}
            className="text-[hsl(var(--destructive-text))] focus:text-[hsl(var(--destructive-text))] focus:bg-[hsl(var(--destructive))]/10">
            <Ban className="w-3.5 h-3.5 mr-2" />
            Terminate session
          </DropdownMenuItem>
        </>
        }
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export interface SessionHeaderProps {
  session: SessionSummary;
  /**
   * The session's LIVE context — the most recent `context` event's payload
   * (`getLastContext`), which `SessionDetailView` already derives for the
   * composer and the recovery model.
   *
   * It matters now that a session can move: the agent switches project mid-run
   * and records it as a `context` event, so `session.context` (a list-fetch
   * snapshot) names where the session STARTED while this names where it IS.
   * Optional, defaulting to the snapshot, so a caller that has no event stream
   * (and every existing test) keeps the old reading rather than blanking.
   */
  context?: SessionContext;
  /** Live usage snapshot for the subtitle (model summary + context bar). */
  usage: SessionUsage | null;
  /** Three-signal terminated flag from `SessionDetailView` (hides Terminate). */
  isTerminated: boolean;
  /** Authoritative status/done_reason from the live events poll — a fetch-time
   * snapshot (`session.status`/`session.done_reason`) can go stale between
   * polls (e.g. a plan-approval or off-tab start), so these win whenever the
   * poll has returned a value; `undefined` (poll not yet returned) falls back
   * to the snapshot. */
  liveStatus?: string;
  liveDoneReason?: string;
  onBack: () => void;
  onRenameTitle?: (sessionId: string, title: string) => Promise<void>;
  onRegenerateTitle?: (sessionId: string) => Promise<string>;
  onArchive?: (sessionId: string) => void;
  onUnarchive?: (sessionId: string) => void;
  onShare?: (sessionId: string) => void;
  onExport?: (sessionId: string) => void;
  langfuseUrl?: string | null;
}

/**
 * In-pane session header — the sticky top of the session detail view. Owns
 * every session-level obligation: back, the editable title + regenerate, the
 * timestamp/model/context subtitle, the global StatusBadge, the IDE capsule,
 * the overflow menu (archive/share/export/langfuse/terminate) and the
 * terminate dialog. On mobile a hamburger opens the rail.
 */
export function SessionHeader({
  session,
  context,
  usage,
  isTerminated,
  liveStatus,
  liveDoneReason,
  onBack,
  onRenameTitle,
  onRegenerateTitle,
  onArchive,
  onUnarchive,
  onShare,
  onExport,
  langfuseUrl,
}: SessionHeaderProps) {
  const isMobile = useIsMobile();
  // Poll-derived truth wins whenever the poll has returned a value; `??` only
  // falls back to the snapshot on `undefined` (poll not started/returned
  // yet), never on an intentionally empty `liveDoneReason`.
  // No `"idle"` default any more: this header renders from the route's session
  // id while the sessions listing is still in flight, and stamping "Idle" on a
  // session whose state simply is not known yet is a claim rather than a
  // placeholder — one that reads as wrong the moment a running session's poll
  // answers. The badge is omitted until either source has said something.
  const displayStatus = liveStatus ?? session.status;
  const displayDoneReason = liveDoneReason ?? session.done_reason;
  const { openMobileRail } = useRailControls();
  const [terminateOpen, setTerminateOpen] = useState(false);
  const isArchived = Boolean(session.archived);

  // Model(s) used — prefer usage data (richer: all models per turn) with a
  // fallback to the session context model when usage data is absent.
  const models = usage?.models_used?.length
    ? usage.models_used
    : session.context?.model ? [session.context.model] : [];
  const currentModel = usage?.root_model || session.context?.model || null;

  // Cache read, not a second fetch — `useProjects()` is the shared `["projects"]`
  // TanStack query the composer's picker and the landing page already keep warm.
  // Read here rather than prop-drilled so `SessionDetailView` stays untouched.
  const { projects } = useProjects();
  // The live context when the caller has one, else the list-fetch snapshot.
  const liveContext = context ?? session.context;
  // ONE resolver for both facts. The project label and the repo slug walk the
  // same managed-id → worktree-defers-to-parent chain, so building two would be
  // two chances to disagree about which workspace the session is in.
  const projectResolver = useMemo(() => new ProjectLabel(projects), [projects]);
  const repoSlug = useMemo(
    () => projectResolver.repoSlug(liveContext),
    [projectResolver, liveContext],
  );
  // Which project the session is running in RIGHT NOW. Legible here because it
  // is no longer a fixed property of the session: an auto-select session starts
  // in a temporary directory and the agent moves it, possibly more than once,
  // so the header is the only always-visible place that can answer "where is
  // this running" without opening the composer.
  const projectLabel = useMemo(
    () => projectResolver.resolve(liveContext).label,
    [projectResolver, liveContext],
  );
  // The SERVER-side total, i.e. the same number the landing-page row renders.
  // Deliberately NOT `SessionDetailView`'s timeline-derived `sessionFiles`
  // aggregate — two sources for one fact is the bug this replaces.
  const diffStat = session.diff_stat;

  // IDE state lives here so the header owns the capsule control. Both hooks are
  // safe to call unconditionally — `useIdeStatus(null)` no-ops and
  // `useWebIdeEnabled` returns null until the config resolves.
  const webIdeEnabled = useWebIdeEnabled();
  // `hasIdeMountTarget` reads `project` off the LIVE context (an auto-select
  // session has no project until the agent picks one) and `slug`/`app_id` off
  // the session's merged snapshot (set once at creation, never restated
  // per-turn) — see that function's doc for why the two sources differ.
  const ideTrackingSessionId =
    webIdeEnabled === true && hasIdeMountTarget(session, liveContext)
      ? session.session_id ?? null
      : null;
  const { instance: ideInstance, refresh: refreshIde, setInstance: setIdeInstance } =
    useIdeStatus(ideTrackingSessionId);
  const [ideBusy, setIdeBusy] = useState(false);

  const handleOpenIde = useCallback(() => {
    if (!session.session_id) return;
    window.open(`/ide-loader/${session.session_id}`, "_blank", "noopener");
  }, [session.session_id]);

  const handleExtendIde = useCallback(async () => {
    if (ideBusy || !session.session_id) return;
    setIdeBusy(true);
    try {
      const updated = await extendIde(session.session_id, { hours: 1 });
      setIdeInstance(updated);
    } catch (err) {
      if (err instanceof IdeApiError && err.status === 409) {
        const cap = err.body.max_deadline;
        window.alert(
          cap
            ? `Can't extend — the IDE has reached its maximum lifetime (${cap}).`
            : "Can't extend — the IDE has reached its maximum lifetime."
        );
      } else {
        const message = err instanceof Error ? err.message : String(err);
        window.alert(`Failed to extend IDE: ${message}`);
      }
    } finally {
      setIdeBusy(false);
    }
  }, [ideBusy, session.session_id, setIdeInstance]);

  const handleStopIde = useCallback(async () => {
    if (ideBusy || !session.session_id) return;
    if (!window.confirm("Stop the Web IDE container for this session?")) return;
    setIdeBusy(true);
    try {
      await stopIde(session.session_id);
      setIdeInstance(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      window.alert(`Failed to stop IDE: ${message}`);
    } finally {
      setIdeBusy(false);
      void refreshIde();
    }
  }, [ideBusy, session.session_id, refreshIde, setIdeInstance]);

  return (
    <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-2 border-b border-[hsl(var(--border))] bg-[hsl(var(--background))]/95 px-4 backdrop-blur">
      {/* Left zone: hamburger (mobile) + back + title/subtitle + status. */}
      <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden md:gap-3">
        {isMobile && (
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            onClick={openMobileRail}
            aria-label="Open navigation"
            title="Open navigation"
            className="shrink-0">
            <Menu className="size-4" />
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          onClick={onBack}
          aria-label="Back"
          className="shrink-0">
          <ArrowLeft className="w-3.5 h-3.5" />
        </Button>

        <div className="group flex min-w-0 flex-1 flex-col">
          {onRenameTitle && session.session_id ?
            <EditableTitle
              value={session.title ?? ""}
              onSave={(next) => onRenameTitle(session.session_id, next)}
              onRegenerate={onRegenerateTitle ? () => onRegenerateTitle(session.session_id) : undefined}
              className="text-sm font-semibold text-[hsl(var(--foreground))]" /> :

            <h2 className="truncate text-sm font-semibold text-[hsl(var(--foreground))]">
              {session.title}
            </h2>
          }
          {/* Subtitle line — timestamp · model · context window · project · repo · diff. */}
          <div className="hidden min-w-0 items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] md:flex">
            <span className="truncate">{formatSessionTime(session.created_at)}</span>
            {models.length > 0 &&
              <>
                <span aria-hidden className="shrink-0">·</span>
                <ModelSummary models={models} current={currentModel} />
              </>
            }
            {usage && usage.root_max_input_tokens > 0 &&
              <>
                <span aria-hidden className="shrink-0">·</span>
                <ContextWindowBar usage={usage} compact />
              </>
            }
            {projectLabel &&
              <>
                <span aria-hidden className="shrink-0">·</span>
                <span
                  className="flex shrink-0 items-center gap-1"
                  title={`Running in ${projectLabel}`}>
                  <GitFork className="size-3 shrink-0" aria-hidden />
                  <span className="max-w-[14ch] truncate">{projectLabel}</span>
                </span>
              </>
            }
            {repoSlug &&
              <>
                <span aria-hidden className="shrink-0">·</span>
                {/* `short` (owner/repo) keeps the dense header readable; the
                    host stays legible in RepoLink's own "Open on <host>" title. */}
                <RepoLink slug={repoSlug} display="short" className="shrink-0" />
              </>
            }
            {diffStat && (diffStat.additions > 0 || diffStat.deletions > 0) &&
              <>
                <span aria-hidden className="shrink-0">·</span>
                <DiffStats
                  additions={diffStat.additions}
                  deletions={diffStat.deletions}
                  className="shrink-0" />
              </>
            }
          </div>
        </div>

        {displayStatus && (
          <StatusBadge
            status={displayStatus}
            doneReason={displayDoneReason}
            compact={isMobile} />
        )}
      </div>

      {/* Right zone: artifact jump → IDE capsule → overflow menu. */}
      <div className="ml-3 flex shrink-0 items-center gap-1.5 md:ml-4">
        {session.context?.app_id &&
          <AppJumpButton appId={session.context.app_id} />
        }

        {session.origin === "wiki" && session.session_id &&
          <WikiJumpButton sessionId={session.session_id} />
        }

        {ideTrackingSessionId !== null &&
          <IdeCapsule
            projectLabel={projectLabel || "project"}
            ideInstance={ideInstance}
            ideBusy={ideBusy}
            onOpen={handleOpenIde}
            onExtend={handleExtendIde}
            onStop={handleStopIde} />
        }

        <OverflowMenu
          sessionId={session.session_id}
          isArchived={isArchived}
          isTerminated={isTerminated}
          langfuseUrl={langfuseUrl}
          onArchive={onArchive}
          onUnarchive={onUnarchive}
          onShare={onShare}
          onExport={onExport}
          onTerminateClick={() => setTerminateOpen(true)} />
      </div>

      {session.session_id &&
        <TerminateSessionDialog
          sessionId={session.session_id}
          open={terminateOpen}
          onOpenChange={setTerminateOpen} />
      }
    </header>
  );
}
