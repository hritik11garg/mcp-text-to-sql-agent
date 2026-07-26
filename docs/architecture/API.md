# HTTP API Reference

> **Status: TBD — Stage 1.** Endpoint shapes below are the design intent. Request/response bodies are confirmed against the implementation when the core loop lands, and this page becomes the authoritative contract.

Base URL: `http://localhost:8000`
Content type: `application/json` unless noted.
API version prefix: `/v1`

---

## Conventions

- All timestamps are RFC 3339 UTC.
- All errors share the envelope in [Error model](#error-model).
- `session_id` is optional on every request; omit it to start a fresh session.
- Requests carry a `X-Request-Id` header if supplied, or one is generated; it appears in logs and traces (see [../operations/OBSERVABILITY.md](../operations/OBSERVABILITY.md)).

---

## `POST /v1/query`

Ask a natural-language analytical question. This is the primary endpoint.

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

`200 OK`, `Content-Type: text/event-stream`.

Event types:

| Event | Payload | Meaning |
|---|---|---|
| `session` | `{"session_id": "..."}` | First event; always sent |
| `plan` | `{"steps": ["...", "..."]}` | Decomposition result (multi-step only) |
| `tool_call` | `{"tool": "...", "input_summary": "..."}` | About to invoke an MCP tool |
| `tool_result` | `{"tool": "...", "status": "ok\|error", "duration_ms": 41}` | Tool returned |
| `sql` | `{"sql": "...", "attempt": 1}` | Candidate SQL generated |
| `rows` | `{"columns": [...], "rows": [...], "truncated": false}` | Result set |
| `answer_delta` | `{"text": "..."}` | Incremental natural-language answer |
| `done` | `{"row_count": 2, "usage": {...}}` | Terminal success event |
| `error` | Error envelope | Terminal failure event |

Exactly one of `done` or `error` terminates the stream.

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

## `GET /health` · `GET /ready`

- `/health` — process liveness. `200` with `{"status": "ok"}`. No dependency checks.
- `/ready` — dependency readiness: database reachable, MCP servers connected, embedding model loaded. `200` or `503` with per-dependency status.

Distinguishing them matters for orchestrators — a failing `/ready` should stop traffic, a failing `/health` should restart the pod.

---

## Error model

Every error uses this envelope:

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

**Error messages never include** raw database error text containing data values, connection strings, or internal paths. See [../operations/SECURITY.md](../operations/SECURITY.md).

---

## Examples

> **TBD — Stage 1.** Runnable `curl` and Python examples for: a simple aggregate, a follow-up question using `session_id`, a multi-step comparison, a validation-failure-then-recovery trace, and a timeout. Each example here must correspond to a verified entry in [../project/DEMO_SCRIPT.md](../project/DEMO_SCRIPT.md).
