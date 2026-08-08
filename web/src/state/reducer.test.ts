import { describe, expect, it } from 'vitest';

import type { QueryEvent } from '../api/events';
import type { State } from './reducer';
import { durationsOf, initialState, reduce, totalMs } from './reducer';

const asking = (question = 'how many singers'): State =>
  reduce(initialState, { type: 'ask', question });

/** Feed events with explicit arrival times, which is what the rail is built on. */
const feed = (state: State, ...events: [QueryEvent, number][]): State =>
  events.reduce((acc, [event, at]) => reduce(acc, { type: 'event', event, at }), state);

const stage = (name: string): QueryEvent => ({ kind: 'stage', stage: name, status: 'ok' });

const rows: QueryEvent = { kind: 'rows', columns: ['n'], rows: [[3]], truncated: false };

const done: QueryEvent = {
  kind: 'done',
  rowCount: 1,
  executed: true,
  steps: [{ stage: 'answer', duration_ms: 10, status: 'ok' }],
  inputTokens: 5,
  outputTokens: 2,
};

describe('asking', () => {
  it('clears the previous answer instead of merging into it', () => {
    // Leaving the last result on screen under a new question is how someone
    // reads an old answer as the new one.
    const settled = feed(asking(), [rows, 10], [done, 20]);
    const next = reduce(settled, { type: 'ask', question: 'a different question' });
    expect(next).toEqual({ ...initialState, phase: 'asking', question: 'a different question' });
  });
});

describe('the rail records when this browser saw each phase end', () => {
  it('marks stages in arrival order, not a fixed template', () => {
    const state = feed(
      asking(),
      [stage('retrieve'), 1200],
      [stage('generate'), 3400],
      [stage('execute'), 3430],
    );
    expect(state.marks.map((m) => m.label)).toEqual(['retrieve', 'generate', 'execute']);
    expect(state.marks.map((m) => m.at)).toEqual([1200, 3400, 3430]);
  });

  it('does not mark sql or rows, which are data rather than phases', () => {
    const state = feed(asking(), [stage('generate'), 10], [
      { kind: 'sql', sql: 'SELECT 1', attempt: 1 },
      11,
    ]);
    expect(state.marks).toHaveLength(1);
  });

  it('marks the terminal event so the rail has an end', () => {
    const state = feed(asking(), [stage('retrieve'), 5], [done, 40]);
    expect(state.marks.map((m) => m.kind)).toEqual(['stage', 'done']);
    expect(state.settledAt).toBe(40);
  });

  it('flags a stage the server reported as failed', () => {
    const state = feed(asking(), [{ kind: 'stage', stage: 'execute', status: 'error' }, 9]);
    expect(state.marks[0]?.failed).toBe(true);
  });

  it('records a failure as a mark too, so the axis ends where it stopped', () => {
    const state = feed(asking(), [
      { kind: 'error', code: 'llm_unavailable', message: 'no provider', requestId: 'r1' },
      770,
    ]);
    expect(state.marks).toEqual([{ kind: 'error', label: 'failed', at: 770, failed: true }]);
  });
});

describe('durationsOf', () => {
  it('turns arrival times into per-phase durations', () => {
    // Arrival is cumulative; a phase's duration is the gap. This is the
    // difference between a rail that shows a 20 s retrieval and one that shows
    // four evenly spaced ticks.
    const state = feed(
      asking(),
      [stage('retrieve'), 20_000],
      [stage('generate'), 22_000],
      [stage('execute'), 22_030],
    );
    expect(durationsOf(state.marks)).toEqual([20_000, 2000, 30]);
  });

  it('measures the first phase from the request, not from zero events', () => {
    const state = feed(asking(), [stage('retrieve'), 1500]);
    expect(durationsOf(state.marks)).toEqual([1500]);
  });

  it('never returns a negative gap if timestamps arrive out of order', () => {
    const state = feed(asking(), [stage('a'), 100], [stage('b'), 40]);
    expect(durationsOf(state.marks)).toEqual([100, 0]);
  });

  it('is empty for no marks', () => {
    expect(durationsOf([])).toEqual([]);
  });
});

describe('data events', () => {
  it('keeps the highest attempt when the model self-corrects', () => {
    const state = feed(
      asking(),
      [{ kind: 'sql', sql: 'SELECT bad', attempt: 1 }, 10],
      [{ kind: 'sql', sql: 'SELECT good', attempt: 2 }, 20],
    );
    expect(state.sql).toBe('SELECT good');
    expect(state.attempts).toBe(2);
  });

  it('takes truncated from the server and never infers it', () => {
    const state = feed(asking(), [{ ...rows, truncated: true } as QueryEvent, 10]);
    expect(state.truncated).toBe(true);
  });
});

describe('the stream settles exactly once', () => {
  it('ignores events that arrive after done', () => {
    const state = feed(asking(), [rows, 10], [done, 20], [
      { kind: 'error', code: 'internal_error', message: 'late', requestId: '' },
      30,
    ]);
    expect(state.phase).toBe('answered');
    expect(state.failure).toBeNull();
    expect(state.marks).toHaveLength(1);
  });

  it('ignores events that arrive after an error', () => {
    const failed = feed(asking(), [
      { kind: 'error', code: 'llm_unavailable', message: 'no provider', requestId: 'r1' },
      10,
    ]);
    expect(feed(failed, [rows, 20]).rows).toEqual([]);
    expect(failed.phase).toBe('failed');
  });

  it('keeps the SQL visible when the failure came after it', () => {
    // A validation failure is more informative next to the query that caused
    // it than on its own.
    const state = feed(
      asking(),
      [{ kind: 'sql', sql: 'SELECT nope', attempt: 1 }, 10],
      [{ kind: 'error', code: 'sql_validation_failed', message: 'no', requestId: '' }, 20],
    );
    expect(state.sql).toBe('SELECT nope');
    expect(state.phase).toBe('failed');
  });
});

describe('explain only', () => {
  it('ends answered but not executed', () => {
    const state = feed(asking(), [{ ...done, executed: false, rowCount: 0 } as QueryEvent, 30]);
    expect(state.phase).toBe('answered');
    expect(state.executed).toBe(false);
  });
});

describe('stopping', () => {
  it('leaves what had already arrived on screen', () => {
    const partway = feed(asking(), [{ kind: 'sql', sql: 'SELECT 1', attempt: 1 }, 10]);
    const stopped = reduce(partway, { type: 'stop', at: 50 });
    expect(stopped.phase).toBe('stopped');
    expect(stopped.sql).toBe('SELECT 1');
    expect(stopped.settledAt).toBe(50);
  });

  it('does nothing to a stream that had already settled', () => {
    const settled = feed(asking(), [done, 20]);
    expect(reduce(settled, { type: 'stop', at: 99 }).phase).toBe('answered');
  });

  it('drops events that arrive after the stop', () => {
    const stopped = reduce(asking(), { type: 'stop', at: 10 });
    expect(feed(stopped, [rows, 20]).rows).toEqual([]);
  });
});

describe('totalMs', () => {
  it('sums the stages the server reported', () => {
    expect(
      totalMs([
        { stage: 'answer', duration_ms: 1.4, status: 'ok' },
        { stage: 'execute', duration_ms: 2.2, status: 'ok' },
      ]),
    ).toBeCloseTo(3.6);
  });

  it('is zero for no steps rather than NaN', () => {
    expect(totalMs([])).toBe(0);
  });
});
