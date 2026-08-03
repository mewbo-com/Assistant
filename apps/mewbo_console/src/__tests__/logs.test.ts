import { expect, test } from 'vitest';
import { buildLogs } from '../utils/logs';
import { EventRecord } from '../types';

// ── Fixtures ──────────────────────────────────────────────────────────
// The `agent_batch` envelope shape is `spawn_agent.py:run_batch_async`'s
// return payload — see also `orchestration_cards.py:_render_spawn_batch`
// on the CLI side, which this test's expectations mirror.

function batchToolResultEvent(overrides: {
  toolId?: string;
  agentId?: string;
  agents: Array<Record<string, unknown>>;
  spawned?: number;
  accepted?: number;
  rejected?: number;
  dispatched?: number;
  tasks?: Array<Record<string, unknown>>;
  durationMs?: number;
}): EventRecord {
  const {
    toolId = 'spawn_agents',
    agentId = 'root0000',
    agents,
    spawned,
    accepted,
    rejected,
    dispatched,
    tasks,
    durationMs,
  } = overrides;
  const result: Record<string, unknown> = {
    kind: 'agent_batch',
    text: 'Spawned summary',
    agents,
    agent_ids: agents.map((a) => a.agent_id ?? null),
  };
  if (spawned !== undefined) result.spawned = spawned;
  if (accepted !== undefined) result.accepted = accepted;
  if (rejected !== undefined) result.rejected = rejected;
  if (dispatched !== undefined) result.dispatched = dispatched;
  return {
    ts: '2026-01-01T00:00:00Z',
    type: 'tool_result',
    payload: {
      tool_id: toolId,
      agent_id: agentId,
      operation: 'run',
      success: true,
      result: JSON.stringify(result),
      tool_input: tasks ? { tasks } : {},
      duration_ms: durationMs,
    },
  };
}

test('a spawn_agents batch result builds a spawn_batch log entry, not a generic shell card', () => {
  const events: EventRecord[] = [
    batchToolResultEvent({
      agents: [
        { index: 0, agent_id: 'a1111111aaaa', status: 'submitted', task: 'Map auth call sites' },
        { index: 1, agent_id: null, status: 'rejected', task: 'Rewrite middleware', reason: 'no free concurrency slot' },
      ],
      spawned: 1,
      rejected: 1,
    }),
  ];
  const logs = buildLogs(events);
  expect(logs).toHaveLength(1);
  const log = logs[0];
  expect(log.type).toBe('spawn_batch');
  if (log.type !== 'spawn_batch') throw new Error('unreachable');
  expect(log.spawnBatchSpawned).toBe(1);
  expect(log.spawnBatchRejected).toBe(1);
  expect(log.spawnBatchAgents).toHaveLength(2);
  expect(log.spawnBatchAgents[0]).toMatchObject({ index: 0, agentId: 'a1111111aaaa', status: 'submitted' });
  expect(log.spawnBatchAgents[1]).toMatchObject({ index: 1, agentId: null, status: 'rejected', reason: 'no free concurrency slot' });
  expect(log.spawnBatchCaller).toBe('root0000');
});

test('the OLD toolId gate (spawn_agent singular) previously swallowed a batch result — spawn_agent must also route to spawn_batch', () => {
  // Defensive: if a future envelope ever rides the singular tool_id, the
  // `kind` discriminator alone must still route it correctly.
  const events: EventRecord[] = [
    batchToolResultEvent({
      toolId: 'spawn_agent',
      agents: [{ index: 0, agent_id: 'b2222222bbbb', status: 'submitted', task: 'Solo task' }],
    }),
  ];
  const logs = buildLogs(events);
  expect(logs).toHaveLength(1);
  expect(logs[0].type).toBe('spawn_batch');
});

test('spawned/rejected counts fall back to counting agents when the envelope omits the summary fields', () => {
  const events: EventRecord[] = [
    batchToolResultEvent({
      agents: [
        { index: 0, agent_id: 'c1', status: 'submitted', task: 'one' },
        { index: 1, agent_id: 'c2', status: 'submitted', task: 'two' },
        { index: 2, agent_id: null, status: 'rejected', task: 'three', reason: 'unresolvable project' },
      ],
      // spawned/rejected deliberately omitted.
    }),
  ];
  const logs = buildLogs(events);
  const log = logs[0];
  if (log.type !== 'spawn_batch') throw new Error('unreachable');
  expect(log.spawnBatchSpawned).toBe(2);
  expect(log.spawnBatchRejected).toBe(1);
});

test('the scheduler-era envelope ({accepted, dispatched}) is read defensively alongside the historical {spawned, rejected} shape', () => {
  const events: EventRecord[] = [
    batchToolResultEvent({
      agents: [
        { index: 0, agent_id: 'd1', status: 'submitted', task: 'waiting for a free slot' },
        { index: 1, agent_id: 'd2', status: 'running', task: 'already dispatched' },
      ],
      accepted: 2,
      dispatched: 1,
      rejected: 0,
    }),
  ];
  const logs = buildLogs(events);
  const log = logs[0];
  if (log.type !== 'spawn_batch') throw new Error('unreachable');
  // `accepted` (new envelope) wins over the absent `spawned` (old envelope).
  expect(log.spawnBatchSpawned).toBe(2);
  expect(log.spawnBatchDispatched).toBe(1);
  expect(log.spawnBatchRejected).toBe(0);
  // No status this codebase mints is "queued" — a capacity-deferred unit is
  // `submitted` with a real agent_id (hypervisor.py `AgentStatus`, A2A v1.0).
  expect(log.spawnBatchAgents.map((a) => a.status)).toEqual(['submitted', 'running']);
});

test('dispatched is undefined (not 0) on the historical envelope — never fabricated', () => {
  const events: EventRecord[] = [
    batchToolResultEvent({
      agents: [{ index: 0, agent_id: 'e9', status: 'submitted', task: 'one' }],
      spawned: 1,
      rejected: 0,
    }),
  ];
  const logs = buildLogs(events);
  const log = logs[0];
  if (log.type !== 'spawn_batch') throw new Error('unreachable');
  expect(log.spawnBatchDispatched).toBeUndefined();
});

test('per-agent model/agentType are joined in from the matching tool_input.tasks[index] entry', () => {
  const events: EventRecord[] = [
    batchToolResultEvent({
      agents: [
        { index: 0, agent_id: 'e1', status: 'submitted', task: 'research' },
      ],
      tasks: [{ task: 'research', model: 'claude-sonnet-5', agent_type: 'general-purpose' }],
    }),
  ];
  const logs = buildLogs(events);
  const log = logs[0];
  if (log.type !== 'spawn_batch') throw new Error('unreachable');
  expect(log.spawnBatchAgents[0].model).toBe('claude-sonnet-5');
  expect(log.spawnBatchAgents[0].agentType).toBe('general-purpose');
});

test('a single (non-batch) spawn_agent result is unaffected — still builds spawn_submit', () => {
  const events: EventRecord[] = [
    {
      ts: '2026-01-01T00:00:00Z',
      type: 'tool_result',
      payload: {
        tool_id: 'spawn_agent',
        agent_id: 'root0000',
        operation: 'run',
        success: true,
        result: JSON.stringify({ agent_id: 'f1111111', status: 'submitted', task: 'do a thing', message: 'Agent spawned.' }),
        tool_input: { task: 'do a thing' },
      },
    },
  ];
  const logs = buildLogs(events);
  expect(logs).toHaveLength(1);
  expect(logs[0].type).toBe('spawn_submit');
});
