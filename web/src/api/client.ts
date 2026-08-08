/**
 * The transport. Everything that needs a network lives here and nowhere else.
 *
 * Three things are worth knowing before reading the code.
 *
 * **The request is same-origin and relative.** `/v1/query`, never a host. In
 * development the Vite proxy forwards it; in production FastAPI serves this
 * bundle itself. Either way the browser makes one same-origin request, so the
 * API's empty CORS allowlist -- its closed default, and the right one while
 * there is no authentication -- never has to be opened for the demo to work.
 *
 * **An HTTP error is not a stream.** The server admits a request *before*
 * returning `200`, precisely so a `429` is still expressible as a status code
 * (`api/query.py`, `_Admission`). So a failure can arrive two ways: as a status
 * with a JSON envelope, or as an `error` event inside a `200` stream. Both are
 * normalised into the same `ErrorEvent` here, because a person watching does
 * not care which layer refused them and a component that had to care would be
 * a component with two error paths to keep in step.
 *
 * **It does not reconnect.** `EventSource` retries automatically, which for
 * this endpoint would mean silently re-asking a question -- another LLM call,
 * another connection, against a service whose in-flight cap is four. A retry
 * here is a person pressing the button again, which is the only actor who
 * knows whether asking twice is what they want.
 */

import type { QueryEvent } from './events';
import { parseEvent } from './events';
import { SseParser, SseProtocolError } from './sse';

export interface AskOptions {
  readonly explainOnly: boolean;
  readonly maxRows: number | null;
  readonly signal: AbortSignal;
}

/** The request body, in the shape `api.schemas.QueryRequest` accepts. */
interface QueryRequestBody {
  question: string;
  stream: boolean;
  options: { explain_only: boolean; max_rows?: number };
}

function body(question: string, options: AskOptions, stream: boolean): QueryRequestBody {
  const payload: QueryRequestBody = {
    question,
    stream,
    // `extra="forbid"` on the server, so a field it does not know is a 400
    // naming the field rather than a silently ignored setting. Nothing
    // speculative may be sent here.
    options: { explain_only: options.explainOnly },
  };
  if (options.maxRows !== null) {
    payload.options.max_rows = options.maxRows;
  }
  return payload;
}

const clientError = (code: string, message: string): QueryEvent => ({
  kind: 'error',
  code,
  message,
  requestId: '',
});

/**
 * Ask a question and yield events as they arrive.
 *
 * Never throws for a server or network failure -- those are yielded as an
 * `error` event, so a caller has exactly one thing to handle. It does propagate
 * `AbortError`-shaped cancellation as a normal end of iteration, because a
 * person who pressed Stop is not looking at an error message.
 */
export async function* ask(
  question: string,
  options: AskOptions,
): AsyncGenerator<QueryEvent, void, void> {
  let response: Response;
  try {
    response = await fetch('/v1/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body(question, options, true)),
      signal: options.signal,
      // No cookies, no credentials. There is nothing to send and sending
      // nothing is what keeps this request unable to act on anyone's behalf.
      credentials: 'omit',
      cache: 'no-store',
    });
  } catch (err) {
    if (isAbort(err)) {
      return;
    }
    yield clientError('network_error', 'could not reach the API. Is it running?');
    return;
  }

  if (!response.ok) {
    yield await envelopeOf(response);
    return;
  }
  if (response.body === null) {
    yield clientError('internal_error', 'the response had no body to read');
    return;
  }

  const reader = response.body.getReader();
  // `stream: true` matters: a multi-byte character can straddle two chunks, and
  // a decoder without it emits a replacement character in the middle of a
  // string the JSON parser then rejects.
  const decoder = new TextDecoder('utf-8');
  const parser = new SseParser();

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      let frames;
      try {
        frames = parser.push(decoder.decode(value, { stream: true }));
      } catch (err) {
        yield clientError(
          'protocol_error',
          err instanceof SseProtocolError ? err.message : 'the event stream was malformed',
        );
        return;
      }
      for (const frame of frames) {
        const event = parseEvent(frame.event, frame.data);
        if (event !== null) {
          yield event;
        }
      }
    }
    if (parser.pending) {
      // The socket closed with a partial event in hand. The spec discards it,
      // and so does this -- but silence would look identical to a finished
      // answer, and it is not one.
      yield clientError('stream_truncated', 'the connection closed before the answer finished');
    }
  } catch (err) {
    if (!isAbort(err)) {
      yield clientError('network_error', 'the connection dropped while the answer was arriving');
    }
  } finally {
    // Releases the lock and, on an early return, cancels the underlying socket
    // so the server's in-flight slot comes back rather than waiting out its
    // keepalive.
    await reader.cancel().catch(() => undefined);
  }
}

/** Read a non-`2xx` response's error envelope, falling back to the status. */
async function envelopeOf(response: Response): Promise<QueryEvent> {
  try {
    const parsed: unknown = await response.json();
    if (typeof parsed === 'object' && parsed !== null && 'error' in parsed) {
      const event = parseEvent('error', JSON.stringify(parsed));
      if (event !== null) {
        return event;
      }
    }
  } catch {
    // Fall through: a body that is not the documented envelope is not worth a
    // second guess at.
  }
  return clientError('http_error', `the API answered ${response.status}`);
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException ? err.name === 'AbortError' : false;
}
