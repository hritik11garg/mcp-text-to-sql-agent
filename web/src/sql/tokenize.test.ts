import { describe, expect, it } from 'vitest';

import { tokenize } from './tokenize';

const kinds = (sql: string) => tokenize(sql).map((t) => `${t.kind}:${t.text}`);

const roundTrip = (sql: string) =>
  tokenize(sql)
    .map((t) => t.text)
    .join('');

describe('the round trip is the safety property', () => {
  // Colour must never change what a person reads. If concatenation did not
  // return the input, the page would be showing SQL that is not the SQL that
  // ran -- which is worse than no highlighting at all.
  const cases = [
    'SELECT 1',
    "SELECT * FROM t WHERE name = 'O''Brien' -- trailing\n",
    'SELECT /* block */ "Odd Column" FROM "s"."t" LIMIT 10;',
    'select count(*) from a join b on a.id = b.id where x between 1.5 and 2',
    "SELECT '<script>alert(1)</script>' AS x",
    'SELECT $$dollar$$, 12.34, a_b$c FROM t',
    '',
    "   \n\t  ",
    "SELECT 'unterminated",
    'SELECT /* unterminated',
  ];

  for (const sql of cases) {
    it(`preserves ${JSON.stringify(sql.slice(0, 40))}`, () => {
      expect(roundTrip(sql)).toBe(sql);
    });
  }
});

describe('no token ever carries markup', () => {
  it('leaves angle brackets as text in a literal', () => {
    // The component renders token text as a React child, so this stays text.
    // The test is here because the property is easy to lose the moment someone
    // reaches for a highlighter that returns HTML.
    const tokens = tokenize("SELECT '<img src=x onerror=alert(1)>'");
    const literal = tokens.find((t) => t.kind === 'string');
    expect(literal?.text).toBe("'<img src=x onerror=alert(1)>'");
    expect(tokens.every((t) => typeof t.text === 'string')).toBe(true);
  });
});

describe('classification', () => {
  it('marks keywords case-insensitively', () => {
    expect(kinds('SeLeCt')).toEqual(['keyword:SeLeCt']);
  });

  it('does not mark a column that merely contains a keyword', () => {
    expect(kinds('selected')).toEqual(['plain:selected']);
  });

  it('treats a doubled quote inside a literal as an escape, not a close', () => {
    expect(kinds("'a''b'")).toEqual(["string:'a''b'"]);
  });

  it('treats a double-quoted name as an identifier', () => {
    expect(kinds('"Odd Column"')).toEqual(['identifier:"Odd Column"']);
  });

  it('runs a line comment to the newline and no further', () => {
    expect(kinds('-- hi\n1')).toEqual(['comment:-- hi', 'plain:\n', 'number:1']);
  });

  it('does not mistake a minus for a comment', () => {
    expect(kinds('1-2').some((k) => k.startsWith('comment'))).toBe(false);
  });

  it('merges adjacent runs of the same kind so the DOM stays small', () => {
    expect(tokenize('   ').length).toBe(1);
  });
});

describe('inputs chosen to be awkward', () => {
  it('is linear on a long unterminated literal', () => {
    // A hand-written scanner cannot backtrack. The regex people reach for here
    // can, and this is the input that makes it pathological.
    const sql = "SELECT '" + "a'".repeat(50_000);
    const started = performance.now();
    expect(roundTrip(sql)).toBe(sql);
    expect(performance.now() - started).toBeLessThan(1000);
  });

  it('handles a query that is only whitespace', () => {
    expect(tokenize('\n\n')).toEqual([{ kind: 'plain', text: '\n\n' }]);
  });
});
