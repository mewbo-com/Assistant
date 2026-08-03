import React from 'react';
import { Archive, GitFork, Pin, PinOff, Play, RotateCcw } from 'lucide-react';
import { SessionSummary } from '../types';
import { StatusBadge } from './StatusBadge';
import { SessionOriginBadge } from './SessionOriginBadge';
import { DiffStats } from './DiffStats';
import { Button } from './ui/button';
import { FOCUS_RING } from './ui/focus-ring';
import { useRecoverSession } from '../hooks/useRecoverSession';
import { ProjectLabel } from '../utils/projectLabel';
import { formatSessionTime } from '../utils/time';

/**
 * Human labels for capabilities a session actually EXERCISED (proven by a tool
 * invocation), not the set a client advertised. An id outside this map still
 * renders — falls back to the raw id — so a newly-added capability never
 * silently disappears from the row.
 */
const CAPABILITY_LABELS: Record<string, string> = {
  stlite: 'Widget',
  apps: 'App',
  ask_user: 'Asked user',
  generative_ui: 'Generative UI',
};

interface SessionItemProps {
  session: SessionSummary;
  projectLabel: ProjectLabel;
  onClick: (sessionId: string) => void;
  onArchive?: (sessionId: string) => void;
  onUnarchive?: (sessionId: string) => void;
  onPin?: (sessionId: string) => void;
  onUnpin?: (sessionId: string) => void;
}
export function SessionItem({
  session,
  projectLabel,
  onClick,
  onArchive,
  onUnarchive,
  onPin,
  onUnpin
}: SessionItemProps) {
  const isArchived = Boolean(session.archived);
  const isPinned = Boolean(session.pinned);
  const { label: project, branch } = projectLabel.resolve(session.context);
  const capabilities = session.capabilities ?? [];
  const workspace = session.workspace ?? null;
  const recover = useRecoverSession();
  const showRecover = Boolean(session.recoverable) && !session.running;
  const handleRecover = (
    event: React.MouseEvent<HTMLButtonElement>,
    action: 'retry' | 'continue',
  ) => {
    event.stopPropagation();
    if (recover.isPending) return;
    // Carry the session's own model. Omitting it lets the server fall back to
    // config policy, so a row-level recovery would run on a different model
    // than the session it is recovering.
    recover.mutate({
      sessionId: session.session_id,
      action,
      model: session.context?.model,
    });
  };
  const handleArchive = (event: React.MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (isArchived) {
      onUnarchive?.(session.session_id);
    } else {
      onArchive?.(session.session_id);
    }
  };
  const handlePin = (event: React.MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (isPinned) {
      onUnpin?.(session.session_id);
    } else {
      onPin?.(session.session_id);
    }
  };
  return (
    <div
      data-testid="session-row"
      onClick={() => onClick(session.session_id)}
      className="group flex items-start gap-4 py-3.5 px-3 hover:bg-[hsl(var(--accent))] cursor-pointer transition-colors">

      <div className="flex flex-col gap-1.5 min-w-0 flex-1">
        {/* The title is the row's own emphasis, so it keeps `font-medium`;
            every metadata chip below it stays at the base weight and drops one
            scale step. Hierarchy in this row comes from weight and colour — the
            chips are deliberately NOT a third size. */}
        <h3 className="text-sm font-medium text-[hsl(var(--foreground))] group-hover:opacity-90 transition-colors line-clamp-2">
          {session.title}
        </h3>
        <div className="flex items-center gap-1.5 text-xs font-normal text-[hsl(var(--muted-foreground))]">
          {/* Quiet, always-visible marker (unlike the hover-revealed action
              button below) — the section header already says "Pinned", so this
              stays subdued rather than competing with the origin/status chips. */}
          {isPinned && (
            <Pin className="w-3 h-3 shrink-0" aria-label="Pinned" />
          )}
          <span className="whitespace-nowrap">{formatSessionTime(session.created_at)}</span>
          <SessionOriginBadge session={session} />
          {session.diff_stat && (session.diff_stat.additions > 0 || session.diff_stat.deletions > 0) && (
            <>
              <span>·</span>
              <span
                className="shrink-0"
                title={`${session.diff_stat.additions} lines added, ${session.diff_stat.deletions} lines removed`}
              >
                <DiffStats additions={session.diff_stat.additions} deletions={session.diff_stat.deletions} />
              </span>
            </>
          )}
          {project && (
            <>
              <span>·</span>
              <GitFork className="w-3 h-3 shrink-0" />
              <span className="truncate">{project}</span>
            </>
          )}
          {/* A branch and a workspace are NAMES you read, not machine text you
              copy, diff or align — so they take the proportional face like the
              rest of the row. The chip background and the medium weight carry
              the "this is a distinct token" job that the second typeface used
              to do. */}
          {branch && (
            <span className="truncate bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded text-2xs font-medium">{branch}</span>
          )}
          {workspace && (
            <span
              className="truncate bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded text-2xs font-medium"
              title={`Workspace ${workspace}`}
            >
              {workspace}
            </span>
          )}
          {capabilities.map((cap) => (
            <span
              key={cap}
              className="tracking-wide bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded text-2xs"
              title={`Used the "${cap}" capability`}
            >
              {CAPABILITY_LABELS[cap] ?? cap}
            </span>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-2 shrink-0 pt-0.5">
        {showRecover && (
          <div className="flex items-center gap-1.5 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
            <Button
              variant="neutral"
              size="sm"
              tone="warn"
              disabled={recover.isPending}
              leadingIcon={<Play className="w-3 h-3" />}
              onClick={(e) => handleRecover(e, 'continue')}
              title="Resume this session with its context intact"
            >
              Continue
            </Button>
            <Button
              variant="neutral"
              size="sm"
              tone="info"
              disabled={recover.isPending}
              leadingIcon={<RotateCcw className="w-3 h-3" />}
              onClick={(e) => handleRecover(e, 'retry')}
              title="Restart the last turn from scratch"
            >
              Restart
            </Button>
          </div>
        )}
        <StatusBadge
          status={session.status || 'idle'}
          doneReason={session.done_reason} />

        {(onPin || onUnpin) &&
        <button
          onClick={handlePin}
          aria-label={isPinned ? 'Unpin session' : 'Pin session'}
          title={isPinned ? 'Unpin session' : 'Pin session'}
          // Pinned state stays visible (not hover-gated) so a user can tell
          // which rows are pinned without hovering every one — the archive
          // button below keeps the hover-reveal convention since it carries
          // no persistent state of its own to show.
          className={`p-1 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-opacity ${FOCUS_RING} ${isPinned ? 'opacity-100' : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100'}`}>

            {isPinned ?
          <PinOff className="w-4 h-4" /> :

          <Pin className="w-4 h-4" />
          }
          </button>
        }
        {(onArchive || onUnarchive) &&
        <button
          onClick={handleArchive}
          aria-label={isArchived ? 'Unarchive session' : 'Archive session'}
          title={isArchived ? 'Unarchive session' : 'Archive session'}
          className={`p-1 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 ${FOCUS_RING} transition-opacity`}>

            {isArchived ?
          <RotateCcw className="w-4 h-4" /> :

          <Archive className="w-4 h-4" />
          }
          </button>
        }
      </div>
    </div>);

}
