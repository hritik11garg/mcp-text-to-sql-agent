import { useEffect, useState } from 'react';
import type { MutableRefObject } from 'react';

/** How often the open segment on the rail advances, in milliseconds. */
export const TICK_MS = 250;

/**
 * Elapsed milliseconds since `startedAt`, while `running`.
 *
 * The rail's finished segments are positioned by event arrival times, which are
 * exact. The *open* segment has no end yet, and without a ticking clock it
 * would sit at zero height for however long the phase takes -- which on a cold
 * retrieval is twenty seconds of a page that looks like it has stopped. That is
 * the failure this whole screen exists to avoid, so the open segment grows.
 *
 * Four times a second, not every frame. The segment is a compressed square-root
 * scale, so at one second in it grows by about a tenth of a pixel per
 * millisecond; a 60 Hz redraw of a small tree would be spending fifteen times
 * the work to render a difference nobody can see. It stops when the stream
 * settles, so an idle page costs nothing at all.
 */
export function useElapsed(running: boolean, startedAt: MutableRefObject<number>): number {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!running) {
      return;
    }
    // Set once immediately so the first paint after `ask` is not a frame at
    // zero, then on an interval.
    setElapsed(performance.now() - startedAt.current);
    const id = window.setInterval(() => {
      setElapsed(performance.now() - startedAt.current);
    }, TICK_MS);
    return () => window.clearInterval(id);
  }, [running, startedAt]);

  return elapsed;
}
