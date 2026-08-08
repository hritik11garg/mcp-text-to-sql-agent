import { tokenize } from '../sql/tokenize';

interface Props {
  readonly sql: string | null;
  readonly attempts: number;
}

/**
 * The generated SQL, which is the thing worth looking at.
 *
 * Rendered as one `<span>` per token with a class. **No `dangerouslySetInnerHTML`
 * anywhere** -- see `sql/tokenize.ts` for why that constraint drove the
 * highlighter's design rather than the other way round.
 *
 * `attempts` above one means the self-correction loop ran: the first query
 * failed validation, the error went back to the model, and this is what came
 * back. That is the part of the system worth seeing and it is invisible in the
 * answer.
 */
export function SqlPanel({ sql, attempts }: Props): JSX.Element | null {
  if (sql === null) {
    return null;
  }

  return (
    <section>
      <div className="panel__head">
        <h3 className="panel__title">Generated SQL</h3>
        {attempts > 1 && (
          <span className="panel__aside">self-corrected · attempt {attempts}</span>
        )}
      </div>
      <pre className="sql">
        <code>
          {tokenize(sql).map((token, index) => (
            <span key={index} className={`tok tok--${token.kind}`}>
              {token.text}
            </span>
          ))}
        </code>
      </pre>
    </section>
  );
}
