import { useMemo } from 'react';
import { Users, Eye, Code2 } from 'lucide-react';
import { LogEventCard } from './LogEventCard';
import { Tabs, TabsList, TabsTrigger, TabsContent } from './ui/tabs';
import { AgentIdChip, StatusDot } from './agents';
import { MODEL_TAG_CLASS } from '../utils/agents';
import { SpawnBatchAgentEntry } from '../types';
import { cn } from '../utils/cn';
import {
  STATUS_ORDER,
  STATUS_STYLES,
  StatusKey,
  statusKey,
} from '../utils/agentStatus';

interface SpawnAgentBatchCardProps {
  caller?: string;
  agents: SpawnBatchAgentEntry[];
  spawned: number;
  rejected: number;
  /** Additive scheduler-era count — the subset of `spawned` already running.
   *  Undefined on the historical envelope and on an old transcript. */
  dispatched?: number;
  durationMs?: number;
  timestamp?: string;
  rawText?: string;
}

const TASK_PREVIEW_LEN = 90;

function previewTask(task: string): string {
  if (task.length <= TASK_PREVIEW_LEN) return task;
  return task.slice(0, TASK_PREVIEW_LEN).trimEnd() + '…';
}

/* ── Row ─────────────────────────────────────────────────────────────── */

function BatchRow({ agent }: { agent: SpawnBatchAgentEntry }) {
  const key = statusKey(agent.status);
  const s = STATUS_STYLES[key];
  return (
    <div className="py-1.5 px-0.5">
      <div className="flex items-start gap-2 min-w-0">
        <StatusDot status={agent.status} pulse={agent.status === 'running'} className="mt-1.5 shrink-0" />
        {agent.agentId && <AgentIdChip agentId={agent.agentId} />}
        <span className={cn('shrink-0 text-2xs font-medium font-sans uppercase tracking-wider mt-px', s.text)}>
          {agent.status}
        </span>
        <span className="flex-1 min-w-0 text-sm font-sans text-[hsl(var(--foreground))] truncate">
          &quot;{previewTask(agent.task)}&quot;
        </span>
        {agent.agentType && (
          <span className="shrink-0 text-2xs font-medium text-agent-7 px-1.5 py-px rounded bg-agent-7/10 border border-agent-7/30 whitespace-nowrap">
            {agent.agentType}
          </span>
        )}
        {agent.model && <span className={cn(MODEL_TAG_CLASS, 'shrink-0')}>{agent.model}</span>}
      </div>
      {/* Refused slots carry WHY — the cause a batch fan-out otherwise
          discards (see spawn_agent.py:run_batch_async). `code` is the
          machine-readable cause and `reason` the prose; showing the code makes
          the refusal searchable and unambiguous, which prose alone is not.
          Either may be absent — `code` on transcripts predating it, `reason`
          never on an accepted entry. */}
      {(agent.reason || agent.code) && (
        <div
          className="mt-1 text-xs text-[hsl(var(--muted-foreground))] italic pl-2 border-l-2 border-l-[hsl(var(--destructive)/0.5)]"
          style={{ marginLeft: 20 }}
        >
          {agent.code && (
            <span className="not-italic font-mono text-2xs mr-1.5 text-[hsl(var(--destructive))]">
              {agent.code}
            </span>
          )}
          {agent.reason}
        </div>
      )}
    </div>
  );
}

/* ── Card ────────────────────────────────────────────────────────────── */

export function SpawnAgentBatchCard({
  caller,
  agents,
  spawned,
  rejected,
  dispatched,
  durationMs,
  timestamp,
  rawText,
}: SpawnAgentBatchCardProps) {
  const counts = useMemo(() => {
    const c: Partial<Record<StatusKey, number>> = {};
    for (const a of agents) {
      const k = statusKey(a.status);
      c[k] = (c[k] || 0) + 1;
    }
    return c;
  }, [agents]);

  const header = (
    <span className="flex items-center gap-2 min-w-0">
      <span className="text-xs font-medium">spawn_agents</span>
      <span className="text-2xs text-[hsl(var(--muted-foreground))] whitespace-nowrap">
        {agents.length} task{agents.length === 1 ? '' : 's'}
      </span>
    </span>
  );

  const badge = (
    <span className="flex items-center gap-1 shrink-0 text-2xs whitespace-nowrap">
      <span className="text-[hsl(var(--success))]">{spawned} spawned</span>
      {dispatched != null && (
        <span className="text-[hsl(var(--muted-foreground))]">, {dispatched} running</span>
      )}
      {rejected > 0 && (
        <span className="text-[hsl(var(--destructive-text))]">, {rejected} rejected</span>
      )}
      {durationMs != null && (
        <span className="text-[hsl(var(--muted-foreground))] pl-1">{durationMs}ms</span>
      )}
    </span>
  );

  return (
    <LogEventCard
      icon={<Users className="w-4 h-4 text-[hsl(var(--muted-foreground))]" />}
      title={header}
      badge={badge}
      timestamp={timestamp}
      accent="agent-7"
    >
      <Tabs
        defaultValue="rendered"
        className="w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-2">
          <TabsList className="h-7 p-0.5 bg-[hsl(var(--surface))]">
            <TabsTrigger value="rendered" className="h-6 px-2 text-2xs gap-1">
              <Eye className="w-3 h-3" /> Rendered
            </TabsTrigger>
            <TabsTrigger value="raw" className="h-6 px-2 text-2xs gap-1">
              <Code2 className="w-3 h-3" /> Raw
            </TabsTrigger>
          </TabsList>
          {caller && (
            <span className="text-2xs tracking-wider text-[hsl(var(--muted-foreground))] flex items-center gap-1">
              from <AgentIdChip agentId={caller} />
            </span>
          )}
        </div>

        <TabsContent value="rendered" className="mt-0">
          {agents.length === 0 ? (
            <div className="p-2 text-xs text-[hsl(var(--muted-foreground))]">
              No tasks in batch.
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 mb-2 pb-2 border-b border-dashed border-[hsl(var(--border))] text-2xs text-[hsl(var(--muted-foreground))]">
                <span className="text-2xs tracking-wider uppercase">Tasks</span>
                {STATUS_ORDER.filter((k) => counts[k]).map((k) => (
                  <span key={k}>
                    {counts[k]} <span className={STATUS_STYLES[k].text}>{k}</span>
                  </span>
                ))}
              </div>
              {agents.map((a) => (
                <BatchRow key={a.index} agent={a} />
              ))}
            </>
          )}
        </TabsContent>

        <TabsContent value="raw" className="mt-0">
          <pre className="p-3 -mx-3 -mb-3 bg-[hsl(var(--code-body))] text-[hsl(var(--code-fg))] text-2xs leading-[1.55] font-mono whitespace-pre-wrap break-words">
            {rawText || JSON.stringify(agents, null, 2)}
          </pre>
        </TabsContent>
      </Tabs>
    </LogEventCard>
  );
}
