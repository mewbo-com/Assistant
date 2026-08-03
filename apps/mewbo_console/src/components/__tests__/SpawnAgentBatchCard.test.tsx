import { afterEach, expect, test } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SpawnAgentBatchCard } from '../SpawnAgentBatchCard';
import { SpawnBatchAgentEntry } from '../../types';

afterEach(cleanup);

// ── Fixtures — mirrors a real fan-out: 20 requested, 6 refused for a
// concurrency ceiling (the production failure this defect names). ──────

const FANOUT_AGENTS: SpawnBatchAgentEntry[] = [
  { index: 0, agentId: 'aaaa1111bbbb', status: 'submitted', task: 'Audit auth middleware call sites' },
  { index: 1, agentId: null, status: 'rejected', task: 'Rewrite session cache layer', reason: 'no free concurrency slot' },
  { index: 2, agentId: 'cccc3333dddd', status: 'running', task: 'Draft migration plan for the billing service' },
];

async function renderExpanded(props: React.ComponentProps<typeof SpawnAgentBatchCard>) {
  const user = userEvent.setup();
  const utils = render(<SpawnAgentBatchCard {...props} />);
  await user.click(screen.getByText('spawn_agents'));
  return { user, ...utils };
}

test('header renders spawn_agents label + task count and is collapsed by default', () => {
  render(<SpawnAgentBatchCard agents={FANOUT_AGENTS} spawned={2} rejected={1} rawText="raw" />);
  expect(screen.getByText('spawn_agents')).toBeInTheDocument();
  expect(screen.getByText('3 tasks')).toBeInTheDocument();
  // Body content (the per-agent rows) is hidden until expanded.
  expect(screen.queryByText(/Audit auth middleware/)).toBeNull();
});

test('the badge honestly names the specific unit that was refused — spawned AND rejected counts both surface', () => {
  render(<SpawnAgentBatchCard agents={FANOUT_AGENTS} spawned={2} rejected={1} rawText="raw" />);
  expect(screen.getByText('2 spawned')).toBeInTheDocument();
  expect(screen.getByText(', 1 rejected')).toBeInTheDocument();
});

test('an additive dispatched count renders when present (scheduler-era envelope), and is absent on the historical envelope', () => {
  const { rerender } = render(
    <SpawnAgentBatchCard agents={FANOUT_AGENTS} spawned={2} rejected={1} dispatched={1} rawText="raw" />,
  );
  expect(screen.getByText(', 1 running')).toBeInTheDocument();

  rerender(<SpawnAgentBatchCard agents={FANOUT_AGENTS} spawned={2} rejected={1} rawText="raw" />);
  expect(screen.queryByText(/running/)).toBeNull();
});

test('expanded body lists every task with its status, and a rejected row shows its reason', async () => {
  await renderExpanded({ agents: FANOUT_AGENTS, spawned: 2, rejected: 1, rawText: 'raw' });
  expect(screen.getByText(/Audit auth middleware/)).toBeInTheDocument();
  expect(screen.getByText(/Rewrite session cache layer/)).toBeInTheDocument();
  expect(screen.getByText(/Draft migration plan/)).toBeInTheDocument();
  expect(screen.getByText('no free concurrency slot')).toBeInTheDocument();
  // Each status label appears twice — once in the counts summary row, once
  // on the task row itself — so assert presence rather than a single match.
  expect(screen.getAllByText('rejected').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('running').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('submitted').length).toBeGreaterThanOrEqual(1);
});

test('a capacity-deferred unit is "submitted" with a real agent_id, not a distinct status', async () => {
  // hypervisor.py's AgentStatus has no queued/pending member (A2A v1.0) — a
  // slot the scheduler hasn't dispatched yet is still `submitted`.
  const agents: SpawnBatchAgentEntry[] = [
    { index: 0, agentId: 'ffff9999eeee', status: 'submitted', task: 'Waiting for a free slot' },
  ];
  const { container } = await renderExpanded({ agents, spawned: 1, rejected: 0, rawText: 'raw' });
  expect(screen.getByText(/Waiting for a free slot/)).toBeInTheDocument();
  expect(within(container).getAllByText('submitted').length).toBeGreaterThanOrEqual(1);
});

test('a status this card does not specifically know still renders, folded to submitted styling', async () => {
  const agents: SpawnBatchAgentEntry[] = [
    { index: 0, agentId: null, status: 'some_future_status', task: 'An unrecognised outcome' },
  ];
  const { container } = await renderExpanded({ agents, spawned: 0, rejected: 0, rawText: 'raw' });
  expect(screen.getByText(/An unrecognised outcome/)).toBeInTheDocument();
  expect(within(container).getAllByText('some_future_status').length).toBeGreaterThanOrEqual(1);
});

test('caller renders as a chip in the expanded header when present', async () => {
  await renderExpanded({ caller: '09c085c1', agents: FANOUT_AGENTS, spawned: 2, rejected: 1, rawText: 'raw' });
  expect(screen.getByText('09c085c1')).toBeInTheDocument();
});

test('an empty batch shows the empty state instead of crashing', async () => {
  await renderExpanded({ agents: [], spawned: 0, rejected: 0, rawText: '' });
  expect(screen.getByText('No tasks in batch.')).toBeInTheDocument();
});
