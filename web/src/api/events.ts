/**
 * The event contract, as types plus the checks that earn them.
 *
 * The wire shapes here mirror what `src/api/query.py` emits. Nothing enforces
 * that across the two languages, so this file is the place the contract is
 * written down twice on purpose -- and the reason every event is *validated*
 * rather than cast.
 *
 * `JSON.parse` returns `any`. Writing `as DoneEvent` on the result produces a
 * value TypeScript will happily let you read `.usage.input_tokens` from while
 * the server actually sent something else, and the failure surfaces as
 * `undefined` rendered into the page rather than as an error. A cast is a
 * claim about a value that arrived over a network; the functions below are the
 * only things in this client entitled to make one.
 *
 * The rule for a payload that does not match is to **reject the event, not
 * repair it**. A `rows` event missing its `truncated` flag could be defaulted
 * to `false`, and that default would be the client silently promising a result
 * is complete when the server never said so.
 */

export interface StageEvent {
  readonly kind: 'stage';
  readonly stage: string;
  readonly status: string;
}

export interface SqlEvent {
  readonly kind: 'sql';
  readonly sql: string;
  readonly attempt: number;
}

export interface RowsEvent {
  readonly kind: 'rows';
  readonly columns: readonly string[];
  readonly rows: readonly (readonly unknown[])[];
  readonly truncated: boolean;
}

export interface QueryStep {
  readonly stage: string;
  readonly duration_ms: number;
  readonly status: string;
}

export interface DoneEvent {
  readonly kind: 'done';
  readonly rowCount: number;
  readonly executed: boolean;
  readonly steps: readonly QueryStep[];
  readonly inputTokens: number;
  readonly outputTokens: number;
}

export interface ErrorEvent {
  readonly kind: 'error';
  readonly code: string;
  readonly message: string;
  readonly requestId: string;
}

export type QueryEvent = StageEvent | SqlEvent | RowsEvent | DoneEvent | ErrorEvent;

/** An event name the server may send that ends the stream. */
export const TERMINAL_KINDS: ReadonlySet<QueryEvent['kind']> = new Set(['done', 'error']);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const str = (value: unknown): string | null => (typeof value === 'string' ? value : null);

const num = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

const bool = (value: unknown): boolean | null => (typeof value === 'boolean' ? value : null);

/**
 * Turn one framed event into a typed event, or `null` if it is not one.
 *
 * `null` covers three different situations on purpose -- an event name this
 * client does not know, a payload that is not JSON, and a payload whose shape
 * is wrong. The caller treats all three the same way: ignore the event and keep
 * reading. That is the right response to the first (a newer server sending
 * something extra) and a survivable one for the others, because the stream
 * still ends with `done` or `error` and those are the events that decide what
 * the person sees.
 */
export function parseEvent(name: string, data: string): QueryEvent | null {
  let payload: unknown;
  try {
    payload = JSON.parse(data);
  } catch {
    return null;
  }
  if (!isRecord(payload)) {
    return null;
  }

  switch (name) {
    case 'stage': {
      const stage = str(payload.stage);
      const status = str(payload.status);
      return stage === null || status === null ? null : { kind: 'stage', stage, status };
    }
    case 'sql': {
      const sql = str(payload.sql);
      const attempt = num(payload.attempt);
      return sql === null ? null : { kind: 'sql', sql, attempt: attempt ?? 1 };
    }
    case 'rows':
      return parseRows(payload);
    case 'done':
      return parseDone(payload);
    case 'error':
      return parseError(payload);
    default:
      return null;
  }
}

function parseRows(payload: Record<string, unknown>): RowsEvent | null {
  const { columns, rows, truncated } = payload;
  if (!Array.isArray(columns) || !columns.every((c) => typeof c === 'string')) {
    return null;
  }
  if (!Array.isArray(rows) || !rows.every(Array.isArray)) {
    return null;
  }
  const isTruncated = bool(truncated);
  if (isTruncated === null) {
    // Not defaulted. See the module docstring: a missing `truncated` defaulted
    // to `false` is the client asserting completeness on the server's behalf.
    return null;
  }
  return {
    kind: 'rows',
    columns: columns as string[],
    rows: rows as unknown[][],
    truncated: isTruncated,
  };
}

function parseDone(payload: Record<string, unknown>): DoneEvent | null {
  const rowCount = num(payload.row_count);
  const executed = bool(payload.executed);
  if (rowCount === null || executed === null) {
    return null;
  }
  const usage = isRecord(payload.usage) ? payload.usage : {};
  return {
    kind: 'done',
    rowCount,
    executed,
    steps: parseSteps(payload.steps),
    inputTokens: num(usage.input_tokens) ?? 0,
    outputTokens: num(usage.output_tokens) ?? 0,
  };
}

function parseSteps(value: unknown): QueryStep[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const steps: QueryStep[] = [];
  for (const item of value) {
    if (!isRecord(item)) {
      continue;
    }
    const stage = str(item.stage);
    const duration = num(item.duration_ms);
    const status = str(item.status);
    if (stage !== null && duration !== null && status !== null) {
      steps.push({ stage, duration_ms: duration, status });
    }
  }
  return steps;
}

function parseError(payload: Record<string, unknown>): ErrorEvent | null {
  const inner = isRecord(payload.error) ? payload.error : null;
  if (inner === null) {
    return null;
  }
  const code = str(inner.code);
  const message = str(inner.message);
  if (code === null || message === null) {
    return null;
  }
  return { kind: 'error', code, message, requestId: str(inner.request_id) ?? '' };
}
