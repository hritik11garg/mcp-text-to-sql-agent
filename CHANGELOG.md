# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions map to build stages in [ROADMAP.md](docs/project/ROADMAP.md). Every entry that claims a number links to the [BENCHMARKS.md](docs/ml/BENCHMARKS.md) row it came from.

---

## [Unreleased]

### Added
- Documentation scaffolding: 28 documents across `docs/` plus root-level README, CHANGELOG, CONTRIBUTING, LICENSE.
- `requirements.txt` / `requirements-dev.txt` pinned to versions verified against PyPI for Python 3.12.
- `.python-version` pinning the interpreter to 3.12.
- `pyproject.toml`: ruff, mypy (strict on `src/`), pytest markers, coverage floor.
- **Provider-agnostic LLM access.** `LLMClient` protocol in `src/core/ports/`, with a fake adapter and a factory. One OpenAI-compatible adapter is planned to cover Groq, Gemini, OpenRouter, Ollama and LM Studio via `base_url`. See ADR-014, which supersedes ADR-009.
- **Typed settings validated at startup** (`src/core/settings.py`), including an SSRF guard on `LLM_BASE_URL` that resolves the host and rejects link-local, private, reserved and multicast addresses.
- **PostgreSQL 16 + pgvector**, Alembic migrations, and the `agent_meta` schema: `schema_elements`, `foreign_keys`, `sessions`, `session_turns`, `query_audit`, with an HNSW index for ANN retrieval.
- **The read-only role** — `SELECT`-only, no privileges on `agent_meta`, with role-level `statement_timeout`, `idle_in_transaction_session_timeout`, `work_mem` and `default_transaction_read_only`.
- **The schema catalog** — introspection of tables, columns, types, comments and foreign keys from `pg_catalog`; serialization to the text that gets embedded; an `Embedder` port with a sentence-transformer adapter and a dependency-free hashing adapter; and an idempotent, single-transaction indexer. `assert_catalog_ready` refuses startup when the configured retriever has no vectors indexed.
- **Schema retrieval** — `SchemaRetriever` ranks catalog elements against a question over pgvector ANN, returns the foreign-key edges connecting the tables it matched, and supports `table_filter`. `k` is clamped to the ceiling published in the `schema_search` tool schema, and `RETRIEVAL_TOP_K` / `HNSW_EF_SEARCH` are configurable.
- **Test suite: 181 tests** — 79 unit, 49 integration against a real Postgres via testcontainers, 53 security. The security suite is negative: it passes when the database refuses.

### Fixed
- **Filtered ANN searches silently returned fewer results than requested.** The `(dataset, model_version)` predicate is applied *after* the HNSW scan, and with pgvector's default `hnsw.iterative_scan = off` the scan stops once its candidate list is exhausted. Measured at 6 rows for `k=10` over two datasets, and **0 of 10** when the filter correlated with position in vector space — the shape a second dataset or a re-index under a new model produces. Retrieval now sets `relaxed_order` per search, and warns instead of degrading silently on pgvector older than 0.8. See [DATABASE.md](docs/architecture/DATABASE.md) §5.1.
- Embeddings were passed to the driver as a bare `list[float]`, which adapts to `double precision[]`. `INSERT` coerced it to the column type, so the write path worked while any operator expression — every distance query — failed to resolve. Both paths now use pgvector's `Vector` wrapper.
- `schema_elements` uniqueness constraint permitted unlimited duplicate table rows. `column_name` is `NULL` for table elements, and under the default `NULLS DISTINCT` two such rows never conflict, so `ON CONFLICT` never fired and every re-index would have appended another copy of every table. Rebuilt as `UNIQUE NULLS NOT DISTINCT` in migration 003.
- `.gitignore` negation `!.env.example` was disabled by a trailing comment — `#` only starts a comment at the start of a line — which had silently excluded `.env.example` from the repository.

### Security
- Retrieval runs on the privileged owner connection by necessity, since the read-only role cannot see `agent_meta` at all. `src/schema/retrieval.py` is therefore held to static SQL and bound parameters only — no dynamic composition — and `table_filter` is bound as a `text[]` rather than built into the statement. Query text and returned elements are deliberately kept out of the search log. Written up as [SECURITY.md](docs/operations/SECURITY.md) §14.2.2.
- Documented and mitigated two exfiltration paths in [SECURITY.md](docs/operations/SECURITY.md) §14: SSRF via a configurable `base_url` (§14.1), and third-party exposure of sampled row values (§14.2), including the schema catalog path (§14.2.1), where samples are persisted and re-sent on every retrieval hit and never appear in the audit log. Schema value sampling defaults to **off**.

---

## Planned releases

These are targets, not shipped versions. Each becomes a real entry above when the stage lands.

### v0.1 — Core loop
Schema retrieval, SQL generation, validation, sandboxed execution. Working single-query text-to-SQL against a real database.

### v0.2 — Eval harness
Spider/BIRD subset wired up, execution accuracy measured. Establishes the baseline everything else improves against.

### v0.3 — MCP servers + client refactor
The four capabilities become MCP servers; the agent becomes an MCP client with runtime tool discovery. Runnable from any MCP host.

### v0.4 — Agent layer
Multi-step decomposition, session memory, self-correction on database errors. Multi-step task success metric.

### v0.5 — Fine-tuned schema linker
Contrastive training on question→column pairs. Recall@k before/after ablation committed.

### v0.6 — Hardening
Statement timeouts, row limits, cost caps, OpenTelemetry tracing, test suite. Production-style repo.

### v1.0 — Release
All stages complete, benchmarks committed, demo script verified end to end.
