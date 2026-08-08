# HTTP API Reference

> **Status: partly built.** Three endpoints are served and are authoritative here. The rest of this page is design intent — much of it describes the *Stage 4* system, and marking that plainly is the point of the table below rather than letting a reader assume every route responds.

| Endpoint | Status |
|---|---|
| `GET /health` | **Served.** |
| `GET /ready` | **Served.** |
| Error model | **Served** for every response, including 404s and unhandled exceptions |
| `X-Request-Id` | **Served.** Honoured when safe to repeat, replaced when not — see below |
| `POST /v1/query` — **non-streaming** | **Served.** Question in, SQL and rows out |
| `POST /v1/query` with `stream: true` | **Served.** `stage`, `sql`, `rows`, `done`, `error` events |
| `session` / `answer_delta` events | Not built — no session memory, no prose synthesis |
| `session_id` on any request | Not built — the field is **refused**, not ignored |
| `answer` (prose synthesis) | Not built — absent from the response rather than empty |
| `GET /v1/schema/search` | Not built |
| `POST`/`GET`/`DELETE /v1/sessions…` | Not built — session memory is Stage 4 |
| `plan`, `tool_call`, `tool_result` events | Not built — these come from the agent loop, Stage 4 |
| `steps[]` | **Served**, with the phases that exist: `answer`, then `execute` or `validate` |

**Unimplemented fields are refused by name, not accepted and ignored.** `session_id` produces a `400 invalid_request` naming the field. Accepting it silently would be the same defect as a config variable nothing reads — the caller sets it, gets no error, and concludes it took effect. It is the sharper case of the two: ignoring it means a follow-up question is answered *without* the previous turn's context, and the answer comes back plausible rather than obviously wrong.

