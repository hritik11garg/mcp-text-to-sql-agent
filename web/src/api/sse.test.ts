import { describe, expect, it } from 'vitest';

import { MAX_EVENT_CHARS, MAX_LINE_CHARS, SseParser, SseProtocolError } from './sse';

const frames = (parser: SseParser, ...chunks: string[]) =>
  chunks.flatMap((chunk) => parser.push(chunk));

describe('a whole event in one chunk', () => {
  it('parses the name and the payload', () => {
    const got = new SseParser().push('event: stage\ndata: {"stage":"retrieve"}\n\n');
    expect(got).toEqual([{ event: 'stage', data: '{"stage":"retrieve"}' }]);
  });

  it('defaults the name to "message" when the server sends only data', () => {
    expect(new SseParser().push('data: hello\n\n')).toEqual([{ event: 'message', data: 'hello' }]);
  });

  it('strips exactly one space after the colon, not the rest', () => {
    expect(new SseParser().push('data:  two spaces\n\n')[0]?.data).toBe(' two spaces');
  });

  it('accepts a field with no value', () => {
    expect(new SseParser().push('data\n\n')).toEqual([{ event: 'message', data: '' }]);
  });
});

describe('chunk boundaries are not line boundaries', () => {
  // The failure this whole class exists for: a network read can end anywhere,
  // and the payloads most likely to be split are the long ones -- which here
  // means the ones carrying rows.
  it('reassembles an event split mid-payload', () => {
    const parser = new SseParser();
    expect(frames(parser, 'event: sql\ndata: {"sql":"SEL', 'ECT 1"}\n\n')).toEqual([
      { event: 'sql', data: '{"sql":"SELECT 1"}' },
    ]);
  });

  it('reassembles an event split inside its field name', () => {
    const parser = new SseParser();
    expect(frames(parser, 'ev', 'ent: done\nda', 'ta: {}\n\n')).toEqual([
      { event: 'done', data: '{}' },
    ]);
  });

  it('emits nothing until the blank line arrives', () => {
    const parser = new SseParser();
    expect(parser.push('event: done\ndata: {}\n')).toEqual([]);
    expect(parser.push('\n')).toEqual([{ event: 'done', data: '{}' }]);
  });

  it('holds a trailing CR rather than treating it as a terminator', () => {
    // A `\r` at the end of a chunk may be the first half of a `\r\n`. Deciding
    // early turns one line ending into two, and two line endings dispatch an
    // event that was never complete.
    const parser = new SseParser();
    expect(parser.push('data: a\r')).toEqual([]);
    expect(parser.push('\ndata: b\r\n\r\n')).toEqual([{ event: 'message', data: 'a\nb' }]);
  });

  it('resolves a held CR as a terminator once the next chunk shows it was alone', () => {
    // The held `\r` is not lost and not guessed at -- it stays undecided until
    // a character arrives that settles which of the two line endings it was.
    // Here the chunk ends on the blank line's `\r`, so the event it closes is
    // not dispatched until the following chunk proves no `\n` was coming.
    const parser = new SseParser();
    expect(parser.push('data: a\rdata: b\r\r')).toEqual([]);
    expect(parser.push('data: c\r\r\n')).toEqual([
      { event: 'message', data: 'a\nb' },
      { event: 'message', data: 'c' },
    ]);
  });
});

describe('multiple events and multiple data lines', () => {
  it('returns every event completed by one chunk, in order', () => {
    const wire = 'event: stage\ndata: 1\n\nevent: stage\ndata: 2\n\n';
    expect(new SseParser().push(wire).map((f) => f.data)).toEqual(['1', '2']);
  });

  it('joins repeated data lines with a newline, per spec', () => {
    expect(new SseParser().push('data: one\ndata: two\n\n')[0]?.data).toBe('one\ntwo');
  });
});

describe('lines that are not fields', () => {
  it('ignores the keepalive comment', () => {
    const parser = new SseParser();
    expect(parser.push(': keepalive\n\n')).toEqual([]);
    expect(parser.push('event: done\ndata: {}\n\n')).toEqual([{ event: 'done', data: '{}' }]);
  });

  it('ignores leading blank lines rather than dispatching empty events', () => {
    expect(new SseParser().push('\n\n\nevent: done\ndata: {}\n\n')).toEqual([
      { event: 'done', data: '{}' },
    ]);
  });

  it('ignores id and retry without dropping the event they sit in', () => {
    expect(new SseParser().push('id: 7\nretry: 500\nevent: done\ndata: {}\n\n')).toEqual([
      { event: 'done', data: '{}' },
    ]);
  });

  it('ignores an unknown field so a newer server does not break this client', () => {
    expect(new SseParser().push('trace: abc\nevent: done\ndata: {}\n\n')).toEqual([
      { event: 'done', data: '{}' },
    ]);
  });
});

describe('the bounds are refusals, not truncations', () => {
  it('refuses a line that never ends', () => {
    // The unbounded case: a server that opens `data: ` and never sends a
    // newline makes the tab allocate until it dies.
    const parser = new SseParser();
    expect(() => parser.push('data: ' + 'x'.repeat(MAX_LINE_CHARS + 1))).toThrow(SseProtocolError);
  });

  it('refuses one event whose data lines add up past the cap', () => {
    // Each line is legal on its own. The attack is the accumulation, which is
    // why the check cannot live on the line.
    const parser = new SseParser();
    const line = 'data: ' + 'y'.repeat(100_000) + '\n';
    expect(() => {
      for (let i = 0; i <= MAX_EVENT_CHARS / 100_000; i += 1) {
        parser.push(line);
      }
    }).toThrow(SseProtocolError);
  });

  it('does not refuse a large event that stays under the cap', () => {
    const parser = new SseParser();
    const payload = 'z'.repeat(500_000);
    expect(parser.push(`data: ${payload}\n\n`)[0]?.data).toHaveLength(500_000);
  });
});

describe('pending', () => {
  it('is false when the last event was complete', () => {
    const parser = new SseParser();
    parser.push('event: done\ndata: {}\n\n');
    expect(parser.pending).toBe(false);
  });

  it('is true when the stream stopped mid-event', () => {
    // A socket that closes here has not delivered an answer, and silence would
    // look exactly like one that did.
    const parser = new SseParser();
    parser.push('event: done\ndata: {"row_c');
    expect(parser.pending).toBe(true);
  });

  it('is true when fields arrived but the blank line never did', () => {
    const parser = new SseParser();
    parser.push('event: done\ndata: {}\n');
    expect(parser.pending).toBe(true);
  });
});
