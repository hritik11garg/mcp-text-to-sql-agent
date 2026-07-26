# MCP Design

> **Status: contracts are design intent — Stage 3 confirms them.** Tool schemas, error taxonomy, and versioning policy below are decided; exact JSON payloads are validated against the implementation when the MCP refactor lands.

This is the document that decides whether this project reads as "MCP-native" or as "three functions in a protocol wrapper." The substance is in the contracts, not the transport.

---

## 1. Why four servers, not one

Each server is a capability with genuinely different properties. Merging them would force the strictest policy onto every operation.

| Server | Side effects | Retryable | Privilege needed | Failure meaning |
|---|---|---|---|---|
| `schema_search` | None | Freely | Read embeddings | Retrieval miss — widen k |
| `validate_sql` | **None by construction** | **Freely** | `EXPLAIN` only | The SQL is wrong — fix and retry |
| `execute_sql` | Reads real data; consumes DB resources | **No** | `SELECT` on target schema | Query is too expensive, or data is unexpected |
| `profile_table` | None | Freely | `SELECT` + stats | Table unknown or empty |

The `validate_sql` / `execute_sql` split is the central design decision. Validation must be safe to call in a loop — the self-correction path may call it three or four times per question. Execution must not be, because each call costs a real query against a real database under a real timeout. If they were one tool, either validation inherits execution's cost or execution inherits validation's liberal retry policy. Both are wrong.

## 2. Transport and lifecycle

- **Transport:** stdio for local/host-driven use (Claude Desktop launches the server as a subprocess), Streamable HTTP for the deployed configuration.
- **Protocol:** JSON-RPC 2.0 per the MCP specification.
- **Lifecycle:** `initialize` → capability negotiation → `tools/list` → `tools/call`.

The agent never assumes a tool exists. It calls `tools/list` on connect and dispatches on what it finds. A server that goes away degrades the agent's capability rather than crashing it — see [Degradation](#7-degradation).

## 3. Tool contracts

Contract quality is the actual engineering here. Rules applied to every tool below:

1. **The description states when to call the tool, not just what it does.** Descriptions are the only thing the model sees at selection time.
2. **Inputs are constrained in the schema**, not in prose. `enum`, `maximum`, `required` — a limit the schema enforces is a limit; a limit in the description is a suggestion.
3. **Errors return structured content with `isError: true`**, never an exception that kills the call. The agent needs to *read* the failure to correct it.
4. **Outputs carry enough context to act on** — a validation failure returns which identifier was unknown, not just "invalid".

### 3.1 `schema_search`

**When to call it:** before writing any SQL, and again when a first attempt referenced a column that turned out not to exist.

```json
{
  "name": "search_schema",
  "description": "Find the tables and columns relevant to a natural-language question. Call this before writing SQL. Returns ranked schema elements with types and comments. If a generated query referenced an unknown identifier, call this again with a more specific phrasing.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "The question or phrase to find schema elements for"},
      "k": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
      "table_filter": {"type": "array", "items": {"type": "string"},
                       "description": "Restrict results to these tables"}
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```

**Returns:** ranked elements — `table`, `column`, `type`, `comment`, `score`, plus foreign-key edges connecting the returned tables. The FK edges matter: retrieval that returns two tables without the join path between them leaves the model to guess it.

### 3.2 `validate_sql`

**When to call it:** on every generated query, before `execute_sql`, every time. It is cheap and side-effect-free.

```json
{
  "name": "validate_sql",
  "description": "Check that a SQL query parses, is a single read-only SELECT, and references only real tables and columns. Runs EXPLAIN without executing the query, so it is safe to call repeatedly. Call this on every generated query before executing it. Returns the specific identifier or syntax problem when validation fails.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "sql": {"type": "string"},
      "dialect": {"type": "string", "enum": ["postgres"], "default": "postgres"}
    },
    "required": ["sql"],
    "additionalProperties": false
  }
}
```

**Validation stages, in order** (cheapest first, stop at first failure):

1. **Parse** — sqlglot produces an AST, or a syntax error with position.
2. **Single statement** — exactly one top-level statement. Rejects stacked-query attempts.
3. **Read-only** — AST node type must be `SELECT` (or a `WITH` whose body is a `SELECT`). Any DML/DDL node is rejected here, before the database is involved.
4. **Identifier resolution** — every referenced table/column exists in the catalog.
5. **`EXPLAIN`** — the planner accepts it. Returns estimated cost.

**Returns:** `{valid, stage_failed, message, identifier, estimated_cost, plan_summary}`.

Returning `estimated_cost` lets the agent bail out on a query the planner thinks is catastrophic *before* spending the execution budget on it.

### 3.3 `execute_sql`

**When to call it:** only after `validate_sql` returned `valid: true`.

```json
{
  "name": "execute_sql",
  "description": "Execute a validated read-only SELECT against the database and return rows. Runs under a read-only role with a row limit and a statement timeout. Only call this after validate_sql succeeds. A timeout means the query was too expensive — narrow it rather than retrying unchanged.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "sql": {"type": "string"},
      "max_rows": {"type": "integer", "default": 500, "minimum": 1, "maximum": 5000},
      "timeout_ms": {"type": "integer", "default": 30000, "minimum": 100, "maximum": 60000}
    },
    "required": ["sql"],
    "additionalProperties": false
  }
}
```

The description explicitly tells the model what a timeout *means*, because the correct response differs from a syntax error: narrow the query, don't retry it verbatim.

