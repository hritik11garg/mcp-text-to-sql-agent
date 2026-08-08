import type { State } from '../state/reducer';
import { totalMs } from '../state/reducer';

interface Props {
  readonly state: State;
}

/**
 * The server's own timings, which are not the rail's.
 *
 * The rail measures what this browser observed -- request out to event in, so
 * it carries the network. This is what the server measured inside itself. They
 * are two different quantities and neither corrects the other, which is why
 * both are on the page and each says which it is.
 *
 * The stage names differ too, and that is the server's shape rather than a
 * rendering choice: the stream announces `retrieve` and `generate` separately
 * while `steps[]` reports them together as one `answer`. Worth showing rather
 * than hiding, because it was exactly this split that corrected a performance
 * claim in this project's benchmarks -- a single aggregate over retrieval and
 * generation was equally consistent with a slow provider and with a model
 * checkpoint loading on the first request.
 */
export function StepsFooter({ state }: Props): JSX.Element | null {
  if (state.steps.length === 0) {
    return null;
  }
  const total = totalMs(state.steps);

  return (
    <footer className="steps">
      <ul className="steps__list">
        {state.steps.map((step, i) => (
          <li key={`${step.stage}-${i}`} className={`steps__item steps__item--${step.status}`}>
            <span className="steps__stage">{step.stage}</span>
            <span className="steps__ms">{Math.round(step.duration_ms)} ms</span>
          </li>
        ))}
        <li className="steps__item steps__item--total">
          <span className="steps__stage">server total</span>
          <span className="steps__ms">{Math.round(total)} ms</span>
        </li>
      </ul>
      <p className="steps__tokens">
        {state.inputTokens.toLocaleString()} in / {state.outputTokens.toLocaleString()} out
      </p>
    </footer>
  );
}
