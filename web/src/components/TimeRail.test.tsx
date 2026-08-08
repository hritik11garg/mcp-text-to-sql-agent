import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { Mark } from '../state/reducer';
import { TimeRail, formatMs, segmentHeight } from './TimeRail';

const mark = (label: string, at: number): Mark => ({ kind: 'stage', label, at, failed: false });

describe('segmentHeight', () => {
  it('is monotonic, so a longer phase is never drawn shorter', () => {
    const heights = [0, 10, 100, 1000, 5000, 20_000, 60_000].map(segmentHeight);
    const sorted = [...heights].sort((a, b) => a - b);
    expect(heights).toEqual(sorted);
  });

  it('keeps a very short phase visible instead of collapsing it to nothing', () => {
    // A 26 ms execution beside a 20 s retrieval is one pixel on a linear scale,
    // which reads as "did not happen".
    expect(segmentHeight(0)).toBeGreaterThan(0);
    expect(segmentHeight(26)).toBeGreaterThanOrEqual(segmentHeight(0));
  });

  it('keeps a very long phase on the page instead of scrolling off it', () => {
    expect(segmentHeight(600_000)).toBeLessThanOrEqual(200);
  });

  it('draws a twenty-second phase far taller than a twenty-millisecond one', () => {
    // The compression is real but it must not flatten the distinction the rail
    // exists to show -- this is the exact pair that hid a model load.
    expect(segmentHeight(20_000)).toBeGreaterThan(segmentHeight(20) * 3);
  });

  it('treats a negative duration as zero rather than producing NaN', () => {
    expect(Number.isFinite(segmentHeight(-5))).toBe(true);
  });
});

describe('formatMs', () => {
  it('uses milliseconds below a second', () => {
    expect(formatMs(26.4)).toBe('26 ms');
  });

  it('uses seconds at and above one', () => {
    expect(formatMs(1000)).toBe('1.00 s');
    expect(formatMs(20_412)).toBe('20.41 s');
  });

  it('does not claim more precision than the measurement has', () => {
    expect(formatMs(0.4)).toBe('0 ms');
  });
});

describe('the rail', () => {
  it('renders nothing before a question is asked', () => {
    const { container } = render(<TimeRail marks={[]} phase="idle" elapsedMs={0} />);
    expect(container.firstChild).toBeNull();
  });

  it('labels each phase with the duration it actually took', () => {
    render(
      <TimeRail
        marks={[mark('retrieve', 20_000), mark('generate', 22_000)]}
        phase="answered"
        elapsedMs={22_000}
      />,
    );
    expect(screen.getByText('retrieve')).toBeDefined();
    expect(screen.getByText('20.00 s')).toBeDefined();
    expect(screen.getByText('2.00 s')).toBeDefined();
  });

  it('shows an open segment while the answer is still arriving', () => {
    // The twenty seconds a cold retrieval takes is exactly when a page must not
    // look like it has stopped.
    render(<TimeRail marks={[]} phase="asking" elapsedMs={4200} />);
    expect(screen.getByText('working')).toBeDefined();
    expect(screen.getByText('4.20 s')).toBeDefined();
  });

  it('closes the axis once the stream settles', () => {
    render(<TimeRail marks={[mark('retrieve', 10)]} phase="answered" elapsedMs={10} />);
    expect(screen.queryByText('working')).toBeNull();
  });

  it('says the scale is compressed, so a segment is not read as a measurement', () => {
    render(<TimeRail marks={[mark('retrieve', 10)]} phase="answered" elapsedMs={10} />);
    expect(screen.getByText(/compressed scale/i)).toBeDefined();
  });
});
