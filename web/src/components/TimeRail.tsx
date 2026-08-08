import type { Mark, Phase } from '../state/reducer';
import { durationsOf } from '../state/reducer';

/**
 * The signature element: a real time axis, drawn as the answer arrives.
 *
 * Not a stepper. A stepper with four checkmarks says the same thing whether a
 * phase took twenty milliseconds or twenty seconds, and this project exists
 * partly because that distinction was invisible once: retrieval and generation
 * were timed together as one `answer` stage, and the aggregate was equally
 * consistent with a slow provider and with a model checkpoint loading inside
 * the first request. It was the *split* that told them apart. So the interface
 * that reports progress should report the thing whose absence caused that: how
 * long each phase actually took, to scale, without being asked.
 *
 * **The scale is compressed, and the axis says so.** Height is proportional to
 * the square root of the duration. Linear would be honest and unreadable --
 * a 26 ms execution next to a 20 s retrieval is either one invisible pixel or
 * a page nobody can see the end of. Square root keeps a 20-second phase about
 * thirty times taller than a 20-millisecond one instead of a thousand times.
 * Every segment is labelled with its real duration, so the compression cannot
 * be read as a measurement.
 *
 * **These are times this browser observed**, from the request leaving to the
 * event arriving, so they include the network. The server's own per-phase
 * numbers arrive with `done` and are reported separately, in `StepsFooter`.
 * They are two different measurements and neither is a correction of the
 * other; showing one in place of the other would be quietly substituting a
 * number for a different number.
 */

const MIN_SEGMENT_PX = 26;
const MAX_SEGMENT_PX = 190;
const PX_PER_ROOT_MS = 5.5;

/** Compressed, monotonic, and bounded at both ends. */
export function segmentHeight(durationMs: number): number {
  const scaled = Math.sqrt(Math.max(0, durationMs)) * PX_PER_ROOT_MS;
  return Math.round(Math.min(MAX_SEGMENT_PX, Math.max(MIN_SEGMENT_PX, scaled)));
}

/** `1.24 s` past a second, `842 ms` below it. Never more precision than the clock has. */
export function formatMs(ms: number): string {
  if (ms >= 1000) {
    return `${(ms / 1000).toFixed(2)} s`;
  }
  return `${Math.round(ms)} ms`;
}

interface Props {
  readonly marks: readonly Mark[];
  readonly phase: Phase;
  /** Elapsed now, so the open segment grows while nothing is arriving. */
  readonly elapsedMs: number;
}

export function TimeRail({ marks, phase, elapsedMs }: Props): JSX.Element | null {
  if (phase === 'idle') {
    return null;
  }

  const durations = durationsOf(marks);
  const last = marks[marks.length - 1];
  const openFor = Math.max(0, elapsedMs - (last?.at ?? 0));

  return (
    <div className="rail" aria-label="Elapsed time by phase">
      <p className="rail__axis">
        observed <span className="rail__axis-note">— compressed scale</span>
      </p>

      <ol className="rail__marks">
        {marks.map((mark, i) => (
          <li
            key={`${mark.label}-${i}`}
            className={`rail__mark rail__mark--${mark.kind}${mark.failed ? ' is-failed' : ''}`}
            style={{ '--seg': `${segmentHeight(durations[i] ?? 0)}px` } as React.CSSProperties}
          >
            <span className="rail__seg" aria-hidden="true" />
            <span className="rail__tick" aria-hidden="true" />
            <span className="rail__row">
              <span className="rail__label">{mark.label}</span>
              <span className="rail__ms">{formatMs(durations[i] ?? 0)}</span>
            </span>
          </li>
        ))}

        {phase === 'asking' && (
          <li
            className="rail__mark rail__mark--open"
            style={{ '--seg': `${segmentHeight(openFor)}px` } as React.CSSProperties}
          >
            <span className="rail__seg rail__seg--live" aria-hidden="true" />
            <span className="rail__tick rail__tick--open" aria-hidden="true" />
            <span className="rail__row">
              <span className="rail__label">working</span>
              <span className="rail__ms">{formatMs(openFor)}</span>
            </span>
          </li>
        )}
      </ol>
    </div>
  );
}
