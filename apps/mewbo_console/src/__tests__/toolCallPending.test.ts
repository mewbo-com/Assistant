import { expect, test } from 'vitest';
import { buildLogs } from '../utils/logs';
import { EventRecord } from '../types';

// The engine emits `tool_call` immediately before dispatch and `tool_result`
// after the tool returns, correlated by `tool_call_id`. These cover the three
// shapes that matter: the pending row, its in-place settlement, and a legacy
// transcript that carries results only.

function toolCall(callId: string, command = 'sleep 60'): EventRecord {
  return {
    ts: '2026-01-01T00:00:00Z',
    type: 'tool_call',
    payload: {
      tool_call_id: callId,
      tool_id: 'shell',
      operation: 'run',
      tool_input: { command },
      agent_id: 'root0000',
      depth: 0,
      model: 'claude-sonnet-5',
    },
  };
}

function toolResult(callId: string | undefined, command = 'sleep 60'): EventRecord {
  return {
    ts: '2026-01-01T00:01:00Z',
    type: 'tool_result',
    payload: {
      ...(callId === undefined ? {} : { tool_call_id: callId }),
      tool_id: 'shell',
      operation: 'run',
      success: true,
      tool_input: { command },
      result: JSON.stringify({ kind: 'shell', command, exit_code: 0, stdout: 'done' }),
    },
  };
}

test('a RUNNING shell call carries the fields that route it to TerminalCard', () => {
  // The defect this pins: `renderShell` dispatches on `shellCommand`, so a
  // pending row without it fell through to the generic card and printed the
  // argument JSON — the custom component never rendered while the command ran.
  const logs = buildLogs([toolCall('call_1', 'ls -la /tmp')]);
  const log = logs[0];
  if (log.type !== 'shell') throw new Error('unreachable');
  expect(log.shellCommand).toBe('ls -la /tmp');
  // Nothing a running command cannot honestly report is invented.
  expect(log.shellExitCode).toBeUndefined();
  expect(log.shellStdout).toBeUndefined();
  expect(log.shellStderr).toBeUndefined();
  expect(log.shellDurationMs).toBeUndefined();
});

test('a non-shell tool has no pending custom card — its identity needs the result', () => {
  // `<DiffCard>`/`<FileReadCard>` key on result data, so there is no honest
  // pending variant; the row must NOT claim a terminal it cannot render.
  const logs = buildLogs([
    {
      ts: '2026-01-01T00:00:00Z',
      type: 'tool_call',
      payload: {
        tool_call_id: 'call_1',
        tool_id: 'search_replace_block',
        operation: 'set',
        tool_input: { file_path: 'a.ts', search: 'x', replace: 'y' },
      },
    },
  ]);
  const log = logs[0];
  if (log.type !== 'shell') throw new Error('unreachable');
  expect(log.pending).toBe(true);
  expect(log.shellCommand).toBeUndefined();
});

test('a bare-string shell argument still resolves a command', () => {
  const logs = buildLogs([
    {
      ts: '2026-01-01T00:00:00Z',
      type: 'tool_call',
      payload: {
        tool_call_id: 'call_1',
        tool_id: 'aider_shell_tool',
        operation: 'set',
        tool_input: '$ echo hi',
      },
    },
  ]);
  const log = logs[0];
  if (log.type !== 'shell') throw new Error('unreachable');
  expect(log.shellCommand).toBe('echo hi');
});

test('a tool_call alone renders one pending row carrying the input and no result claim', () => {
  const logs = buildLogs([toolCall('call_1')]);
  expect(logs).toHaveLength(1);
  const log = logs[0];
  if (log.type !== 'shell') throw new Error('unreachable');
  expect(log.pending).toBe(true);
  expect(log.toolCallId).toBe('call_1');
  expect(log.title).toBe('shell (run)');
  expect(log.shellInput).toContain('sleep 60');
  expect(log.shellOutput).toBeUndefined();
  expect(log.shellExitCode).toBeUndefined();
  expect(log.error).toBeUndefined();
  expect(log.model).toBe('claude-sonnet-5');
});

test('the matching tool_result REPLACES the pending row in place — never a second row', () => {
  const logs = buildLogs([toolCall('call_1'), toolResult('call_1')]);
  expect(logs).toHaveLength(1);
  const log = logs[0];
  if (log.type !== 'shell') throw new Error('unreachable');
  expect(log.pending).toBeUndefined();
  expect(log.shellCommand).toBe('sleep 60');
  expect(log.shellExitCode).toBe(0);
  expect(log.shellStdout).toBe('done');
});

test('a settled row keeps its position — an interleaved event does not reorder the trace', () => {
  const logs = buildLogs([
    toolCall('call_1'),
    { ts: '2026-01-01T00:00:30Z', type: 'agent_message', payload: { text: 'still working', agent_id: 'root0000', depth: 0 } },
    toolResult('call_1'),
  ]);
  expect(logs.map((l) => l.type)).toEqual(['shell', 'agent_message']);
});

test('a legacy tool_result with no preceding tool_call renders exactly as before', () => {
  const logs = buildLogs([toolResult(undefined)]);
  expect(logs).toHaveLength(1);
  const log = logs[0];
  if (log.type !== 'shell') throw new Error('unreachable');
  expect(log.pending).toBeUndefined();
  expect(log.shellCommand).toBe('sleep 60');
});

test('an empty tool_call_id is not correlatable — the result appends instead of upserting', () => {
  const logs = buildLogs([toolCall(''), toolResult('')]);
  expect(logs).toHaveLength(2);
  const pending = logs[0];
  if (pending.type !== 'shell') throw new Error('unreachable');
  expect(pending.pending).toBe(true);
  expect(pending.toolCallId).toBeUndefined();
  expect(logs[1].type).toBe('shell');
});

test('two concurrent calls settle onto their OWN rows', () => {
  const logs = buildLogs([
    toolCall('call_a', 'echo a'),
    toolCall('call_b', 'echo b'),
    toolResult('call_b', 'echo b'),
  ]);
  expect(logs).toHaveLength(2);
  const [first, second] = logs;
  if (first.type !== 'shell' || second.type !== 'shell') throw new Error('unreachable');
  expect(first.pending).toBe(true);
  expect(first.toolCallId).toBe('call_a');
  expect(second.pending).toBeUndefined();
  expect(second.shellCommand).toBe('echo b');
});