**Enforcement is server-side and does not trust the input:**
- Validation stages 1–4 are re-run here. `execute_sql` never assumes the caller validated first — a separate MCP host could call it directly.
- `LIMIT` is applied at the AST level, clamped to `max_rows`. If the query already has a smaller limit, the smaller wins.
- `SET LOCAL statement_timeout` is applied per transaction.
- The connection uses the `SELECT`-only role. This is the boundary that holds when everything above fails.

**Returns:** `{columns, rows, row_count, truncated, duration_ms}`.

### 3.4 `profile_table`

**When to call it:** when two or more retrieved columns could plausibly answer the question, or when a value-based filter needs to match real data.

```json
{
  "name": "profile_table",
  "description": "Get column statistics and a few sample rows for a table. Call this to disambiguate between similarly-named columns, or to see the actual format of values before writing a WHERE clause against them.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "table": {"type": "string"},
      "columns": {"type": "array", "items": {"type": "string"}},
      "sample_rows": {"type": "integer", "default": 5, "minimum": 0, "maximum": 20}
    },
    "required": ["table"],
    "additionalProperties": false
  }
}
```

**Returns:** per column — type, null fraction, distinct count, min/max for ordered types, and top-k frequent values for low-cardinality columns; plus sampled rows.

**Truncation is mandatory.** Text values are clipped and wide tables are column-limited. An unbounded profile of a wide table can consume the entire context budget in one tool result.

> ⚠️ **Sampled values are untrusted input.** A row value can contain text that reads as an instruction. Sample rows are wrapped in a delimited block and the system prompt states that tool-result content is data, never instructions. See [../operations/SECURITY.md](../operations/SECURITY.md).

## 4. Discovery

```
client                          server
  │── initialize ──────────────▶│
  │◀─ capabilities ─────────────│
  │── tools/list ──────────────▶│
  │◀─ [tool definitions] ───────│
  │── tools/call ──────────────▶│
  │◀─ content blocks ───────────│
```

The agent builds its available-tool set from these responses at connect time. Consequences:

- Adding a fifth capability needs no agent change.
- Tool descriptions are the model's only selection signal, so they are versioned and treated as prompts — see [../ml/PROMPTS.md](../ml/PROMPTS.md).
- If `tools/list` returns a tool the agent has no policy for, it is still callable. The agent does not filter to a hardcoded allowlist; that would defeat the point.

## 5. Request / response format

Standard MCP `tools/call`:

```json
{
  "jsonrpc": "2.0", "id": 3, "method": "tools/call",
  "params": {"name": "validate_sql", "arguments": {"sql": "SELECT 1"}}
}
```

Successful result:

```json
{
  "jsonrpc": "2.0", "id": 3,
  "result": {"content": [{"type": "text", "text": "{\"valid\": true, \"estimated_cost\": 1.05}"}],
             "isError": false}
}
```

Tool payloads are JSON-encoded inside a text content block. Structured output support is used where the host advertises it; the JSON-in-text form is the fallback that works everywhere.

## 6. Error handling

Two distinct failure classes, deliberately not conflated:

| Class | Mechanism | Example | Agent response |
|---|---|---|---|
| **Protocol error** | JSON-RPC error response | Unknown tool, malformed params | Bug — surface it, don't retry |
| **Tool error** | `isError: true` + content | Invalid SQL, timeout, unknown table | Expected — read it and correct |

A tool error is a normal outcome. The self-correction loop depends on the agent *receiving* the failure as readable content; an exception that terminates the call gives it nothing to work with.

Error content is structured:

```json
{"error_type": "unknown_identifier", "identifier": "orders.revenu",
 "message": "Column \"revenu\" does not exist on table \"orders\".",
 "suggestion": "Nearest matches: total_amount, revenue_usd"}
```

`error_type` values: `syntax_error`, `unknown_identifier`, `not_read_only`, `multiple_statements`, `explain_failed`, `statement_timeout`, `row_limit_exceeded`, `table_not_found`, `permission_denied`, `connection_failed`.

**Never leaked in tool errors:** connection strings, role names, file paths, or raw driver tracebacks.

## 7. Degradation

| Server down | Effect |
|---|---|
| `schema_search` | Fatal for a new question; a session with cached schema context can still run follow-ups |
| `validate_sql` | Agent must refuse to execute — no unvalidated query reaches `execute_sql` |
| `execute_sql` | `explain_only` mode still works: generate and validate, return the SQL without results |
| `profile_table` | Degraded disambiguation; the agent asks a clarifying question instead |

## 8. Versioning

- Tool **names** are stable. A breaking change ships as a new name (`search_schema_v2`), never as a silent shape change.
- **Additive changes** — a new optional input field, a new output field — do not bump anything.
- **Breaking changes** — removing a field, changing a type, tightening an enum — require a new tool name; the old one stays for at least one minor release with a deprecation note in its description.
- Each server reports its own version in `initialize`; the agent logs it on every span so a trace records exactly which contract ran.
- Tool description changes are treated as **prompt changes**: versioned in [../ml/PROMPTS.md](../ml/PROMPTS.md) and re-evaluated, because they change model behaviour as much as any system prompt edit.

## 9. Host configuration

> **TBD — Stage 3.** The Claude Desktop `claude_desktop_config.json` block, plus the equivalent for a generic MCP host, land with the refactor. This is the section that makes the project *runnable by other people*, so it gets copy-pasteable config and a troubleshooting cross-reference to [../operations/TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md).
