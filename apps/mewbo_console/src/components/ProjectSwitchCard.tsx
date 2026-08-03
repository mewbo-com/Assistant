import { ArrowRight, FolderSymlink, GitBranch } from 'lucide-react';

import type { ProjectSwitchMeta } from '../types';
import { Badge } from './agents';
import { LogEventCard } from './LogEventCard';

/**
 * The ONE readout for a completed `switch_project` call, rendered by BOTH
 * `ConversationTimeline` and `LogsView` — the `RunFailedCard` precedent for
 * putting an instrument-panel card into the conversation.
 *
 * It earns that placement: a switch moves every following tool call, file read
 * and spawned sub-agent into a different directory, so a reader who misses it
 * misreads everything after it. Folded into a tool-call group it would be one
 * unremarkable row among the shell calls whose meaning it changes.
 *
 * Composes `LogEventCard` rather than restating its chrome, so the `rounded-md`
 * body, the 2.5 px brand rail and the expand/collapse behaviour come from the
 * one place that owns them. The body is the resolved detail worth having but
 * not worth reading every time: an absolute path is machine text a user copies,
 * which is exactly what the mono law reserves the second typeface for.
 */
export function ProjectSwitchCard({
  meta,
  timestamp,
}: {
  meta: ProjectSwitchMeta;
  timestamp?: string;
}) {
  const to = meta.name || meta.project;
  const hasBody = Boolean(
    meta.cwd || meta.repo || meta.branch || meta.previousCwd || meta.projectInstructionsFound,
  );
  return (
    <LogEventCard
      icon={<FolderSymlink className="w-4 h-4 text-[hsl(var(--info))]" />}
      title={
        // A switch is a movement and half of it is the origin, so the header
        // shows it whenever there is one to show. On a session's FIRST switch
        // there is no previous KEY — the loop was handed a directory, never a
        // catalog key — so the origin falls back to the body's directory line
        // rather than inventing a name for where the session started.
        meta.previous ? (
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="truncate text-[hsl(var(--muted-foreground))]">
              {meta.previous}
            </span>
            <ArrowRight className="w-3 h-3 shrink-0 opacity-60" aria-hidden />
            <span className="truncate">{to}</span>
          </span>
        ) : (
          <span className="truncate">Switched to {to}</span>
        )
      }
      badge={<Badge color="blue">Project</Badge>}
      timestamp={timestamp}
      accent="blue"
    >
      {hasBody ? (
        <div className="space-y-1.5 text-xs text-[hsl(var(--muted-foreground))]">
          {/* Directories are the only mono lines here: a path is machine text a
              user copies. A repository slug and a branch are NAMES you read, so
              they take the proportional face, same call the session row makes. */}
          {/* Both carry a `title`, because the strike-through is the ONLY visual
              difference between them and styling alone conveys nothing to a
              screen reader — two absolute paths would read as an unexplained
              pair. */}
          {meta.previousCwd && (
            <p
              title="Previous working directory"
              className="font-mono break-all leading-relaxed line-through opacity-60">
              {meta.previousCwd}
            </p>
          )}
          {meta.cwd && (
            <p title="Working directory" className="font-mono break-all leading-relaxed">
              {meta.cwd}
            </p>
          )}
          {(meta.repo || meta.branch) && (
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
              {meta.repo && <span className="break-all">{meta.repo}</span>}
              {meta.branch && (
                <span className="inline-flex items-center gap-1">
                  <GitBranch className="w-3 h-3 shrink-0" aria-hidden />
                  <span>{meta.branch}</span>
                </span>
              )}
            </p>
          )}
          {/* The new directory brought its own instructions into the system
              prompt. Worth naming: it is the one part of a switch that changes
              how the agent behaves rather than only where it works. */}
          {meta.projectInstructionsFound && (
            <p className="leading-relaxed">
              Project instructions from this directory are now in effect.
            </p>
          )}
        </div>
      ) : undefined}
    </LogEventCard>
  );
}