**`stream` was in that category until the stream existed, and is now a real field.** That is the rule running forwards: a field appears when the behaviour behind it does, so the request shape and the served behaviour never disagree. See [ADR-038](DECISIONS.md#adr-038--the-served-request-accepts-only-fields-that-do-something).

**There is no authentication.** The server refuses to start on any bind address that is not loopback while that remains true. See [../operations/SECURITY.md](../operations/SECURITY.md) §13.1 and [../operations/CONFIG.md](../operations/CONFIG.md) §6.

Base URL: `http://127.0.0.1:8000`
Content type: `application/json` unless noted.
API version prefix: `/v1`

Run it: `python -m api`, or `uvicorn api.app:create_app --factory --reload` while developing.

---

## Conventions

- All timestamps are RFC 3339 UTC.
- All errors share the envelope in [Error model](#error-model).
- `session_id` is optional on every request in the design; **the served endpoint refuses it** until session memory exists (Stage 4).
- **`X-Request-Id`** is honoured when it matches `[A-Za-z0-9._:-]{1,128}`, so a gateway's trace id survives this hop. Anything else — a newline, a control character, an over-long value — is **replaced** with a generated `req_<hex>` rather than rejected, because a 400 on a correlation header would fail requests that were otherwise fine. The value appears on every response, in the header and in the error body. See [../operations/SECURITY.md](../operations/SECURITY.md) §13.6 for why it is not trusted verbatim.

---

## `POST /v1/query`

Ask a natural-language analytical question. This is the primary endpoint.

> **Served, both response shapes.** The request the endpoint actually accepts
> is `question`, `stream`, and `options.{max_rows, timeout_ms, explain_only}`,
> and nothing else — `session_id` is refused by name. The response omits
> `answer`, because prose synthesis is Stage 4 and an empty string would be a
> claim the system cannot back. Everything below describes the finished shape.

### Served request

| Field | Type | Required | Notes |
|---|---|---|---|
| `question` | string | yes | 1–`API_MAX_QUESTION_CHARS` (2000) |
| `options.max_rows` | int | no | May only make the result **smaller**; the server ceiling still applies |
| `options.timeout_ms` | int | no | Same — clamped, never raised |
| `options.explain_only` | bool | no | Generate and validate, do not execute |

Any other field is a `400`. The body is capped at `API_MAX_BODY_BYTES` before parsing (`413 payload_too_large`), and the process answers `API_MAX_CONCURRENT_REQUESTS` questions at once — over that, `429 rate_limited` immediately rather than a queue.

**`explain_only` returns `executed: false`** rather than an empty `rows`. A query that legitimately returns no rows and a query that never ran are different facts with identical shape, and only one of them is worth retrying.

**A validation failure does not name the identifier.** `SQL_VALIDATION_FAILED` carries a fixed message; the offending name and the catalog's nearest match go to the log and the audit trail, correlated by `request_id`. The detailed form is written for an operator holding the schema, and over an unauthenticated endpoint it is a schema-enumeration oracle — submit questions, read which column names come back.

### Request

```json
{
  "question": "What was revenue by region in Q4 2025?",
  "session_id": "sess_01HX...",
  "stream": true,
  "options": {
    "max_rows": 500,
    "timeout_ms": 30000,
    "explain_only": false
  }
}
```

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `question` | string | yes | — | 1–2000 chars |
| `session_id` | string | no | new session | Enables follow-up questions against prior results |
| `stream` | boolean | no | `true` | `false` returns a single JSON response |
| `options.max_rows` | integer | no | from config | Clamped to the server maximum; see [../operations/CONFIG.md](../operations/CONFIG.md) |
| `options.timeout_ms` | integer | no | from config | Clamped to the server maximum |
| `options.explain_only` | boolean | no | `false` | Generate and validate, but do not execute |

### Response — non-streaming (`stream: false`)

`200 OK`

```json
{
  "session_id": "sess_01HX...",
  "answer": "Revenue in Q4 2025 was highest in EMEA at $4.2M, ...",
  "sql": "SELECT c.region, SUM(o.total_amount) ...",
  "columns": ["region", "revenue"],
  "rows": [["EMEA", 4200000.0], ["AMER", 3100000.0]],
  "row_count": 2,
  "truncated": false,
  "steps": [
    {"tool": "schema_search", "duration_ms": 41, "status": "ok"},
    {"tool": "validate_sql",  "duration_ms": 18, "status": "error", "attempt": 1},
    {"tool": "validate_sql",  "duration_ms": 16, "status": "ok",    "attempt": 2},
    {"tool": "execute_sql",   "duration_ms": 233, "status": "ok"}
  ],
  "usage": {"input_tokens": 0, "output_tokens": 0, "tool_calls": 4}
}
```

`truncated` is `true` when the row limit clipped the result set — the client must not present a truncated result as complete.

### Response — streaming (`stream: true`)

`200 OK`, `Content-Type: text/event-stream`, plus `Cache-Control: no-cache` and `X-Accel-Buffering: no` — the last because a buffering reverse proxy holds every event until the response ends, which turns a stream back into a slow non-streaming reply.

**Served events:**

| Event | Payload | Meaning |
|---|---|---|
| `stage` | `{"stage": "retrieve", "status": "ok"}` | A phase completed. `retrieve`, `generate`, then either `execute` or — with `explain_only` — `validate` |
| `sql` | `{"sql": "...", "attempt": 1}` | Candidate SQL generated |
| `rows` | `{"columns": [...], "rows": [...], "truncated": false}` | Result set |
| `done` | `{"row_count": 2, "executed": true, "steps": [...], "usage": {...}}` | Terminal success event |
| `error` | Error envelope, same keys as the JSON error body | Terminal failure event |

**Not served, and not emitted as empty:**

| Event | Waiting on |
|---|---|
| `session` | Session memory (Stage 4). Specified as *"first event; always sent"* — a fabricated id would be the `session_id` defect wearing an event name |
| `plan`, `tool_call`, `tool_result` | The agent loop (Stage 4) |
| `answer_delta` | Prose synthesis (Stage 4) |

`stage` is an addition to the original specification rather than one of its events. It exists because the specified list has nothing to send during retrieval and generation, which is where the time goes — and it earned its place immediately: separating `retrieve` from `generate` is what found [ADR-040](DECISIONS.md#adr-040--startup-opens-the-model-because-naming-it-is-not-loading-it).

**Every phase announces itself, including execution.** Until 2026-08-08 the ordinary path emitted `retrieve` and `generate` only, while `explain_only` also emitted `validate` — so a client watching progress had to infer that execution had finished from the arrival of the `rows` event. That is an inference about the server's control flow, and inferences are what these events exist to make unnecessary. `stage: execute` is now emitted **before** the `rows` it produced. Additive: no existing event changed shape, and a client that ignored unknown stages is unaffected.

**`stage` and `steps[]` do not report the same phases, and this is worth knowing before writing a client.** The stream announces three phases — `retrieve`, `generate`, `execute`. The `steps[]` array on `done` reports two — `answer`, which covers retrieval *and* generation together, and `execute`. They are produced by different mechanisms: `stage` events come from an observer on the answering path, while `steps[]` is the timing wrapper around the two calls the route makes.

**Do not merge them.** There is no correct way to distribute one `answer` duration across two `stage` events, so a client that tries produces a number that is neither measurement. Report them separately if both are needed, and label which is which — [ADR-044](DECISIONS.md#adr-044--two-clocks-both-reported-neither-substituted-for-the-other) is the same decision on the browser side, where the client's own arrival times are a third measurement again.

**Unifying them is deliberately not done yet.** Splitting `steps[]` into `retrieve`/`generate` would change a published response field, and the split is only meaningful because `answer` covers a phase whose cost moves — which is precisely what [ADR-040](DECISIONS.md#adr-040--startup-opens-the-model-because-naming-it-is-not-loading-it) was about. It is recorded here rather than fixed silently.

**Exactly one of `done` or `error` terminates the stream.** A stream that stops without one leaves a client waiting on a socket that will never say anything again, so every exit path — including an unhandled exception — emits a terminal event.

**Comment frames (`: keepalive`) are sent after `API_STREAM_KEEPALIVE_SECONDS` of silence.** They are comments, not events, so a client cannot mistake one for a terminal event. See [CONFIG.md](../operations/CONFIG.md) §6 for why they are a liveness control rather than a cosmetic one.

**A `429` is possible before the stream begins and never after it.** Admission happens synchronously, while the status line still exists — see [ADR-039](DECISIONS.md#adr-039--a-stream-is-admitted-before-it-is-a-stream). A client that receives `200` and `text/event-stream` has been admitted.

Example:

```
event: stage
data: {"stage":"retrieve","status":"ok"}

event: stage
data: {"stage":"generate","status":"ok"}

event: sql
data: {"sql":"SELECT COUNT(*) FROM singer;","attempt":1}

event: stage
data: {"stage":"execute","status":"ok"}

event: rows
data: {"columns":["count"],"rows":[[6]],"truncated":false}

event: done
data: {"row_count":1,"executed":true,"steps":[{"stage":"answer","duration_ms":495.8,"status":"ok"},{"stage":"execute","duration_ms":10.2,"status":"ok"}],"usage":{"input_tokens":501,"output_tokens":43}}
```

**Every payload is JSON, always.** SSE is newline-delimited — a field ends at `\n` and an event ends at `\n\n` — so a raw newline in a payload does not corrupt the frame, it *ends* it, and everything after is parsed as a new event. Generated SQL is routinely multi-line, so this is the ordinary case rather than an attack. JSON encoding is what makes it harmless; see [SECURITY.md](../operations/SECURITY.md) §13.11.

## `POST /v1/sessions`

Create a session explicitly (useful when the client wants an ID before asking anything).

**Response** `201 Created` — `{"session_id": "sess_01HX...", "created_at": "..."}`

## `GET /v1/sessions/{session_id}`

Retrieve session state: prior questions, generated SQL, and result metadata (not full result sets).

**Response** `200 OK` · **Errors** `404 session_not_found`

## `DELETE /v1/sessions/{session_id}`

Discard a session and its memory. **Response** `204 No Content`

## `GET /v1/schema/search`

Direct access to the retrieval step, bypassing the agent. Useful for debugging retrieval quality and for the eval harness.

**Query params:** `q` (required), `k` (default 10, max 50)

```json
{
  "results": [
    {"table": "orders", "column": "total_amount", "type": "numeric",
     "score": 0.82, "comment": "Order total including tax"}
  ]
}
```

## `GET /health` · `GET /ready` — **served**

Distinguishing them matters for orchestrators: a failing `/ready` should stop traffic, a failing `/health` should restart the pod. Conflating them produces the worst behaviour in an outage — a `/health` that checked the database would fail every replica's liveness probe at once and the orchestrator would restart the fleet, adding a cold start, a connection storm and a catalog reload to an incident that was resolving itself.

### `/health`

`200 OK` with exactly `{"status": "ok"}`. **Checks nothing else** — deliberately, and asserted by a test that fails if it issues a query.

Nothing more is reported. No version, no hostname, no uptime: all of it is a fingerprint for someone matching services against a CVE list, and none of it is what a kubelet consumes.

### `/ready`

`200 OK` when every dependency is up:

```json
{"status": "ready", "dependencies": {"database": "up", "database_readonly": "up"}}
```

`503` in the standard error envelope otherwise, with the same map under `details`:

```json
{"error": {"code": "not_ready", "message": "one or more dependencies are unavailable",
           "request_id": "req_...", "details": {"dependencies": {"database": "up",
           "database_readonly": "down"}}}}
```

Each dependency is one of exactly two words, `up` or `down`. **The reason is never reported** — a driver message carries the DSN, the internal hostname and the role name, and this endpoint is unauthenticated because a kubelet cannot hold a credential. The real cause is in the process log under the same `request_id`. See [../operations/SECURITY.md](../operations/SECURITY.md) §13.4.

The verdict is cached for 5 seconds, so the cost of being probed does not scale with how often somebody probes. Before startup completes, `/ready` reports `{"startup": "down"}` and `503` — an unconfigured readiness check must not answer yes, and `all([])` is `True`.

The dependency set will grow with the components that have one. MCP server connectivity and the embedding model are listed in older drafts of this page; neither is probed today, because both are loaded at startup and held in memory, and a probe that cannot fail reports nothing.

---

## Error model

**Served.** Every error uses this envelope, including the ones no route author writes: a 404 for an unknown path, a framework validation failure, and an unhandled exception. Starlette's own default is `{"detail": ...}`, which would be a second shape for a client to parse on exactly the paths its error handling is least likely to have been tested against.

```json
{
  "error": {
    "code": "sql_validation_failed",
    "message": "Column \"revenu\" does not exist on table \"orders\".",
    "details": {"attempts": 3, "last_sql": "SELECT revenu FROM orders"},
    "request_id": "req_01HX..."
  }
}
```

| HTTP | `code` | Cause |
|---|---|---|
| 400 | `invalid_request` | Malformed body, question too long, bad option value |
| 404 | `session_not_found` | Unknown `session_id` |
| 408 | `query_timeout` | Statement timeout hit during execution |
| 422 | `sql_validation_failed` | Retry budget exhausted without valid SQL |
| 422 | `ambiguous_question` | Cannot resolve which schema elements are meant |
| 429 | `rate_limited` | Per-client limit exceeded; `Retry-After` header set |
| 499 | `client_disconnected` | Client closed the SSE stream mid-request |
| 500 | `internal_error` | Unhandled failure; `request_id` is the correlation key |
| 502 | `llm_unavailable` | Upstream model call failed after retries |
| 503 | `database_unavailable` | Cannot reach PostgreSQL |
| 503 | `mcp_server_unavailable` | One or more MCP servers unreachable |
| 503 | `not_ready` | `/ready` — a dependency is unavailable |

Two more are emitted by the framework layer and are in the envelope for the same reason: `404 not_found` for an unrouted path, and `405 method_not_allowed`.

**`message` is either something this project wrote for a caller to read, or a fixed generic string.** There is no third case. Every message in `core.exceptions` was written to help someone fix their query and passes through; anything else — a driver error, a `KeyError`, a dependency's traceback — becomes `"the server could not complete this request"`, identical for every cause so it cannot be used as an oracle, with the real exception logged under the `request_id` the caller was handed.

`details` is omitted rather than null when there is nothing to put in it. Validation failures report the field and the rule that was broken, never the value that was sent: pydantic's own error list carries the offending input verbatim, and reflecting it back puts the request body into the response and from there into anything that records responses.

See [../operations/SECURITY.md](../operations/SECURITY.md) §13.3.

---

## Examples

> **TBD — Stage 1.** Runnable `curl` and Python examples for: a simple aggregate, a follow-up question using `session_id`, a multi-step comparison, a validation-failure-then-recovery trace, and a timeout. Each example here must correspond to a verified entry in [../project/DEMO_SCRIPT.md](../project/DEMO_SCRIPT.md).
