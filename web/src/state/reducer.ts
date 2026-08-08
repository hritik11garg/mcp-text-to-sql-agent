/**
 * What the page knows, and how each event changes it.
 *
 * A reducer rather than a handful of `useState` calls, for one reason that is
 * specific to this screen: the interesting states are *combinations*. SQL has
 * arrived but rows have not. Rows arrived and were truncated. The stream ended
 * without executing because the question was asked in explain-only mode. An
 * error landed after the SQL was already on screen. Held as separate pieces of
 * state those combinations are reachable in orders nobody intended, and the
 * component ends up asking "do I have SQL but no rows and no error yet?" in
 * three places with three slightly different answers.
 *
 * It is also the whole of the logic, which means the whole of the logic is
 * testable without rendering anything.
 *
 * **Every event carries the elapsed time at which this browser saw it**, and
 * the reducer stores it rather than reading a clock. Two reasons. A reducer
 * that calls `performance.now()` is not a pure function and cannot be tested by
 * calling it. And the number matters: the rail down the side of the page is a
 * real time axis, so a stage's position is a measurement rather than a
 * decoration, and a measurement needs a defined origin -- here, the moment the
 * request was sent.
 */

import type { QueryEvent, QueryStep } from '../api/events';

export type Phase = 'idle' | 'asking' | 'answered' | 'failed' | 'stopped';

/** What the rail marks. `sql` and `rows` are data, not phases, and are not on it. */
export type MarkKind = 'stage' | 'done' | 'error';

export interface Mark {
  readonly kind: MarkKind;
  readonly label: string;
  /** Milliseconds from the request leaving this browser to the event reaching it. */
  readonly at: number;
  readonly failed: boolean;
}

export interface Failure {
  readonly code: string;
  readonly message: string;
  readonly requestId: string;
}

export interface State {
  readonly phase: Phase;
  readonly question: string;
  readonly marks: readonly Mark[];
  readonly sql: string | null;
  readonly attempts: number;
  readonly columns: readonly string[];
  readonly rows: readonly (readonly unknown[])[];
  /** The server clipped the result set. Never inferred -- only ever the server's word. */
  readonly truncated: boolean;
  readonly rowCount: number;
  /** `false` when the question was asked in explain-only mode. */
  readonly executed: boolean;
  readonly steps: readonly QueryStep[];
  readonly inputTokens: number;
  readonly outputTokens: number;
  readonly failure: Failure | null;
  /** Elapsed at the terminal event, or `0` while still in flight. */
  readonly settledAt: number;
}

export const initialState: State = {
  phase: 'idle',
  question: '',
  marks: [],
  sql: null,
  attempts: 0,
  columns: [],
  rows: [],
  truncated: false,
  rowCount: 0,
  executed: false,
  steps: [],
  inputTokens: 0,
  outputTokens: 0,
  failure: null,
  settledAt: 0,
};

export type Action =
  | { readonly type: 'ask'; readonly question: string }
  | { readonly type: 'event'; readonly event: QueryEvent; readonly at: number }
  | { readonly type: 'stop'; readonly at: number }
  | { readonly type: 'reset' };

export function reduce(state: State, action: Action): State {
  switch (action.type) {
    case 'ask':
      // A fresh start rather than a merge. Leaving the previous answer's rows
      // on screen while a new question is in flight is the failure mode where
      // someone reads an old result as the answer to a new question.
      return { ...initialState, phase: 'asking', question: action.question };

    case 'stop':
      return state.phase === 'asking'
        ? { ...state, phase: 'stopped', settledAt: action.at }
        : state;

    case 'reset':
      return initialState;

    case 'event':
      return applyEvent(state, action.event, action.at);
  }
}

function applyEvent(state: State, event: QueryEvent, at: number): State {
  // Events arriving after the stream has settled are dropped rather than
  // applied. The server always terminates with exactly one `done` or `error`,
  // so anything after one is either a bug on this side or a server this client
  // does not understand -- and in both cases the settled result is the more
  // trustworthy of the two.
  if (state.phase !== 'asking') {
    return state;
  }

  const mark = (kind: MarkKind, label: string, failed = false): Mark[] => [
    ...state.marks,
    { kind, label, at, failed },
  ];

  switch (event.kind) {
    case 'stage':
      return { ...state, marks: mark('stage', event.stage, event.status !== 'ok') };

    case 'sql':
      return { ...state, sql: event.sql, attempts: Math.max(state.attempts, event.attempt) };

    case 'rows':
      return {
        ...state,
        columns: event.columns,
        rows: event.rows,
        truncated: event.truncated,
      };

    case 'done':
      return {
        ...state,
        phase: 'answered',
        marks: mark('done', 'done'),
        rowCount: event.rowCount,
        executed: event.executed,
        steps: event.steps,
        inputTokens: event.inputTokens,
        outputTokens: event.outputTokens,
        settledAt: at,
      };

    case 'error':
      return {
        ...state,
        phase: 'failed',
        marks: mark('error', 'failed', true),
        failure: { code: event.code, message: event.message, requestId: event.requestId },
        settledAt: at,
      };
  }
}

/** Total wall time the server reported, in milliseconds. */
export function totalMs(steps: readonly QueryStep[]): number {
  return steps.reduce((sum, step) => sum + step.duration_ms, 0);
}

/**
 * How long each mark's phase took, by subtracting the one before it.
 *
 * The marks record *arrival* times, which is a cumulative axis; a phase's
 * duration is the gap. Deriving it here rather than storing it keeps one
 * number on the state and no chance of the two disagreeing.
 */
export function durationsOf(marks: readonly Mark[]): number[] {
  let previous = 0;
  return marks.map((m) => {
    const gap = Math.max(0, m.at - previous);
    previous = m.at;
    return gap;
  });
}
