# Database

> **Status: design intent — Stage 1 confirms.** Role model, containment strategy, and index plan are decided. The ER diagram, concrete DDL, and query-plan analysis land with the implementation.

PostgreSQL 16+ with the `pgvector` extension.

---

## 1. Two schemas, two purposes

The database holds two distinct things, and keeping them separate matters:

| Schema | Contents | Owner | Accessed by |
|---|---|---|---|
| `public` (or per-dataset) | **Target data** — the tables users ask questions about | dataset owner | `execute_sql`, read-only role |
| `agent_meta` | **Catalog and embeddings** — schema elements, vectors, session state, audit log | app owner | retrieval + agent, app role |

The agent's own metadata must never be reachable from generated SQL. The read-only role is granted `SELECT` on the target schema and **nothing** on `agent_meta`. A question that tries to read the audit log fails at the permission boundary, not at a prompt.

## 2. ER diagram

> **TBD — Stage 1.** Committed to `docs/assets/er-diagram.png`.

Two diagrams are needed:
1. **`agent_meta`** — the project's own tables (below).
2. **Target dataset** — whichever schema is loaded; for the demo this is a Spider/BIRD database. Generated per-dataset rather than hand-drawn.

## 3. Tables (`agent_meta`)

> **TBD — Stage 1** for exact DDL. Planned shape:

### `schema_elements`
One row per table or column in the target schema — the retrieval corpus.

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PK | |
| `dataset` | `text` | Which target database this belongs to |
| `element_type` | `text` | `table` \| `column`, CHECK-constrained |
| `table_name` | `text` | |
| `column_name` | `text` | NULL for table-level rows |
| `data_type` | `text` | |
| `comment` | `text` | From `pg_description` where present |
| `serialized` | `text` | The text actually embedded — see below |
| `embedding` | `vector(384)` | Dimension follows the model; see [../ml/TRAINING.md](../ml/TRAINING.md) |
| `model_version` | `text` | Which retriever produced this vector |
| `updated_at` | `timestamptz` | |

`serialized` is the interesting field: retrieval quality depends on what text gets embedded. Plan is `"{table}.{column} ({type}) — {comment}. Examples: {v1}, {v2}, {v3}"`, so type and representative values contribute to the match, not just the name. Confirmed empirically in Stage 5.

`model_version` is not optional bookkeeping — vectors from the baseline and fine-tuned models are **not comparable**, so mixing them silently corrupts retrieval. Queries filter on it.

### `foreign_keys`
Join paths between tables. Returned alongside retrieval results so the model does not have to guess the join condition.

### `sessions` / `session_turns`
Session memory: question, generated SQL, result metadata (not full result sets), timestamps.

### `query_audit`
Append-only. Every statement that reached `execute_sql`: SQL text, role, duration, row count, truncation flag, outcome, request/trace ID. See [../operations/SECURITY.md](../operations/SECURITY.md).

## 4. Relationships

- `schema_elements` — self-referential via `table_name` (columns belong to tables).
- `foreign_keys` → `schema_elements` on both endpoints.
- `session_turns` → `sessions`, `ON DELETE CASCADE` (deleting a session discards its memory).
- `query_audit` → deliberately **not** foreign-keyed to `sessions`. The audit trail must survive session deletion.

## 5. Indexes

| Table | Index | Purpose |
|---|---|---|
| `schema_elements` | HNSW on `embedding` (`vector_cosine_ops`) | ANN retrieval |
| `schema_elements` | btree on `(dataset, model_version)` | Filter before ANN; keeps vector spaces from mixing |
| `schema_elements` | btree on `(dataset, table_name)` | `table_filter` lookups, profiling |
| `foreign_keys` | btree on `(dataset, from_table)` | Join-path expansion |
| `session_turns` | btree on `(session_id, created_at)` | Session replay |
| `query_audit` | btree on `created_at`, `request_id` | Incident lookup |

