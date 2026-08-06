# Observability

> **Status: TBD — Stage 6** for implementation and dashboards. Instrumentation design below is decided, and span boundaries are wired in Stage 1 rather than retrofitted — adding tracing to an agent loop afterwards means restructuring the loop.
>
> **One piece exists: `request_id`.** Everything below assumes a correlation key, and the API assigns one to every request — including ones that 404 — putting it in the response body, the `X-Request-Id` header, and every log line the request produces. See §2a for the part that is not obvious, which is that the header cannot be trusted verbatim.

The question this has to answer: **when a question produces a wrong or slow answer, where did it go wrong?** With an agent, that is genuinely hard — the failure could be retrieval, generation, validation, execution, or synthesis, and the aggregate latency number tells you none of it.

---

## 1. Tracing

OpenTelemetry, OTLP export. One trace per request, spanning agent → MCP servers → database.

### Span hierarchy

```
query.request                                    (root)
├── agent.plan                                    decompose or not
├── agent.step[0]
│   ├── mcp.call schema_search
│   │   └── db.query  (vector search)
│   ├── llm.generate_sql
│   ├── mcp.call validate_sql        attempt=1  ✗ unknown_identifier
│   ├── llm.generate_sql             (retry)
│   ├── mcp.call validate_sql        attempt=2  ✓
│   └── mcp.call execute_sql
│       └── db.query
├── agent.step[1]                                 multi-step only
└── llm.summarize
```

**The retry loop must be visible as sibling spans**, not collapsed into one. A single `validate_sql` span with a duration hides the fact that it ran three times — and the number of attempts is one of the more informative signals this system produces.

### Span attributes

| Span | Attributes |
|---|---|
| `query.request` | `session_id`, `request_id`, `question_length`, `decomposed`, `total_tool_calls`, `outcome` |
| `agent.plan` | `step_count`, `strategy` |
| `mcp.call` | `server`, `tool`, `attempt`, `is_error`, `error_type`, `server_version` |
| `llm.generate_sql` | `model`, `effort`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `attempt` |
| `db.query` | `duration_ms`, `row_count`, `truncated`, `estimated_cost`, `timed_out` |

**Never on a span:** result values, credentials, or the full connection string. Span attributes are exported to a third-party backend with different retention and access controls than the database.

`server_version` on every `mcp.call` means a trace records exactly which tool contract ran — necessary once contracts start versioning ([../architecture/MCP.md](../architecture/MCP.md) §8).

`cache_read_tokens` is the only reliable way to know prompt caching is working. A silent invalidator produces zero cache reads with no error at all.

## 2. Logging

`structlog`, JSON to stdout, correlated to traces by `trace_id` and `request_id` on every line.

| Level | Use |
|---|---|
| `DEBUG` | Full prompts, retrieved elements, raw tool payloads. **Local only** |
| `INFO` | Request lifecycle, tool calls, generated SQL, outcomes |
| `WARNING` | A validation failure's *detail* — the offending identifier and the catalog's nearest match, which the response deliberately withholds ([SECURITY.md](SECURITY.md) §13.10). Correlated to the caller by `request_id`, so the operator loses nothing the response no longer says |
| `WARNING` | Retry attempts, truncated results, timeouts, degraded MCP servers |
| `ERROR` | Unhandled failures, dependency outages, budget exhaustion |

Rules:
- **Every log line carries `request_id` and `trace_id`.** A log line that cannot be correlated to a trace is nearly useless during an incident.
- **Generated SQL is logged** (`LOG_SQL=true`) — it is the primary debugging artifact.
- **Result values are not** (`LOG_RESULT_VALUES=false`) — see [CONFIG.md](CONFIG.md) §7.
- **Secrets are redacted at the formatter**, not by remembering not to log them.
- **Errors log the structured `error_type`**, not just a message string, so failure modes are countable rather than grep-able.

## 2a. `request_id` — implemented, and not trusted from the wire

A caller may supply `X-Request-Id`, and it is honoured: losing a gateway's trace id at this boundary makes a distributed trace stop exactly where the interesting part starts.

But the value arrives from the network and goes to two places where an unchecked string is dangerous:

**Into the log.** A value containing a newline writes a second log line, and an attacker who chooses that line chooses what an operator reads during an incident — a forged `ERROR authentication bypassed for admin`, or enough fabricated entries to bury the real one. That is CWE-117, and it defeats the first two steps of the incident procedure in [SECURITY.md](SECURITY.md) §15, both of which are "read the record".

**Back out**, in a response header and in the error envelope. CR/LF in a header value is response splitting.

