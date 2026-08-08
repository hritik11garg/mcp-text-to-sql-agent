import { useCallback, useEffect, useReducer, useRef, useState } from 'react';

import { ask } from './api/client';
import { FailurePanel } from './components/FailurePanel';
import { QuestionForm } from './components/QuestionForm';
import { ResultTable } from './components/ResultTable';
import { SqlPanel } from './components/SqlPanel';
import { StepsFooter } from './components/StepsFooter';
import { TimeRail } from './components/TimeRail';
import { useElapsed } from './useElapsed';
import { initialState, reduce } from './state/reducer';

/**
 * The screen.
 *
 * Two things here are not wiring.
 *
 * **The in-flight `AbortController`.** It does two jobs and the second is the
 * one that matters: aborting the fetch closes the socket, and closing the
 * socket is how the server learns to stop working and hand back the in-flight
 * slot it is holding. The cap is four questions across all callers, so a page
 * that navigated away without aborting would leave a quarter of the service's
 * capacity held by nobody until the keepalive noticed.
 *
 * **The clock.** Every event is stamped with the elapsed time at which *this
 * browser* saw it, measured from the request leaving. The reducer stores that
 * number rather than reading a clock itself, which keeps it a pure function
 * and keeps the rail's positions a measurement rather than an animation.
 */
export function App(): JSX.Element {
  const [state, dispatch] = useReducer(reduce, initialState);
  const [draft, setDraft] = useState('');
  const [explainOnly, setExplainOnly] = useState(false);
  const inFlight = useRef<AbortController | null>(null);
  const startedAt = useRef(0);

  const busy = state.phase === 'asking';
  const elapsed = useElapsed(busy, startedAt);

  const since = useCallback(() => performance.now() - startedAt.current, []);

  useEffect(
    () => () => {
      inFlight.current?.abort();
    },
    [],
  );

  const stop = useCallback(() => {
    inFlight.current?.abort();
    inFlight.current = null;
    dispatch({ type: 'stop', at: since() });
  }, [since]);

  const onAsk = useCallback(async () => {
    const question = draft.trim();
    if (question === '') {
      return;
    }
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;
    startedAt.current = performance.now();

    dispatch({ type: 'ask', question });
    for await (const event of ask(question, {
      explainOnly,
      maxRows: null,
      signal: controller.signal,
    })) {
      if (controller.signal.aborted) {
        // A newer question, or Stop. Events from the previous stream must not
        // land on the new question's state -- that is how a page ends up
        // showing one question's SQL above another question's rows.
        return;
      }
      dispatch({ type: 'event', event, at: since() });
    }
    if (inFlight.current === controller) {
      inFlight.current = null;
    }
  }, [draft, explainOnly, since]);

  return (
    <div className="page">
      <header className="masthead">
        <h1 className="masthead__title">Text-to-SQL Analytics Agent</h1>
        <p className="masthead__note">retrieve · generate · validate · execute</p>
      </header>

      <QuestionForm
        question={draft}
        explainOnly={explainOnly}
        busy={busy}
        onQuestionChange={setDraft}
        onExplainOnlyChange={setExplainOnly}
        onAsk={() => void onAsk()}
        onStop={stop}
      />

      {state.phase === 'idle' ? (
        <section className="blank">
          <p className="eyebrow">What you will see</p>
          <p className="blank__body">
            The SQL the model writes, the rows it returns, and how long each phase took — timed
            down the left as they arrive.
          </p>
        </section>
      ) : (
        <section className="run">
          <TimeRail marks={state.marks} phase={state.phase} elapsedMs={elapsed} />

          <div className="run__body">
            <h2 className="run__question">{state.question}</h2>
            <SqlPanel sql={state.sql} attempts={state.attempts} />
            {state.failure !== null && <FailurePanel failure={state.failure} />}
            <ResultTable state={state} />
            <StepsFooter state={state} />
            {state.phase === 'stopped' && (
              <p className="note">Stopped. Nothing further was requested.</p>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
