/**
 * Generated input for the SSE parser.
 *
 * **Why this file exists.** `sse.test.ts` asserts examples: a chunk split mid
 * payload, a chunk ending on a carriage return, a comment between two events.
 * Every one of those boundaries was chosen by the person who wrote the parser,
 * which is the failure this project keeps rediscovering -- nobody chose easy
 * inputs, everybody chose convenient ones. A network read ends where the
 * network ends it, and the parser's whole reason to exist is that those two
 * facts are unrelated.
 *
 * **The central claim, stated once.** *Where the chunks fall cannot change what
 * comes out.* Everything else here is a corollary or a bound.
 *
 * The reference model at the bottom splits the whole wire with a regular
 * expression and applies fields with `split(':')`. That is a different
 * algorithm from the incremental character scanner under test -- deliberately,
 * because a model that mirrors the implementation agrees with its bugs. The one
 * rule the model must copy rather than derive is the held carriage return, and
 * copying it is written down as a limitation rather than hidden.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import type { SseFrame } from './sse';
import { MAX_LINE_CHARS, SseParser, SseProtocolError } from './sse';

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/** All three terminators the WHATWG rule accepts, in equal measure. */
const TERMINATOR = fc.constantFrom('\n', '\r\n', '\r');

/**
 * Text that cannot itself end a line.
 *
 * Constructed rather than filtered: a `filter` that rejects every string
 * containing a newline throws away most of what the generator produces, and
 * the shrinker with it. Replacing the two characters keeps the distribution.
 */
const inline = (maxLength: number) =>
  fc.string({ unit: 'binary', maxLength }).map((text) => text.replace(/[\r\n]/g, '.'));

/**
 * One line of a well-formed stream.
 *
 * The field names are the real ones plus an unknown one, because a body that is
 * *nearly right* exercises more of the switch than noise does -- the same
 * reason the request-body fuzzer on the Python side mixes real field names with
 * generated text. `''` produces a line that begins with `:`, which is the
 * keepalive comment.
 */
const wellFormedLine = fc.oneof(
  fc
    .tuple(
      fc.constantFrom('event', 'data', 'id', 'retry', 'trace', ''),
      // The separator is generated, not fixed. A first draft wrote `field:value`
      // with no space -- which is legal, and is not what any server sends. The
      // mutation that stopped stripping the single leading space survived that
      // draft with every property green, because the space was never generated
      // to strip. Two spaces are here for the half of the rule that says only
      // one comes off.
      fc.constantFrom(':', ': ', ':  ', ':\t'),
      inline(24),
    )
    .map(([field, separator, value]) => field + separator + value),
  fc.constantFrom('event', 'data', 'id', 'retry'),
  fc.constant(''),
);

/** A stream of complete lines, each with an independently chosen terminator. */
const wellFormedWire = fc
  .array(fc.tuple(wellFormedLine, TERMINATOR), { maxLength: 14 })
  .map((lines) => lines.map(([line, end]) => line + end).join(''));

/**
 * A stream with no structure imposed at all.
 *
 * `unit: 'binary'` generates arbitrary UTF-16 code units, including lone
 * surrogates -- which is not an academic case here. Chunk boundaries are byte
 * boundaries on the wire, and a decoder that hands a lone surrogate to this
 * parser is a decoder doing the ordinary thing at the end of a read.
 */
const arbitraryWire = fc.oneof(
  { weight: 3, arbitrary: wellFormedWire },
  { weight: 1, arbitrary: fc.string({ unit: 'binary', maxLength: 120 }) },
);

/** Cut a string at generated points, keeping empty pieces. */
const chunkify = (wire: string, cuts: readonly number[]): string[] => {
  const points = [...new Set(cuts)].sort((a, b) => a - b);
  const chunks: string[] = [];
  let from = 0;
  for (const point of points) {
    chunks.push(wire.slice(from, point));
    from = point;
  }
  chunks.push(wire.slice(from));
  return chunks;
};

/** A wire together with one arbitrary way of splitting it into reads. */
const chunkedWire = arbitraryWire.chain((wire) =>
  fc
    .array(fc.nat({ max: wire.length }), { maxLength: 10 })
    .map((cuts) => ({ wire, chunks: chunkify(wire, cuts) })),
);

const feed = (chunks: readonly string[]): { frames: SseFrame[]; parser: SseParser } => {
  const parser = new SseParser();
  const frames = chunks.flatMap((chunk) => parser.push(chunk));
  return { frames, parser };
};

// --------------------------------------------------------------------------
// The central property
// --------------------------------------------------------------------------

describe('where the chunks fall cannot change what comes out', () => {
  it('gives the same frames however the wire is split', () => {
    fc.assert(
      fc.property(chunkedWire, ({ wire, chunks }) => {
        expect(chunks.join('')).toBe(wire);
        expect(feed(chunks).frames).toEqual(feed([wire]).frames);
      }),
    );
  });

  it('gives the same frames when every character arrives alone', () => {
    // The worst split there is, and the one no hand-written example covers past
    // a few characters. A parser that peeks ahead rather than holding state
    // survives every generated split above and dies here.
    fc.assert(
      fc.property(arbitraryWire, (wire) => {
        expect(feed([...wire]).frames).toEqual(feed([wire]).frames);
      }),
    );
  });

  it('agrees with an independently written whole-wire parser', () => {
    fc.assert(
      fc.property(chunkedWire, ({ wire, chunks }) => {
        expect(feed(chunks).frames).toEqual(modelFrames(wire));
      }),
    );
  });
});

// --------------------------------------------------------------------------
// What a frame can carry
// --------------------------------------------------------------------------