So:

| Rule | Why |
|---|---|
| Allowlist `[A-Za-z0-9._:-]`, 1–128 chars | A denylist of dangerous characters is bypassed by the encoding nobody thought of |
| Anchored `\A`…`\Z`, **not** `^`…`$` | In Python `$` also matches before a trailing newline, so `^[\w]+$` accepts `"abc\n"` — precisely the input this rejects |
| A failing value is **replaced**, not rejected | A `400` on a correlation header would fail requests that were otherwise fine, and would let a prober fingerprint this service |
| Assigned before routing | A request that 404s still gets an id, so a scan is as correlatable as real traffic |

The consequence for anyone reading logs: **the `request_id` in a log line is always safe to trust as a single token**, and is not always the one the client sent. If a client reports an id you cannot find, they sent something unrepeatable.

## 3. Metrics

### Request level

| Metric | Type | Purpose |
|---|---|---|
| `requests_total{outcome}` | counter | Success/failure rate |
| `request_duration_seconds` | histogram | Latency distribution |
| `active_sse_streams` | gauge | Connection pressure |
| `tool_calls_per_request` | histogram | Agent efficiency |

### Agent quality — the interesting ones

| Metric | Type | Why it matters |
|---|---|---|
| `sql_generation_attempts` | histogram | How often first-attempt SQL is correct |
| `invalid_sql_total{stage}` | counter | Which validation stage catches what |
| `self_correction_success_total` | counter | **Whether the retry loop actually recovers, or just burns budget** |
| `retry_budget_exhausted_total` | counter | Unrecoverable generation failures |
| `ambiguity_clarification_total` | counter | Questions the agent could not resolve |

`self_correction_success_total` against `retry_budget_exhausted_total` is the honest measure of the self-correction loop. A high retry rate with a low recovery rate means the loop is expensive theatre — which is exactly the kind of thing an aggregate accuracy number hides.

### Retrieval

| Metric | Type |
|---|---|
| `retrieval_duration_seconds` | histogram |
| `retrieval_results_returned` | histogram |
| `retrieval_empty_total` | counter |

### Execution

| Metric | Type | Purpose |
|---|---|---|
| `db_query_duration_seconds` | histogram | |
| `db_statement_timeout_total` | counter | Queries too expensive to run |
| `db_rows_truncated_total` | counter | Row limit engaging |
| `db_pool_in_use` / `db_pool_size` | gauge | **Saturation — the real capacity signal** |
| `explain_cost_rejected_total` | counter | Bail-outs before execution |

### Cost

| Metric | Type |
|---|---|
| `llm_tokens_total{type,model}` | counter |
| `llm_cache_read_tokens_total` | counter |
| `llm_request_duration_seconds` | histogram |
| `llm_errors_total{type}` | counter |

## 4. Alerts

> **TBD — Stage 6** for thresholds, which need a baseline to be meaningful.

| Alert | Condition | Severity | Why |
|---|---|---|---|
| Error rate elevated | 5xx > 5% over 5 min | page | Service broken |
| Database unreachable | `/ready` failing | page | Total outage |
| Invalid-SQL rate spike | 2× baseline over 15 min | page | Generation, retrieval, or validation regression |
| Retry exhaustion spike | 2× baseline | warn | Quality regression |
| Pool saturated | `in_use / size` > 0.9 for 5 min | warn | About to queue |
| Timeout rate elevated | > baseline | warn | Query cost or data volume shifted |
| Token spend anomaly | > 2× daily average | warn | Runaway loop or abuse |
| Cache read rate collapsed | near zero | warn | **Silent prompt-cache invalidation** |

The invalid-SQL alert is the one that catches genuine quality regressions in production — a prompt change or a model version change can degrade generation without producing a single error response.

## 5. Dashboards

> **TBD — Stage 6.** Three, by audience:

**Service health** — request rate, error rate, latency percentiles, pool saturation, active streams. The on-call view.

**Agent quality** — generation attempts distribution, invalid-SQL by stage, self-correction success vs exhaustion, decomposition rate, clarification rate. The "is it getting better or worse" view.

**Cost** — tokens by model and step, cache hit rate, spend per question, spend over time. The view that stops surprises.

## 6. Performance metrics

Latency targets and measured results: [PERFORMANCE.md](PERFORMANCE.md). Instrumentation here is what produces those numbers, which is why span boundaries are decided in Stage 1 rather than added later.

## 7. What good looks like

> **TBD — Stage 6.** Once a baseline exists, this section records the normal operating range for each metric. Without it, "elevated" has no meaning and every alert threshold is a guess.
