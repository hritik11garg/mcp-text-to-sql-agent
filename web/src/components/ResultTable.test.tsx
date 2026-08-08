import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { State } from '../state/reducer';
import { initialState } from '../state/reducer';
import { DISPLAY_ROW_LIMIT, ResultTable, formatCell } from './ResultTable';

const answered = (over: Partial<State>): State => ({
  ...initialState,
  phase: 'answered',
  executed: true,
  ...over,
});

describe('formatCell', () => {
  it('shows NULL as NULL and marks it as not a value the database held', () => {
    expect(formatCell(null)).toEqual({ text: 'NULL', empty: true });
  });

  it('distinguishes an empty string from NULL', () => {
    // Both would render as nothing at all, and they are different answers.
    expect(formatCell('')).toEqual({ text: '(empty)', empty: true });
  });

  it('leaves a real string alone', () => {
    expect(formatCell('NULL')).toEqual({ text: 'NULL', empty: false });
  });

  it('renders zero and false rather than treating them as absent', () => {
    expect(formatCell(0).text).toBe('0');
    expect(formatCell(false).text).toBe('false');
  });
});

describe('what the table refuses to imply', () => {
  it('says so, loudly, when the server truncated the result', () => {
    render(
      <ResultTable
        state={answered({ columns: ['n'], rows: [[1]], truncated: true, rowCount: 1 })}
      />,
    );
    expect(screen.getByText(/row limit clipped this result/i)).toBeDefined();
    expect(screen.getByText(/not the complete answer/i)).toBeDefined();
  });

  it('says nothing about truncation when the server did not', () => {
    render(
      <ResultTable
        state={answered({ columns: ['n'], rows: [[1]], truncated: false, rowCount: 1 })}
      />,
    );
    expect(screen.queryByText(/row limit clipped this result/i)).toBeNull();
  });

  it('reports its own display limit separately from the server’s', () => {
    // Two different facts. One is "the database had more rows"; the other is
    // "the browser is showing fewer than it received".
    const rows = Array.from({ length: DISPLAY_ROW_LIMIT + 5 }, (_, i) => [i]);
    render(<ResultTable state={answered({ columns: ['n'], rows, rowCount: rows.length })} />);
    expect(screen.getByText(/display limit in the browser/i)).toBeDefined();
    expect(screen.queryByText(/row limit clipped this result/i)).toBeNull();
    expect(document.querySelectorAll('tbody tr')).toHaveLength(DISPLAY_ROW_LIMIT);
  });

  it('reports explain-only as not executed rather than as an empty result', () => {
    render(<ResultTable state={answered({ executed: false })} />);
    expect(screen.getByText(/Not executed/i)).toBeDefined();
  });
});

describe('cell contents are text', () => {
  it('renders markup from the database as characters, not elements', () => {
    // Row values are database contents. The database here is loaded from a
    // public benchmark archive, and in any real deployment it is whatever the
    // operator points at -- neither is a source this page may trust.
    const hostile = '<img src=x onerror="alert(1)">';
    render(<ResultTable state={answered({ columns: ['c'], rows: [[hostile]], rowCount: 1 })} />);
    expect(document.querySelector('img')).toBeNull();
    expect(screen.getByText(hostile)).toBeDefined();
  });

  it('renders a column name containing markup as characters too', () => {
    const hostile = '<script>x</script>';
    render(<ResultTable state={answered({ columns: [hostile], rows: [[1]], rowCount: 1 })} />);
    expect(document.querySelector('script')).toBeNull();
    expect(screen.getByText(hostile)).toBeDefined();
  });
});