describe('a frame cannot carry a line terminator', () => {
  // The dual of the server-side property in
  // `tests/security/test_property_sse_framing.py`, which asserts that no
  // payload the server serialises produces two events. This is the same claim
  // read from the other end: nothing the parser hands out can re-split into
  // frames if a caller ever writes it back to a stream.
  it('never returns an event name containing CR or LF', () => {
    fc.assert(
      fc.property(chunkedWire, ({ chunks }) => {
        for (const frame of feed(chunks).frames) {
          expect(frame.event).not.toMatch(/[\r\n]/);
          expect(frame.event).not.toBe('');
        }
      }),
    );
  });

  it('never returns data containing a CR', () => {
    // `\n` is legal in data -- it is what repeated `data:` lines join with, and
    // it is meaningless as a terminator once the frame is parsed. A `\r` would
    // mean a terminator survived the scan.
    fc.assert(
      fc.property(chunkedWire, ({ chunks }) => {
        for (const frame of feed(chunks).frames) {
          expect(frame.data).not.toContain('\r');
        }
      }),
    );
  });
});

// --------------------------------------------------------------------------
// Pending
// --------------------------------------------------------------------------

describe('pending tells a finished stream from a cut one', () => {
  /** A stream of complete events, each of which dispatches. */
  const completeEvents = fc
    .array(
      fc.tuple(inline(16), fc.array(inline(20), { minLength: 1, maxLength: 3 })).map(
        ([name, dataLines]) =>
          `event: ${name}\n${dataLines.map((line) => `data: ${line}`).join('\n')}\n\n`,
      ),
      { minLength: 1, maxLength: 5 },
    )
    .map((events) => events.join(''));

  it('is false after a stream that ended on a dispatched event', () => {
    fc.assert(
      fc.property(completeEvents, (wire) => {
        expect(feed([wire]).parser.pending).toBe(false);
      }),
    );
  });

  it('is true when the last terminator never arrived', () => {
    // The socket closing here has not delivered an answer, and a UI that cannot
    // tell this from a finished stream reports silence as a result.
    fc.assert(
      fc.property(completeEvents, (wire) => {
        expect(feed([wire.slice(0, -1)]).parser.pending).toBe(true);
      }),
    );
  });
});

// --------------------------------------------------------------------------
// The bound
// --------------------------------------------------------------------------

describe('the line bound is a refusal, whatever the chunking', () => {
  it('refuses an unterminated line however it is delivered', () => {
    // The check reads the buffer at the end of `push`, so a line delivered in
    // one read and the same line delivered in forty must reach the same
    // verdict. Expensive input, so few runs: the shapes here are not varied,
    // only the split points.
    const oversized = 'x'.repeat(MAX_LINE_CHARS + 1);
    fc.assert(
      fc.property(fc.array(fc.nat({ max: oversized.length }), { maxLength: 6 }), (cuts) => {
        expect(() => feed(chunkify(oversized, cuts))).toThrow(SseProtocolError);
      }),
      { numRuns: 10 },
    );
  });

  it('accepts a line of exactly the bound', () => {
    // The half that stops the bound being tightened into uselessness: without
    // it, `MAX_LINE_CHARS = 1` passes the test above. Together the two pin the
    // boundary rather than the direction.
    const largest = 'x'.repeat(MAX_LINE_CHARS);
    fc.assert(
      fc.property(fc.array(fc.nat({ max: largest.length }), { maxLength: 6 }), (cuts) => {
        expect(() => feed(chunkify(largest, cuts))).not.toThrow();
      }),
      { numRuns: 10 },
    );
  });
});

// --------------------------------------------------------------------------
// The reference model
// --------------------------------------------------------------------------

/**
 * Parse a whole wire at once, by a different route than the class under test.
 *
 * Lines come from a single regular expression over the complete string rather
 * than from a character scan with held state; fields come from `split(':')`
 * rather than `indexOf`. Where the two implementations agree, the agreement is
 * evidence; where a model merely restates the code, it is not.
 *
 * **The one rule copied rather than derived:** a trailing `\r` is not a
 * terminator, because the `\n` that would pair with it may be in the next
 * chunk. A whole-wire parser has no next chunk and so cannot discover this
 * rule; it is stripped below so the tail is discarded like any other
 * incomplete line. The example suite is what pins that rule down.
 */
function modelFrames(wire: string): SseFrame[] {
  const body = wire.endsWith('\r') ? wire.slice(0, -1) : wire;
  const pieces = body.split(/\r\n|\r|\n/);
  // The last piece is whatever followed the final terminator: an incomplete
  // line, which the spec discards and so does the parser.
  const lines = pieces.slice(0, -1);

  const frames: SseFrame[] = [];
  let eventName = '';
  let dataLines: string[] = [];
  let sawField = false;

  for (const line of lines) {
    if (line === '') {
      if (sawField) {
        frames.push({
          event: eventName === '' ? 'message' : eventName,
          data: dataLines.join('\n'),
        });
        eventName = '';
        dataLines = [];
        sawField = false;
      }
      continue;
    }
    if (line.startsWith(':')) {
      continue;
    }

    const parts = line.split(':');
    const field = parts[0] as string;
    const rest = parts.slice(1).join(':');
    const value = rest.startsWith(' ') ? rest.slice(1) : rest;

    if (field === 'event') {
      eventName = value;
      sawField = true;
    } else if (field === 'data') {
      dataLines.push(value);
      sawField = true;
    } else if (field === 'id' || field === 'retry') {
      sawField = true;
    }
  }
  return frames;
}
