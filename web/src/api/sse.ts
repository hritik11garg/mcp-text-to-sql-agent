/**
 * An incremental server-sent-events parser.
 *
 * **Why this exists at all.** The browser ships one -- `EventSource` -- and it
 * cannot be used here, because `EventSource` only issues `GET` requests and a
 * question is a `POST` body. Encoding the question into a query string to reach
 * `EventSource` would put user input into a URL, which is the one place it is
 * logged by every intermediary on the path and retained in browser history. So
 * the stream is read from `fetch`, and reading it means framing it.
 *
 * **What makes framing a correctness problem rather than a formality.** A
 * network chunk has no relationship to an event: one `read()` can deliver half
 * a line, three events, or a line terminator whose partner arrives in the next
 * chunk. A parser that assumes chunk boundaries are line boundaries works on
 * every local test and fails against a real network, intermittently, on the
 * long payloads -- which here means exactly the ones carrying rows.
 *
 * **What makes it a security problem.** This parser accumulates whatever
 * arrives until it sees a terminator, and *nothing in the protocol bounds
 * that*. A server that opens `data: ` and never sends a newline makes the tab
 * allocate until it dies. The server on the other end of this connection is
 * this project's own, and that is not the point: the standing rule is that
 * input is not trusted because of where it comes from, and a UI pointed at a
 * host by configuration has no way to know what it is talking to. Both bounds
 * below are refusals rather than truncations -- a truncated event would be
 * handed on as though it were complete, which is worse than an error.
 *
 * Line terminators follow the WHATWG rule: `\r\n`, `\r` and `\n` all end a
 * line. `\r` arriving as the last character of a chunk is *held*, because the
 * `\n` that may complete it is in the next chunk and treating it as a
 * terminator immediately would split one line into two.
 */

/** Largest single line accepted, in UTF-16 code units. */
export const MAX_LINE_CHARS = 1_000_000;

/** Largest accumulated `data:` payload for one event, in UTF-16 code units. */
export const MAX_EVENT_CHARS = 8_000_000;

/** A parsed frame, before its payload is interpreted. */
export interface SseFrame {
  /** The `event:` field, or `message` when the server sent none, per spec. */
  readonly event: string;
  /** The `data:` lines, joined with `\n` in arrival order. */
  readonly data: string;
}

export class SseProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SseProtocolError';
  }
}

/**
 * Feed it chunks, take frames out.
 *
 * Deliberately not tied to `ReadableStream`, `fetch` or the DOM: it is a string
 * in, frames out state machine, so every case below can be tested by calling a
 * method rather than by standing up a server. The transport lives in
 * `client.ts`, which is the part that cannot be tested without one.
 */
export class SseParser {
  #buffer = '';
  #dataLines: string[] = [];
  #dataChars = 0;
  #eventName = '';
  #sawField = false;

  /**
   * Consume a chunk and return every frame it completed.
   *
   * @throws SseProtocolError if a single line or one event's data exceeds its
   *   bound. The parser must not be used afterwards -- the stream's framing is
   *   no longer known to be intact, so anything recovered from it would be a
   *   guess presented as data.
   */
  push(chunk: string): SseFrame[] {
    this.#buffer += chunk;
    const frames: SseFrame[] = [];

    for (;;) {
      const cut = this.#nextLineBreak();
      if (cut === null) {
        break;
      }
      const line = this.#buffer.slice(0, cut.start);
      this.#buffer = this.#buffer.slice(cut.end);

      const frame = this.#consumeLine(line);
      if (frame !== null) {
        frames.push(frame);
      }
    }

    if (this.#buffer.length > MAX_LINE_CHARS) {
      throw new SseProtocolError(
        `event stream sent ${this.#buffer.length} characters with no line break`,
      );
    }
    return frames;
  }

  /**
   * Find the next line terminator, or `null` if the buffer does not hold one.
   *
   * Returns the slice points separately because `\r\n` ends a line one
   * character before the buffer resumes.
   */
  #nextLineBreak(): { start: number; end: number } | null {
    for (let i = 0; i < this.#buffer.length; i += 1) {
      const ch = this.#buffer[i];
      if (ch === '\n') {
        return { start: i, end: i + 1 };
      }
      if (ch === '\r') {
        if (i + 1 === this.#buffer.length) {
          // The `\n` that would pair with this may be in the next chunk. Wait:
          // deciding now would turn one `\r\n` into two line endings, and two
          // line endings is a dispatched event rather than a formatting
          // detail.
          return null;
        }
        return this.#buffer[i + 1] === '\n' ? { start: i, end: i + 2 } : { start: i, end: i + 1 };
      }
    }
    return null;
  }

  /** Apply one line; return a frame if the line dispatched one. */
  #consumeLine(line: string): SseFrame | null {
    if (line === '') {
      return this.#dispatch();
    }
    if (line.startsWith(':')) {
      // A comment. The server's keepalive is one of these, and its only job is
      // to keep an intermediary from closing an idle connection.
      return null;
    }

    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) {
      value = value.slice(1);
    }

    switch (field) {
      case 'event':
        this.#eventName = value;
        this.#sawField = true;
        break;
      case 'data':
        this.#dataChars += value.length + 1;
        if (this.#dataChars > MAX_EVENT_CHARS) {
          throw new SseProtocolError(
            `event stream sent ${this.#dataChars} characters in one event`,
          );
        }
        this.#dataLines.push(value);
        this.#sawField = true;
        break;
      case 'id':
      case 'retry':
        // Accepted and ignored, which is what the spec says to do with fields
        // a client does not use. Neither is meaningful without reconnection,
        // and this client does not reconnect -- see `client.ts`.
        this.#sawField = true;
        break;
      default:
        // Unknown field. Ignored per spec rather than treated as an error: a
        // server adding a field must not break a client that predates it.
        break;
    }
    return null;
  }

  /** End the current event, if a blank line closed one that had fields. */
  #dispatch(): SseFrame | null {
    if (!this.#sawField) {
      // Blank lines between events, or leading blank lines, dispatch nothing.
      return null;
    }
    const frame: SseFrame = {
      event: this.#eventName === '' ? 'message' : this.#eventName,
      data: this.#dataLines.join('\n'),
    };
    this.#eventName = '';
    this.#dataLines = [];
    this.#dataChars = 0;
    this.#sawField = false;
    return frame;
  }

  /**
   * Whether anything is still buffered when the connection ends.
   *
   * A stream that stops mid-event is not an event: the spec discards the
   * incomplete tail, and so does this. The caller uses this to tell "the server
   * finished" from "the socket closed under it", which are different things to
   * report to a person watching.
   */
  get pending(): boolean {
    return this.#buffer.length > 0 || this.#sawField;
  }
}
