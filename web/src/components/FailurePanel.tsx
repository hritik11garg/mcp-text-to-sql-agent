import type { Failure } from '../state/reducer';

interface Props {
  readonly failure: Failure;
}

/**
 * What went wrong, in the words the server chose.
 *
 * The message is shown verbatim and is *not* elaborated on. The server decides
 * what a caller may read -- `api.errors.published` -- and a validation failure
 * says "the generated query did not pass validation" rather than naming the
 * column, because the detailed form is a schema enumeration oracle on an
 * endpoint with no authentication. A helpful UI that guessed at the detail
 * would be reintroducing exactly what the server withheld.
 *
 * The request id is shown because it is the only handle a person has when
 * asking an operator what happened: the log holds the detail this page does
 * not, and the id is what joins them.
 */
export function FailurePanel({ failure }: Props): JSX.Element {
  return (
    <section role="alert">
      <div className="panel__head">
        <h3 className="panel__title">Failed</h3>
        <span className="panel__aside panel__aside--warn">{failure.code}</span>
      </div>
      <div className="failure">
        <p className="failure__message">{failure.message}</p>
        {failure.requestId !== '' && <p className="failure__id">{failure.requestId}</p>}
      </div>
    </section>
  );
}
