import { describe, expect, it } from 'vitest';

import { parseEvent } from './events';

describe('payloads that are not events', () => {
  it('rejects data that is not JSON', () => {
    expect(parseEvent('done', 'not json')).toBeNull();
  });

  it('rejects a JSON array, which is not an object', () => {
    expect(parseEvent('done', '[]')).toBeNull();
  });

  it('rejects JSON null', () => {
    expect(parseEvent('done', 'null')).toBeNull();
  });

  it('ignores an event name this client does not know', () => {
    // Not an error: a newer server sending an extra event must not break a
    // client written before it existed.
    expect(parseEvent('telemetry', '{"a":1}')).toBeNull();
  });
});

describe('stage', () => {
  it('parses', () => {
    expect(parseEvent('stage', '{"stage":"retrieve","status":"ok"}')).toEqual({
      kind: 'stage',
      stage: 'retrieve',
      status: 'ok',
    });
  });

  it('rejects a stage name that is not a string', () => {
    expect(parseEvent('stage', '{"stage":3,"status":"ok"}')).toBeNull();
  });
});

describe('sql', () => {
  it('parses and defaults the attempt', () => {
    expect(parseEvent('sql', '{"sql":"SELECT 1"}')).toEqual({
      kind: 'sql',
      sql: 'SELECT 1',
      attempt: 1,
    });
  });

  it('keeps a multi-line query intact', () => {
    // The reason the server encodes JSON: a raw newline in an SSE `data:` line
    // would end the event, and generated SQL is routinely multi-line.
    const event = parseEvent('sql', JSON.stringify({ sql: 'SELECT 1\nFROM t', attempt: 2 }));
    expect(event).toEqual({ kind: 'sql', sql: 'SELECT 1\nFROM t', attempt: 2 });
  });
});

describe('rows', () => {
  const ok = '{"columns":["a"],"rows":[[1]],"truncated":false}';

  it('parses', () => {
    expect(parseEvent('rows', ok)).toEqual({
      kind: 'rows',
      columns: ['a'],
      rows: [[1]],
      truncated: false,
    });
  });

  it('rejects a payload with no truncated flag rather than assuming false', () => {
    // The whole point. Defaulting here would be this client asserting the
    // result is complete when the server never said so.
    expect(parseEvent('rows', '{"columns":["a"],"rows":[[1]]}')).toBeNull();
  });

  it('rejects a truncated flag that is not a boolean', () => {
    expect(parseEvent('rows', '{"columns":["a"],"rows":[[1]],"truncated":"no"}')).toBeNull();
  });

  it('rejects columns that are not all strings', () => {
    expect(parseEvent('rows', '{"columns":["a",2],"rows":[],"truncated":false}')).toBeNull();
  });

  it('rejects rows that are not arrays of arrays', () => {
    expect(parseEvent('rows', '{"columns":["a"],"rows":[{"a":1}],"truncated":false}')).toBeNull();
  });

  it('keeps nulls in cells, which are values and not absences', () => {
    const event = parseEvent('rows', '{"columns":["a"],"rows":[[null]],"truncated":false}');
    expect(event).toMatchObject({ rows: [[null]] });
  });
});

describe('done', () => {
  it('parses steps and usage', () => {
    const wire = JSON.stringify({
      row_count: 2,
      executed: true,
      steps: [{ stage: 'answer', duration_ms: 12.5, status: 'ok' }],
      usage: { input_tokens: 100, output_tokens: 20 },
    });
    expect(parseEvent('done', wire)).toEqual({
      kind: 'done',
      rowCount: 2,
      executed: true,
      steps: [{ stage: 'answer', duration_ms: 12.5, status: 'ok' }],
      inputTokens: 100,
      outputTokens: 20,
    });
  });

  it('rejects a done event with no executed flag', () => {
    // `executed` is how explain-only is told from a query that returned
    // nothing, and those are different facts with the same shape.
    expect(parseEvent('done', '{"row_count":0}')).toBeNull();
  });

  it('survives usage being absent', () => {
    expect(parseEvent('done', '{"row_count":0,"executed":false}')).toMatchObject({
      inputTokens: 0,
      outputTokens: 0,
    });
  });

  it('drops a malformed step rather than the whole event', () => {
    const wire = '{"row_count":0,"executed":true,"steps":[{"stage":"a"},7]}';
    expect(parseEvent('done', wire)).toMatchObject({ steps: [] });
  });
});

describe('error', () => {
  it('parses the envelope the HTTP errors also use', () => {
    const wire = '{"error":{"code":"rate_limited","message":"too many","request_id":"r1"}}';
    expect(parseEvent('error', wire)).toEqual({
      kind: 'error',
      code: 'rate_limited',
      message: 'too many',
      requestId: 'r1',
    });
  });

  it('tolerates a missing request id', () => {
    expect(parseEvent('error', '{"error":{"code":"x","message":"y"}}')).toMatchObject({
      requestId: '',
    });
  });

  it('rejects an error with no code', () => {
    expect(parseEvent('error', '{"error":{"message":"y"}}')).toBeNull();
  });
});