**HNSW vs IVFFlat:** HNSW is planned — better recall at a given latency, no training step, and this corpus is small enough (thousands of elements, not millions) that build time and memory are not a concern. Revisit only if a target schema is unexpectedly enormous. Recorded in [DECISIONS.md](DECISIONS.md).

## 6. Constraints

- `element_type` CHECK to `('table','column')`.
- `NOT NULL` on `dataset`, `element_type`, `table_name`, `serialized`, `model_version`.
- `UNIQUE (dataset, table_name, column_name, model_version)` — prevents duplicate embeddings for one element under one model, which would skew retrieval scores.
- `vector(384)` fixes the dimension; a model change with a different dimension is a migration, not a config toggle.

## 7. Read-only role

The outermost containment boundary. It holds even if prompt defences, AST validation, and every other layer fail — which is why it is the layer to get right first.

```sql
-- Design intent; exact DDL lands in Stage 1 migrations.
CREATE ROLE sql_agent_ro NOLOGIN;

REVOKE ALL ON DATABASE analytics FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

GRANT CONNECT ON DATABASE analytics TO sql_agent_ro;
GRANT USAGE  ON SCHEMA public       TO sql_agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO sql_agent_ro;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO sql_agent_ro;

-- agent_meta is NOT granted. Generated SQL cannot reach it.

ALTER ROLE sql_agent_ro SET statement_timeout = '30s';
ALTER ROLE sql_agent_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE sql_agent_ro SET default_transaction_read_only = on;
ALTER ROLE sql_agent_ro SET work_mem = '32MB';

CREATE ROLE sql_agent_login LOGIN PASSWORD :'ro_password' IN ROLE sql_agent_ro;
```

Points that matter:

- **`default_transaction_read_only = on`** is belt-and-braces alongside the missing write grants. Two independent mechanisms, both of which must fail for a write to land.
- **`statement_timeout` is set on the role**, not just per-connection. A caller that forgets to set it still gets the ceiling.
- **`idle_in_transaction_session_timeout`** stops an abandoned SSE stream from pinning a connection with an open transaction.
- **`work_mem`** caps per-sort memory so one query with a large sort cannot pressure the whole instance.
- **Functions are not granted.** Without `EXECUTE`, `pg_read_file` and friends are unreachable regardless of what SQL is generated.
- **`ALTER DEFAULT PRIVILEGES`** means a table added later is readable without re-granting — and, importantly, is the only thing that is automatic. Write access never is.

> Verification is a **test**, not an assertion in a doc. [../development/TESTING.md](../development/TESTING.md) specifies negative tests: the read-only role must fail on `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `COPY ... TO PROGRAM`, and any `agent_meta` read.

## 8. Migrations

Alembic, autogenerate off by default — migrations are written and reviewed by hand because several of them are grant statements Alembic cannot infer.

Rules:
- One logical change per migration; every migration has a working `downgrade()`.
- Role and grant changes live in migrations, not in a README someone runs by hand. Security posture must be reproducible from a clean database.
- Extension creation (`CREATE EXTENSION IF NOT EXISTS vector`) is the first migration.
- Re-embedding after a model change is a **data migration with a new `model_version`**, not an in-place `UPDATE`. Old vectors stay queryable until the new set is verified.

## 9. Query optimization

> **TBD — Stage 6**, with `EXPLAIN ANALYZE` output against real plans.

Planned analysis:

- **ANN recall/latency tradeoff** — HNSW `ef_search` sweep against Recall@k, so the retrieval latency budget in [../operations/PERFORMANCE.md](../operations/PERFORMANCE.md) is a measured choice.
- **Pre-filter vs post-filter** — filtering by `(dataset, model_version)` before the ANN scan changes the plan substantially; measure rather than assume.
- **Connection pool sizing** — `execute_sql` is the contended resource; pool size is what actually bounds concurrent load on the database.
- **The generated-SQL plans themselves** — the `estimated_cost` returned by `validate_sql` gives the agent a bail-out signal before execution. Calibrating the threshold is empirical work, not a guess.
