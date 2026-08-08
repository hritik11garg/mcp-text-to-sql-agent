/**
 * A tiny SQL scanner, so the generated query can be coloured without HTML.
 *
 * **This exists to avoid `dangerouslySetInnerHTML`.** Every syntax highlighter
 * worth using returns a string of markup, and rendering it is the one way to
 * get script into a React page. The string being highlighted here is SQL a
 * language model wrote from a question a stranger typed -- the exact input an
 * attacker controls most of -- so the highlighter that returns markup is the
 * highlighter that turns a prompt-injection foothold into stored XSS. This one
 * returns tokens, the component turns tokens into elements, and React escapes
 * their text. There is no path from a token's contents to markup.
 *
 * **Hand-rolled rather than regular expressions.** Not style: a scanner is
 * linear by construction, and it cannot be given input that makes it
 * pathological. The obvious regex for a quoted string with escapes is the
 * classic backtracking shape, and the cost of it is a frozen tab on a query
 * nobody would have thought to test.
 *
 * It is deliberately approximate. Colour is a reading aid; being wrong about a
 * dialect corner costs a word the wrong colour, and nothing else depends on it.
 */

export type TokenKind = 'keyword' | 'string' | 'number' | 'comment' | 'identifier' | 'plain';

export interface Token {
  readonly kind: TokenKind;
  readonly text: string;
}

const KEYWORDS: ReadonlySet<string> = new Set([
  'select', 'from', 'where', 'group', 'by', 'order', 'having', 'limit', 'offset',
  'join', 'inner', 'left', 'right', 'full', 'outer', 'cross', 'on', 'using',
  'and', 'or', 'not', 'in', 'is', 'null', 'as', 'distinct', 'all', 'union',
  'intersect', 'except', 'case', 'when', 'then', 'else', 'end', 'with',
  'asc', 'desc', 'between', 'like', 'ilike', 'exists', 'any', 'some', 'over',
  'partition', 'cast', 'true', 'false', 'count', 'sum', 'avg', 'min', 'max',
]);

const isDigit = (ch: string): boolean => ch >= '0' && ch <= '9';

const isWordStart = (ch: string): boolean =>
  (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || ch === '_';

const isWordChar = (ch: string): boolean => isWordStart(ch) || isDigit(ch) || ch === '$';

/**
 * Split SQL into coloured runs.
 *
 * Concatenating every token's `text` returns the input exactly. That property
 * is what makes this safe to render: nothing is dropped, so nothing shown to a
 * person differs from the SQL that actually ran.
 */
export function tokenize(sql: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;

  const push = (kind: TokenKind, text: string): void => {
    const last = tokens[tokens.length - 1];
    if (last !== undefined && last.kind === kind) {
      tokens[tokens.length - 1] = { kind, text: last.text + text };
      return;
    }
    tokens.push({ kind, text });
  };

  while (i < sql.length) {
    const ch = sql[i] as string;

    // Line comment: -- to end of line.
    if (ch === '-' && sql[i + 1] === '-') {
      const stop = indexOfNewline(sql, i);
      push('comment', sql.slice(i, stop));
      i = stop;
      continue;
    }

    // Block comment. Unterminated runs to the end, which is what the server's
    // parser does with it too.
    if (ch === '/' && sql[i + 1] === '*') {
      const close = sql.indexOf('*/', i + 2);
      const stop = close === -1 ? sql.length : close + 2;
      push('comment', sql.slice(i, stop));
      i = stop;
      continue;
    }

    // Single-quoted literal. '' is an escaped quote, not a close.
    if (ch === "'") {
      const stop = closeQuote(sql, i, "'");
      push('string', sql.slice(i, stop));
      i = stop;
      continue;
    }

    // Double-quoted identifier -- how every name the catalog resolves is
    // written, so it is worth distinguishing from a literal.
    if (ch === '"') {
      const stop = closeQuote(sql, i, '"');
      push('identifier', sql.slice(i, stop));
      i = stop;
      continue;
    }

    if (isDigit(ch)) {
      let j = i;
      while (j < sql.length && (isDigit(sql[j] as string) || sql[j] === '.')) {
        j += 1;
      }
      push('number', sql.slice(i, j));
      i = j;
      continue;
    }

    if (isWordStart(ch)) {
      let j = i;
      while (j < sql.length && isWordChar(sql[j] as string)) {
        j += 1;
      }
      const word = sql.slice(i, j);
      push(KEYWORDS.has(word.toLowerCase()) ? 'keyword' : 'plain', word);
      i = j;
      continue;
    }

    push('plain', ch);
    i += 1;
  }

  return tokens;
}

/** Index just past the closing quote, or the end of the input. */
function closeQuote(sql: string, start: number, quote: string): number {
  let j = start + 1;
  while (j < sql.length) {
    if (sql[j] === quote) {
      if (sql[j + 1] === quote) {
        j += 2;
        continue;
      }
      return j + 1;
    }
    j += 1;
  }
  return sql.length;
}

function indexOfNewline(sql: string, from: number): number {
  for (let j = from; j < sql.length; j += 1) {
    if (sql[j] === '\n' || sql[j] === '\r') {
      return j;
    }
  }
  return sql.length;
}
