/**
 * Shared agent status palette — used by any card that renders an agent
 * lifecycle state as a chip, dot, or tree row. Bound to the semantic status
 * tokens in `index.css` (`--success`/`--warning`/`--info`/`--destructive`/
 * `--muted`) so light/dark theming comes for free.
 *
 * Keep the six keys aligned with `AgentStatus` in hypervisor.py `AgentStatus`
 * (A2A v1.0 has no `queued`/`pending` state — `submitted` already means
 * "acknowledged and accepted"; a capacity-deferred unit is `submitted` with a
 * real `agent_id`, never a distinct status). `AGENT_STATUS_KEYS` below is the
 * literal mirror `agentStatusAlignment.test.ts` pins against — if this file's
 * key set ever drifts from `hypervisor.py`, that test fails instead of a
 * comment quietly going stale. Non-matching strings fold to `submitted`.
 */

export type StatusKey =
  | 'submitted'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'rejected';

/** The exact six-member set, exposed so a test can assert against it without
 *  re-deriving it from `STATUS_STYLES`'s own keys (which would just prove the
 *  object equals itself). */
export const AGENT_STATUS_KEYS: readonly StatusKey[] = [
  'submitted',
  'running',
  'completed',
  'failed',
  'cancelled',
  'rejected',
];

export interface StatusStyle {
  text: string;
  bg: string;
  border: string;
  dot: string;
  ring: string;
}

export const STATUS_STYLES: Record<StatusKey, StatusStyle> = {
  submitted: {
    text: 'text-[hsl(var(--muted-foreground))]',
    bg: 'bg-[hsl(var(--muted))]',
    border: 'border-[hsl(var(--border))]',
    dot: 'bg-[hsl(var(--muted-foreground))]',
    ring: 'ring-[hsl(var(--muted-foreground))]/20',
  },
  running: {
    text: 'text-[hsl(var(--info))]',
    bg: 'bg-[hsl(var(--info)/0.1)]',
    border: 'border-[hsl(var(--info)/0.3)]',
    dot: 'bg-[hsl(var(--info))]',
    ring: 'ring-[hsl(var(--info)/0.3)]',
  },
  completed: {
    text: 'text-[hsl(var(--success))]',
    bg: 'bg-[hsl(var(--success)/0.1)]',
    border: 'border-[hsl(var(--success)/0.3)]',
    dot: 'bg-[hsl(var(--success))]',
    ring: 'ring-[hsl(var(--success)/0.3)]',
  },
  failed: {
    text: 'text-[hsl(var(--destructive-text))]',
    bg: 'bg-[hsl(var(--destructive)/0.1)]',
    border: 'border-[hsl(var(--destructive)/0.3)]',
    dot: 'bg-[hsl(var(--destructive))]',
    ring: 'ring-[hsl(var(--destructive)/0.3)]',
  },
  cancelled: {
    text: 'text-[hsl(var(--warning))]',
    bg: 'bg-[hsl(var(--warning)/0.1)]',
    border: 'border-[hsl(var(--warning)/0.3)]',
    dot: 'bg-[hsl(var(--warning))]',
    ring: 'ring-[hsl(var(--warning)/0.3)]',
  },
  // A permanent admission refusal (unresolvable project, no free concurrency
  // slot) — terminal and never recovers, same severity as `failed`. This key
  // was MISSING from this file even though `hypervisor.py` has carried
  // `rejected` as one of its six states from the start — `statusKey()` folded
  // it to `submitted`, so a permanently refused agent rendered as a muted
  // NON-terminal pill with a live-progress affordance, i.e. the UI claimed it
  // was still working.
  rejected: {
    text: 'text-[hsl(var(--destructive-text))]',
    bg: 'bg-[hsl(var(--destructive)/0.1)]',
    border: 'border-[hsl(var(--destructive)/0.3)]',
    dot: 'bg-[hsl(var(--destructive))]',
    ring: 'ring-[hsl(var(--destructive)/0.3)]',
  },
};

export const STATUS_ORDER: readonly StatusKey[] = [
  'running',
  'submitted',
  'completed',
  'failed',
  'cancelled',
  'rejected',
];

const KEY_SET = new Set<string>(STATUS_ORDER);

export function statusKey(s: string): StatusKey {
  return KEY_SET.has(s) ? (s as StatusKey) : 'submitted';
}

const TERMINAL = new Set<StatusKey>(['completed', 'failed', 'cancelled', 'rejected']);

export function isTerminal(s: string): boolean {
  return TERMINAL.has(statusKey(s));
}
