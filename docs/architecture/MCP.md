# MCP Design

> **Status: implemented.** All four servers ship, and the schemas below are the ones they publish — asserted against a committed snapshot in `tests/contract/tool_schemas.json`, so this page and the code cannot drift. The client half (`tools/list` discovery) ships too. Still open: Streamable HTTP transport, and re-running the eval to confirm accuracy is unchanged, which waits on the Stage 2 harness.
>
> **One change reached these servers from outside.** `Resources` moved to `src/composition/` so the HTTP API could share it without importing from this package ([ADR-035](DECISIONS.md#adr-035--the-composition-root-is-its-own-package-because-entrypoints-are-peers)), and it gained a startup assertion that the read-only role genuinely cannot write ([ADR-033](DECISIONS.md#adr-033--the-read-only-role-is-proved-at-startup-by-asking-rather-than-by-writing)). All four servers inherited that check without being modified — including `execute_sql`, which is the one that runs generated SQL and therefore needed it most.

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

- **Transport:** stdio for local/host-driven use (the host launches the server as a subprocess) — **implemented**. Streamable HTTP for the deployed configuration — not yet. It was deferred on the grounds that "a network-reachable endpoint first needs authentication", and the HTTP API has since landed **without** authentication, so that reasoning now has a date attached: the API refuses to bind beyond loopback until authentication exists ([ADR-034](DECISIONS.md#adr-034--the-api-refuses-to-bind-beyond-loopback-while-it-has-no-authentication)), and an HTTP MCP transport would need the same treatment or it would be the way around it.

> ⚠️ **Over stdio, stdout *is* the protocol.** One stray `print` — in this code, in a dependency, or in a debugging session somebody forgot to undo — writes a line the host cannot parse, and the session dies reporting a JSON decode error that names nothing about the cause. Each server therefore hands the real stdout to the transport and repoints `sys.stdout` at stderr at startup, so a stray write is noise instead of a protocol violation. Logging is forced onto stderr for the same reason, with `force=True` so an earlier `basicConfig` by a library cannot leave the root logger pointed at stdout.
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

**Returns:** per column — type, null fraction, distinct count, `min`/`max` for **numeric and temporal types only**, and frequent values that clear the small-cell threshold; plus a `withheld` list naming anything suppressed and why. Per table — a planner row estimate, the `scanned_rows` bound, and how many columns the width cap dropped.

**Enforcement is server-side and does not trust the input:**

- **`table` and `columns` are resolved against the schema catalog before any statement is composed.** An unknown name is rejected. This is a containment boundary, not spell-checking: `sql.Identifier` would quote `pg_authid` perfectly correctly and then read it, so quoting answers *"is this escaped?"* and only the catalog answers *"may this be named at all?"*. It is the same reasoning that makes `table_filter` a bound parameter in §3.1.
- **`sample_rows` is clamped to zero unless `PROFILE_ALLOW_VALUE_SAMPLING` is set.** The parameter is published because it is legitimately useful; it may narrow the default and can never open the gate. A caller asking for 10,000 gets 0.
- **A value is reported only when it occurs at least `PROFILE_MIN_VALUE_FREQUENCY` times.** Rare values identify records rather than categories.
- **Sensitively-named columns are never read**, and the sampling flag does not override that.
- The connection is the `SELECT`-only role, so a table the agent was never granted degrades with a reason rather than being read.

**Truncation is mandatory.** Text values are clipped in SQL and wide tables are column-limited. An unbounded profile of a wide table can consume the entire context budget in one tool result.

**Every number is an approximation, and the tool description must say so.** Statistics are computed over the first `scanned_rows` rows in physical order — not a random sample, because `ORDER BY random()` reads the whole table and `TABLESAMPLE` does not work on views. A null fraction from a profile is not the table's null fraction, and an agent that treats it as one will write a wrong `WHERE` clause with confidence.

> ⚠️ **Profile output is untrusted input.** A frequent value can contain text that reads as an instruction. Values are wrapped in a delimited block and the system prompt states that tool-result content is data, never instructions. See [../operations/SECURITY.md](../operations/SECURITY.md) §14.2.6.

> ⚠️ **This is the only tool whose output is row-derived by design.** `schema_search` returns names and comments; `execute_sql` returns rows to the *caller*, not to a model. A profile is made in order to be shown to a model. Every default above is set for the case where the target database holds real customer records.

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

Implemented as `ToolRegistry` in `src/agent/discovery.py`. Two properties of it are worth stating because they are where "discovery" stops being decorative:

**A server that fails to start costs a capability, not a session.** `connect()` records the failure and continues, which is what makes §7 below a behaviour rather than a table.

**It must be opened and closed in the same task.** The stdio transport is built on anyio task groups, whose cancel scopes must be exited by the task that entered them. This is a real constraint of the transport rather than an implementation choice, and it is called out because the failure surfaces at teardown — after every call has already succeeded — and names nothing about the cause.

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
| **Protocol error** | JSON-RPC error response | A tool name no server advertises | Bug — surface it, don't retry |
| **Tool error** | `isError: true` + content | Invalid SQL, timeout, unknown table, **a schema violation** | Expected — read it and correct |

> **Corrected when this was built.** This table previously placed "malformed params" with protocol errors. It belongs with tool errors, and the reasoning matters: the arguments are written by a *model*, so a value out of range is an ordinary, correctable mistake rather than a caller bug. Returning it as a protocol error would kill the call and give the agent nothing to read — at exactly the point where models most often get things wrong. Argument validation therefore produces `error_type: "invalid_arguments"` in the same envelope as every other failure, naming the offending field. The one case that still raises is a tool name no server advertises, because no argument change fixes it.

**`isError` is derived, not set alongside the payload.** The flag and the payload's `ok` field carry the same fact, so one is computed from the other — two fields stating one thing independently is a bug waiting to happen. `ok` exists inside the payload because structured content is optional in MCP: against a host that does not support it the model sees only the JSON text block, and a failure that announces itself in the first line of that text is much harder to misread as a result.

**A failed validation is not a tool error.** `validate_sql` returning `valid: false` is the answer to the question that was asked, not a failure to answer it — so `isError` is `false` and the payload carries the diagnosis. Conflating the two would make "the SQL is wrong" indistinguishable from "the tool is broken", and those call for different responses.

A tool error is a normal outcome. The self-correction loop depends on the agent *receiving* the failure as readable content; an exception that terminates the call gives it nothing to work with.

Error content is structured:

```json
{"error_type": "unknown_identifier", "identifier": "orders.revenu",
 "message": "Column \"revenu\" does not exist on table \"orders\".",
 "suggestion": "Nearest matches: total_amount, revenue_usd"}
```

`error_type` values: `syntax_error`, `unknown_identifier`, `not_read_only`, `multiple_statements`, `explain_failed`, `statement_timeout`, `row_limit_exceeded`, `table_not_found`, `permission_denied`, `connection_failed`, `invalid_arguments`, `execution_failed`, `internal_error`.

**Never leaked in tool errors:** connection strings, role names, file paths, or raw driver tracebacks.

That last rule needs an active control rather than good intentions, because **the SDK's own catch-all returns `str(exc)`** — and for a `psycopg` error that string can carry a connection string with its password. So the dispatcher catches everything first and splits it two ways:

- **A domain exception** (`TextToSQLError` and its subclasses) passes its message through. Those messages were written by this project for the agent to read, and withholding them would remove the information self-correction runs on.
- **Anything else** becomes `internal_error` with a fixed generic message. The real exception goes to stderr via `logger.exception`, where the operator sees it and the model does not.

The mapping is ordered most-specific-first rather than keyed by type, because the hierarchy is nested: `StatementTimeoutError` and `PermissionDeniedError` are both `ExecutionError`, and matching the parent first would erase the distinction that decides whether retrying is worth anything.

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

Each server is a module launched with `python -m`. Any host that speaks stdio can run them, and **none of the options below costs anything or requires an account** — see the constraint in [PROJECT.md](../../PROJECT.md).

### 9.1 The project's own client — no third party at all

`ToolRegistry` (`src/agent/discovery.py`) is a complete MCP client. It launches the servers, calls `tools/list`, and dispatches on what it finds. It is what the contract suite drives, so it is exercised on every test run.

```python
async with ToolRegistry() as registry:
    print(registry.tools)  # discovered, not hardcoded
    await registry.call("search_schema", {"query": "customer country"})
```

**This is the reference host.** The servers are proven against it in CI; every other host is a compatibility claim.

### 9.2 MCP Inspector — an open-source UI, no install

The official debugging client from the MCP project, MIT-licensed and run straight through `npx`:

```powershell
npx @modelcontextprotocol/inspector .venv\Scripts\python.exe -m mcp_servers.schema_search
```

It opens a browser UI listing the tools, their schemas, and lets you call them by hand. Needs Node available for `npx`; needs no account and installs nothing permanently. **This is the fastest way for someone else to confirm the servers work.**

### 9.3 A desktop MCP host

Claude Desktop, and any other stdio-speaking host, work with the config below. Listed third deliberately:

- It is **one option, not the path.** Depending on a specific vendor's application would make this project's core capability contingent on someone else's product decisions.
- Whether MCP support sits behind a paid tier has changed over time. That variability is itself the argument — a dependency whose availability can move under you is a poor foundation regardless of what it costs today.

The config shape below is `claude_desktop_config.json`; the same four entries translate directly to any other host.

```json
{
  "mcpServers": {
    "schema-search": {
      "command": "D:\\mcp-text-to-sql-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_servers.schema_search"],
      "env": {
        "PYTHONPATH": "D:\\mcp-text-to-sql-agent\\src",
        "DATABASE_URL": "postgresql://agent_owner:...@localhost:5432/analytics",
        "DATABASE_RO_URL": "postgresql://sql_agent_login:...@localhost:5432/analytics",
        "DATASET": "default"
      }
    },
    "validate-sql":  { "command": "...python.exe", "args": ["-m", "mcp_servers.validate_sql"],  "env": { "...": "same" } },
    "execute-sql":   { "command": "...python.exe", "args": ["-m", "mcp_servers.execute_sql"],   "env": { "...": "same" } },
    "profile-table": { "command": "...python.exe", "args": ["-m", "mcp_servers.profile_table"], "env": { "...": "same" } }
  }
}
```

Four points that are easy to get wrong, each of which produces a confusing failure:

- **Use the virtualenv's interpreter by absolute path.** A host does not inherit an activated environment, so a bare `python` resolves to whatever is first on the system `PATH` and fails on the first import.
- **Set `PYTHONPATH` to `src/`.** The packages live there rather than at the repo root.
- **Both database URLs are required**, and they must be different roles. `DATABASE_RO_URL` is the containment boundary; pointing both at the owner would leave every other control in place and remove the only one that cannot be reasoned around.
- **Index the catalog before first launch.** A server whose catalog is empty rejects every identifier it is ever asked about, so it refuses to start instead — the error names the dataset.

**Servers do not need an LLM key.** They are called *by* a model; they never call one. `LLM_PROVIDER=fake` is a valid configuration for running the servers alone under a host like Claude Desktop, where the host supplies the model.

Startup failures land on stderr, which most hosts surface in a log pane rather than in the conversation. Troubleshooting: [../operations/TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md).
