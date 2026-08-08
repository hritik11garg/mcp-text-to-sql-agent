import type { State } from '../state/reducer';

/**
 * Rows rendered at most, whatever the server sent.
 *
 * The server's ceiling is `MAX_ROWS_CEILING`, 5000 by default, and 5000 table
 * rows is a page that stutters while a person scrolls it. This is a *display*
 * bound and it is reported separately from the server's, because "the database
 * had more rows than this" and "the browser is showing you fewer than it
 * received" are different facts, and one banner covering both would let a
 * reader take the wrong one away.
 */
export const DISPLAY_ROW_LIMIT = 200;

interface Props {
  readonly state: State;
}

/** `null` is a value; so is the empty string; they must not look the same. */
export function formatCell(value: unknown): { text: string; empty: boolean } {
  if (value === null || value === undefined) {
    return { text: 'NULL', empty: true };
  }
  if (typeof value === 'string') {
    return value === '' ? { text: '(empty)', empty: true } : { text: value, empty: false };
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return { text: String(value), empty: false };
  }
  // Dates, arrays, JSON columns. The server serialises with `default=str`, so
  // most arrive as strings already; this is the honest fallback for the rest.
  return { text: JSON.stringify(value) ?? String(value), empty: false };
}

export function ResultTable({ state }: Props): JSX.Element | null {
  const { columns, rows, truncated, phase, executed, rowCount } = state;

  if (phase === 'answered' && !executed) {
    return (
      <section>
        <div className="panel__head">
          <h3 className="panel__title">Result</h3>
          <span className="panel__aside">not executed</span>
        </div>
        <p className="note note--info">
          Explain only. The SQL above was generated and validated against the schema, and never
          run.
        </p>
      </section>
    );
  }

  if (columns.length === 0) {
    return null;
  }

  const shown = rows.slice(0, DISPLAY_ROW_LIMIT);
  const hiddenByBrowser = rows.length - shown.length;
  const total = rowCount || rows.length;

  return (
    <section>
      <div className="panel__head">
        <h3 className="panel__title">Result</h3>
        <span className={truncated ? 'panel__aside panel__aside--warn' : 'panel__aside'}>
          {total} row{total === 1 ? '' : 's'}
          {truncated ? ' · clipped' : ''}
        </span>
      </div>

      {truncated && (
        <p className="note note--warn">
          The server&apos;s row limit clipped this result. More rows matched than are shown here,
          so this is not the complete answer.
        </p>
      )}

      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              {columns.map((column, i) => (
                <th key={`${column}-${i}`} scope="col">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, r) => (
              <tr key={r}>
                {columns.map((_column, c) => {
                  const cell = formatCell(row[c]);
                  return (
                    <td key={c} className={cell.empty ? 'cell cell--empty' : 'cell'}>
                      {cell.text}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {hiddenByBrowser > 0 && (
        <p className="note">
          Showing the first {DISPLAY_ROW_LIMIT} of {rows.length} rows received. This is a display
          limit in the browser, not the server&apos;s.
        </p>
      )}
    </section>
  );
}
