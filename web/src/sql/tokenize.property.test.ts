/**
 * Generated input for the SQL tokenizer.
 *
 * **The claim this file is about.** Concatenating every token's text returns
 * the input exactly. `tokenize.test.ts` already asserts it over ten hand-picked
 * strings, and `ENGINEERING_MATRIX.md` §38 named the missing half plainly: the
 * claim was already written down, the generated input was not.
 *
 * **Why the round trip is a security property and not a formality.** The SQL
 * shown on the page is the SQL that ran. It was written by a language model
 * from a question a stranger typed, and it is rendered as tokens precisely so
 * that no highlighter returning markup ever touches it. If the concatenation
 * dropped a character, the page would show a person a query that is not the
 * one the database executed -- `WHERE tenant = 1` reading as `WHERE tenant`,
 * say -- and every review of that output would be reviewing a fiction.
 *
 * The generators mix noise with SQL-shaped text on purpose. Pure noise almost
 * never produces `--`, `/*`, or a balanced pair of quotes, so a generator
 * without a grammar would never reach the three branches where the scanner
 * consumes more than one character at a time -- which is where losing one is
 * possible at all.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import type { Token } from './tokenize';
import { tokenize } from './tokenize';

const rejoin = (tokens: readonly Token[]) => tokens.map((token) => token.text).join('');

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/** Arbitrary UTF-16, lone surrogates included: the scanner indexes code units. */
const noise = fc.string({ unit: 'binary', maxLength: 80 });

/**
 * Fragments chosen to land on every branch of the scanner, including the ones
 * that run to the end of the input when they are not closed.
 */
const fragment = fc.oneof(
  fc.constantFrom(
    'SELECT',
    'select',
    'FROM',
    'where',
    'order',
    'by',
    'count',
    'selected',
    ' ',
    '\n',
    '\t',
    ',',
    '(',
    ')',
    '*',
    '.',
    ';',
    '=',
    '-',
    '--',
    '--\n',
    '/*',
    '*/',
    "'",
    "''",
    '"',
    '""',
    '$',
    '$$',
    '1',
    '1.5',
    '.5',
    '1..2',
    'a_b$c',
    '<script>',
  ),
  fc.string({ unit: 'binary', maxLength: 8 }),
);

const sqlish = fc.array(fragment, { maxLength: 24 }).map((parts) => parts.join(''));

const anySql = fc.oneof(
  { weight: 3, arbitrary: sqlish },
  { weight: 1, arbitrary: noise },
);

// --------------------------------------------------------------------------
// The round trip
// --------------------------------------------------------------------------

describe('the round trip holds for input nobody chose', () => {
  it('returns the input exactly, for SQL-shaped text', () => {
    fc.assert(
      fc.property(anySql, (sql) => {
        expect(rejoin(tokenize(sql))).toBe(sql);
      }),
    );
  });

  it('returns the input exactly, for arbitrary UTF-16', () => {
    fc.assert(
      fc.property(noise, (sql) => {
        expect(rejoin(tokenize(sql))).toBe(sql);
      }),
    );
  });

  it('returns the input exactly for every prefix of a generated query', () => {
    // Truncation is not hypothetical here: the SQL arrives over a stream and a
    // free-tier model runs out of output tokens mid-literal often enough that
    // the server-side validator had to be fixed for exactly that shape. Every
    // prefix is a string the page may have to render.
    fc.assert(
      fc.property(sqlish, (sql) => {
        for (let cut = 0; cut <= sql.length; cut += 1) {
          const prefix = sql.slice(0, cut);
          expect(rejoin(tokenize(prefix))).toBe(prefix);
        }
      }),
      // Quadratic in the query length, so fewer and shorter inputs.
      { numRuns: 25 },
    );
  });
});

// --------------------------------------------------------------------------
// The shape of what comes back
// --------------------------------------------------------------------------

describe('the token list is well formed', () => {
  it('never emits an empty token', () => {
    // An empty token renders an empty element, and -- more to the point -- it
    // is what a scanner that failed to advance produces just before it stops
    // producing anything at all.
    fc.assert(
      fc.property(anySql, (sql) => {
        for (const token of tokenize(sql)) {
          expect(token.text.length).toBeGreaterThan(0);
        }
      }),
    );
  });

  it('never emits two neighbours of the same kind', () => {
    // The merge in `push` exists so a run of whitespace is one element rather
    // than forty. It is asserted here because a merge that silently stops
    // working costs DOM size and nothing else, so nothing else would notice.
    fc.assert(
      fc.property(anySql, (sql) => {
        const kinds = tokenize(sql).map((token) => token.kind);
        for (let i = 1; i < kinds.length; i += 1) {
          expect(kinds[i]).not.toBe(kinds[i - 1]);
        }
      }),
    );
  });

  it('gives every token a text that matches the kind it claims', () => {
    // Not a restatement of the keyword list -- that would be the implementation
    // written twice. These are the shapes each branch is the only way to reach,
    // so a token labelled `string` that does not open with a quote means the
    // scanner took a branch it did not mean to.
    fc.assert(
      fc.property(anySql, (sql) => {
        for (const token of tokenize(sql)) {
          switch (token.kind) {
            case 'string':
              expect(token.text.startsWith("'")).toBe(true);
              break;
            case 'identifier':
              expect(token.text.startsWith('"')).toBe(true);
              break;
            case 'comment':
              expect(token.text.startsWith('--') || token.text.startsWith('/*')).toBe(true);
              break;
            case 'number':
              expect(token.text).toMatch(/^[0-9][0-9.]*$/);
              break;
            case 'keyword':
              expect(token.text).toMatch(/^[A-Za-z_][A-Za-z0-9_$]*$/);
              break;
            case 'plain':
              break;
          }
        }
      }),
    );
  });
});
