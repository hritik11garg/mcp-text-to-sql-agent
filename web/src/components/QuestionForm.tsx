import type { FormEvent } from 'react';

/** Mirrors `APISettings.api_max_question_chars`. The server is the authority. */
export const MAX_QUESTION_CHARS = 2000;

/** Below this, the character count is noise. It appears when it starts to matter. */
export const COUNT_VISIBLE_FROM = 0.8;

interface Props {
  readonly question: string;
  readonly explainOnly: boolean;
  readonly busy: boolean;
  readonly onQuestionChange: (value: string) => void;
  readonly onExplainOnlyChange: (value: boolean) => void;
  readonly onAsk: () => void;
  readonly onStop: () => void;
}

/**
 * The one input, set at the largest size on the page.
 *
 * That is the type decision this screen is built around: the question is the
 * only text here a person wrote, and everything else -- stages, durations, SQL,
 * columns, tokens -- is something the system emitted and is set in monospace.
 * Making the human's sentence the display type says what is being measured.
 *
 * `maxLength` is a courtesy, not a control. It stops someone pasting an essay
 * and waiting for a 422, and it is removed from devtools in one click. The
 * bound that counts is `api_max_question_chars`, checked server-side on a
 * request already capped at 64 KiB before parsing.
 */
export function QuestionForm({
  question,
  explainOnly,
  busy,
  onQuestionChange,
  onExplainOnlyChange,
  onAsk,
  onStop,
}: Props): JSX.Element {
  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (!busy && question.trim() !== '') {
      onAsk();
    }
  };

  const remaining = MAX_QUESTION_CHARS - question.length;
  const showCount = question.length >= MAX_QUESTION_CHARS * COUNT_VISIBLE_FROM;

  return (
    <form className="ask" onSubmit={submit}>
      <label className="ask__field">
        <span className="eyebrow">Ask a question about the database</span>
        <textarea
          className="ask__input"
          value={question}
          onChange={(e) => onQuestionChange(e.target.value)}
          maxLength={MAX_QUESTION_CHARS}
          rows={2}
          placeholder="How many singers are there?"
          spellCheck={false}
          disabled={busy}
        />
      </label>

      <div className="ask__controls">
        <label className="ask__toggle">
          <input
            type="checkbox"
            checked={explainOnly}
            onChange={(e) => onExplainOnlyChange(e.target.checked)}
            disabled={busy}
          />
          <span>Explain only</span>
        </label>

        {showCount && (
          <span className="ask__count">{remaining} characters left</span>
        )}

        <span className="ask__spacer" />

        {busy ? (
          <button type="button" className="btn btn--stop" onClick={onStop}>
            Stop
          </button>
        ) : (
          <button type="submit" className="btn" disabled={question.trim() === ''}>
            Ask
          </button>
        )}
      </div>
    </form>
  );
}
