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
- **SQL validation** — five stages, cheapest first: sqlglot parse, single-statement, read-only, identifier resolution against the catalog with nearest-match suggestions, then `EXPLAIN` with a cost ceiling. Stages 1–4 perform no I/O and are callable on their own, so obviously bad SQL is still rejected when the database is unreachable.
- **Sandboxed execution** — `SQLExecutor` re-validates every query rather than assuming `validate_sql` ran, injects the row limit into the AST (smaller-wins, clamped to a ceiling the caller cannot raise), sets `statement_timeout` per statement, and returns a `truncated` flag that distinguishes a server-imposed cut from the caller's own `LIMIT`. Every attempt is audited over a separate owner connection; result values are never stored.
- **SQL generation** — the OpenAI-compatible adapter (one class for Groq, OpenRouter, Cerebras, Gemini's compatibility endpoint, Ollama and LM Studio, differing by `base_url`), a `sql_gen` prompt that states the dialect and orders itself longest-stable-prefix-first for caching, and a generator that repairs the markdown fences models emit by habit. `CANNOT_ANSWER` is a distinct failure from a bad query, because retrieving more schema helps and retrying generation does not.
- **Provider agility** — `LLM_MODEL_FALLBACKS` gives an ordered model chain that advances on a 429, so a spent per-model daily cap becomes a logged switch rather than a failed run. `python -m generation.check` verifies a provider with one round trip before anything depends on it.
- **Table profiling** — `TableProfiler` returns null fraction, distinct count, extremes and frequent values so the agent can tell two plausible columns apart, or learn that a column stores `'FI'` rather than `'Finland'`. It is the only component whose output is row-derived by design, so every bound on it is a disclosure control: identifiers are resolved against the catalog before any statement is composed, a value must clear a frequency threshold before it may be reported, extremes are returned only for numeric and temporal types, and raw cells require `PROFILE_ALLOW_VALUE_SAMPLING`, which is off and cannot be opened by a caller. Anything withheld is reported as withheld, with the reason. See ADR-016.
- **The four MCP servers**, each an independent process over stdio: `search_schema`, `validate_sql`, `execute_sql`, `profile_table`. Each is a thin adapter over a component that was built and tested without any knowledge of MCP — every bound lives in the component, because another host can connect to one server alone. Published ceilings are imported from the code that clamps them, so what a caller is told and what is enforced cannot drift. Tool descriptions state *when* to call, not just what, and are treated as prompts. See ADR-017 and MCP.md.
- **Runtime tool discovery.** `ToolRegistry` connects to the configured servers, calls `tools/list`, and builds its capability set from the answers — no hardcoded tool list, and a server that fails to start costs a capability rather than the session.
- **Copy-pasteable Claude Desktop configuration** (MCP.md §9), with the four setup mistakes that produce confusing failures.
- **The eval harness** — result-set comparison implementing every rule in EVALUATION.md §1.1, Recall@k over schema elements extracted from the reference SQL, a failure taxonomy that classifies by earliest cause, per-question artifacts, and **resumable runs**. Resumption is the design constraint rather than a convenience: free-tier models cap tokens per model per day, so being stopped partway through a run is routine, and a resume whose configuration differs is refused rather than silently averaged. `python -m evals.run` exists and refuses at the pipeline seam instead of reporting 0% — there is no benchmark loaded yet.
- **Benchmark loading** — `python -m benchmark.load` acquires an archive, converts SQLite to PostgreSQL one schema per database, verifies the conversion, and assigns database-level splits. Every gold query is executed on both engines and compared with the eval harness's *own* comparator, because the question is not whether the two databases are identical but whether the eval will score a correct answer as correct (ADR-022); the command exits 3 when a database fails, so CI cannot pass while reporting the data is wrong. Types are inferred from the data rather than the declaration, since SQLite does not enforce one. Identifiers are folded to lower case so unquoted gold SQL resolves, and an unrepresentable name refuses the database instead of being rewritten into a silent collision (ADR-019). Splits are a hash of the database name, not a seeded shuffle, so adding databases never moves the ones already assigned (ADR-021).
- **Test suite: 782 tests** — 422 unit, 109 integration against a real Postgres via testcontainers, 181 security, 70 contract. The security suite is negative: it passes when the database refuses. The contract suite launches the servers as real subprocesses and talks to them over real stdio.

### Changed
- **`anthropic` is no longer a dependency.** It was pinned for an adapter that is not built, imported by nothing, and serves the one provider in the supported list with no free tier. Removed under the constraint now stated in PROJECT.md: everything required to run, evaluate and demo this project is free and open source. `LLM_PROVIDER=anthropic` still parses and raises at startup with a message saying so.
- **MCP host documentation no longer privileges one application.** The project's own `ToolRegistry` is the reference client — it is what the contract suite drives, so it is exercised on every test run — followed by the open-source MCP Inspector, with desktop hosts third. Depending on a specific vendor's app made a core capability contingent on someone else's product decisions.
- **A React + TypeScript demo UI joined Stage 1's scope.** The project had no surface a reader could see: the MCP servers are a capability, not a demo, and until an API and a UI exist there is nothing to put in a README GIF either.

### Fixed
- **List-valued settings could not be written in a `.env` file.** pydantic-settings JSON-decodes complex fields at the source, before validators run, so `LLM_ALLOWED_HOSTS=a,b` failed with a parse error naming neither the format nor the field's purpose — while CONFIG.md documented them as plain lists. Now comma-separated, via `NoDecode`.
- **A reasoning model that hit the token limit returned an empty string with no error.** `openai/gpt-oss-120b` spends output tokens on reasoning before emitting content — measured at 43 of 45 tokens for a trivial prompt — so too small a budget yields nothing at all. `LLMResponse.truncated` now carries the distinction and the generator names the cause.
- **Filtered ANN searches silently returned fewer results than requested.** The `(dataset, model_version)` predicate is applied *after* the HNSW scan, and with pgvector's default `hnsw.iterative_scan = off` the scan stops once its candidate list is exhausted. Measured at 6 rows for `k=10` over two datasets, and **0 of 10** when the filter correlated with position in vector space — the shape a second dataset or a re-index under a new model produces. Retrieval now sets `relaxed_order` per search, and warns instead of degrading silently on pgvector older than 0.8. See [DATABASE.md](docs/architecture/DATABASE.md) §5.1.
- Embeddings were passed to the driver as a bare `list[float]`, which adapts to `double precision[]`. `INSERT` coerced it to the column type, so the write path worked while any operator expression — every distance query — failed to resolve. Both paths now use pgvector's `Vector` wrapper.
- `schema_elements` uniqueness constraint permitted unlimited duplicate table rows. `column_name` is `NULL` for table elements, and under the default `NULLS DISTINCT` two such rows never conflict, so `ON CONFLICT` never fired and every re-index would have appended another copy of every table. Rebuilt as `UNIQUE NULLS NOT DISTINCT` in migration 003.
- **Float equality in result comparison could not be a tolerance.** `abs(a-b) < 1e-6` is not transitive, and rows are compared as a multiset while columns are matched by content — both require an equivalence relation, or the verdict depends on the order rows arrived in. Implemented as rounding instead (ADR-018), which is transitive and marginally stricter. Also found that `True == 1.0` in Python, so a boolean column would silently match a numeric one unless the value is tagged.
- **`DB_CONNECT_TIMEOUT_MS` was configured but wired to nothing.** libpq's default connect timeout is *none*, so a server pointed at an unreachable host blocked at startup until the OS gave up on the TCP connection — an MCP host would see a subprocess that had started and would never answer. Found by a security test that timed out instead of asserting.
- **`.env.example` documented a JSON list for `SCHEMA_EXTRA_SENSITIVE_COLUMNS`**, which the `NoDecode` fix above had already replaced with comma-separated values. Following the example produced the two-entry denylist `'["internal_ref"'` and `'"account_alias"]'` — patterns that match no real column — so an operator tightening the sensitive-column list got one that silently protected nothing. The primary control is the frequency threshold (ADR-016), not this list, which is why it rates Medium rather than High.
- `.gitignore` negation `!.env.example` was disabled by a trailing comment — `#` only starts a comment at the start of a line — which had silently excluded `.env.example` from the repository.

### Security
- **The benchmark loader is the first untrusted *file* this project handles**, and it runs as the owner role — the widest privilege in the system applied to the least trustworthy input. Analysed in [SECURITY.md](docs/operations/SECURITY.md) §14.2.9. Archives are hashed against a committed lockfile *before* extraction, so a tampered file never reaches the zip parser (ADR-020); extraction refuses absolute paths, `..`, backslash separators, drive letters, symlinks and decompression bombs, validating the whole archive before writing a single byte so a rejection leaves nothing behind. Size caps are enforced against bytes *written*, never against the sizes the archive declares. SQLite sources are opened `mode=ro` with `trusted_schema=OFF`, views and virtual tables are skipped, and pragmas are parameterised via their table-valued forms. Converted schemas are granted `USAGE` + `SELECT` to the read-only role and nothing more, asserted by tests that check it still cannot write or create.
- Retrieval runs on the privileged owner connection by necessity, since the read-only role cannot see `agent_meta` at all. `src/schema/retrieval.py` is therefore held to static SQL and bound parameters only — no dynamic composition — and `table_filter` is bound as a `text[]` rather than built into the statement. Query text and returned elements are deliberately kept out of the search log. Written up as [SECURITY.md](docs/operations/SECURITY.md) §14.2.2.
- Documented and mitigated the exfiltration paths in [SECURITY.md](docs/operations/SECURITY.md) §14: SSRF via a configurable `base_url` (§14.1), and third-party exposure of row values (§14.2). The schema catalog path (§14.2.1) persists sampled values in a store that appears in no audit trail; §14.2.5 enumerates what actually crosses the network boundary and pins it by test. Schema value sampling defaults to **off**.
- **The MCP layer is a transport with its own failure modes**, analysed in [SECURITY.md](docs/operations/SECURITY.md) §14.2.7. Over stdio, stdout *is* the JSON-RPC channel, so each server hands the real stream to the transport and repoints `sys.stdout` at stderr — a stray `print` becomes noise rather than a killed session, and a startup traceback cannot put a connection string into the protocol stream. The MCP SDK's own catch-all returns `str(exc)`, which for a driver error can carry a password; the dispatcher therefore catches every exception first and lets only messages this project wrote reach the model.
- **Profiling is the one path that deliberately sends row-derived values to a model**, and §14.2.6 analyses it in full — including the residual it does not close (a value that is both sensitive and common) and why regex PII redaction was rejected in favour of a frequency threshold (ADR-016): a filter that looks comprehensive changes operator behaviour, and is weakest exactly where people have stopped auditing.

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
