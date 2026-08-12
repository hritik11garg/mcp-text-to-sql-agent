import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { App } from './App';

/**
 * The whole tree, driven by a fake stream.
 *
 * Everything below this file is unit tested -- the framing, the validation, the
 * reducer, the rail's scale. What none of those cover is the wiring: that an
 * event's arrival time reaches the reducer, that the reducer's marks reach the
 * rail, and that aborting actually cancels. Those are the joins, and the joins
 * are where a page that passes every unit test still renders nothing.
 *
 * `fetch` is faked rather than a server started. The transport is one function
 * (`api/client.ts`) and it is the only thing here that needs a network, so
 * replacing it keeps this suite runnable with no Postgres, no model and no
 * provider -- which is what stops it being the suite that gets skipped.
 */

const encoder = new TextEncoder();

/** A `Response` whose body delivers these chunks in order. */
function streamed(chunks: string[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

const WIRE = [
  'event: stage\ndata: {"stage":"retrieve","status":"ok"}\n\n',
  'event: stage\ndata: {"stage":"generate","status":"ok"}\n\n',
  'event: sql\ndata: {"sql":"SELECT count(*) FROM singer;","attempt":1}\n\n',
  'event: stage\ndata: {"stage":"execute","status":"ok"}\n\n',
  'event: rows\ndata: {"columns":["count"],"rows":[[6]],"truncated":false}\n\n',
  'event: done\ndata: {"row_count":1,"executed":true,',
  '"steps":[{"stage":"answer","duration_ms":2533.5,"status":"ok"},',
  '{"stage":"execute","duration_ms":26.2,"status":"ok"}],',
  '"usage":{"input_tokens":501,"output_tokens":43}}\n\n',
];

function ask(question: string): void {
  // By role, not by placeholder. This matched `/how many singers/i` until
  // 2026-08-12 -- a copy of the component's placeholder text, spelled again
  // here. Changing that placeholder (it suggested a question the shipped demo
  // database cannot answer) broke five tests that have nothing to do with
  // placeholder copy, which is the tell: the locator was coupled to
  // presentation rather than to what the element *is*.
  //
  // There is exactly one textbox on this page and it is the question input.
  // Its identity is its role; its placeholder is a hint that may be reworded
  // any time somebody improves the wording.
  fireEvent.change(screen.getByRole('textbox'), { target: { value: question } });
  fireEvent.click(screen.getByRole('button', { name: /^ask$/i }));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('asking a question end to end', () => {
  it('renders the SQL, the rows and every phase on the rail', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamed(WIRE)));
    render(<App />);

    ask('How many singers are there?');

    await waitFor(() => expect(screen.getByText(/SELECT/)).toBeDefined());

    // The question becomes the heading, so a result stays labelled with what it
    // answers even after the input is edited for the next one.
    expect(screen.getByRole('heading', { name: 'How many singers are there?' })).toBeDefined();

    // Every phase the server announced is on the rail, in order. Scoped to the
    // rail on purpose: "execute" is on the page twice, once as a phase this
    // browser observed and once as a step the server timed, and they are
    // deliberately two different measurements of the same phase.
    const rail = within(await screen.findByLabelText(/elapsed time by phase/i));
    await waitFor(() => expect(rail.getByText('execute')).toBeDefined());
    expect(rail.getByText('retrieve')).toBeDefined();
    expect(rail.getByText('generate')).toBeDefined();

    // The rows, and the server's own timings alongside the observed ones.
    expect(screen.getByText('6')).toBeDefined();
    await waitFor(() => expect(screen.getByText('2534 ms')).toBeDefined());
    expect(screen.getByText(/501 in \/ 43 out/)).toBeDefined();
  });

  it('sends the question in the body and never in the URL', async () => {
    // A question in a query string is logged by every intermediary on the path
    // and kept in browser history. It is the reason this client does not use
    // `EventSource`, which can only issue GETs.
    const fetchMock = vi.fn().mockResolvedValue(streamed(WIRE));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);

    ask('How many singers are there?');

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/v1/query');
    expect(url).not.toContain('singers');
    expect(JSON.parse(String(init.body))).toEqual({
      question: 'How many singers are there?',
      stream: true,
      options: { explain_only: false },
    });
    expect(init.credentials).toBe('omit');
  });

  it('shows the failure the server published, and its request id', async () => {
    const wire = [
      'event: stage\ndata: {"stage":"retrieve","status":"ok"}\n\n',
      'event: error\ndata: {"error":{"code":"llm_unavailable",',
      '"message":"the model provider is unavailable","request_id":"req_abc"}}\n\n',
    ];
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamed(wire)));
    render(<App />);

    ask('anything');

    await waitFor(() => expect(screen.getByRole('alert')).toBeDefined());
    expect(screen.getByText('the model provider is unavailable')).toBeDefined();
    expect(screen.getByText('req_abc')).toBeDefined();
    expect(screen.getByText('llm_unavailable')).toBeDefined();
  });

  it('reports an unreachable API rather than hanging', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('failed to fetch')));
    render(<App />);

    ask('anything');

    await waitFor(() => expect(screen.getByText(/could not reach the API/i)).toBeDefined());
  });

  it('offers Stop while a question is in flight, and Ask when it is not', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamed(WIRE)));
    render(<App />);

    expect(screen.queryByRole('button', { name: /stop/i })).toBeNull();
    ask('How many singers are there?');

    await waitFor(() => expect(screen.getByRole('button', { name: /^ask$/i })).toBeDefined());
  });

  it('says what the page will show before anything has been asked', async () => {
    vi.stubGlobal('fetch', vi.fn());
    render(<App />);

    expect(screen.getByText(/what you will see/i)).toBeDefined();
    expect(screen.queryByText('retrieve')).toBeNull();
  });
});
