# Tasks

The working checklist. Stage definitions and completion criteria: [ROADMAP.md](ROADMAP.md).

Convention: `[ ]` open · `[x]` done · `[~]` in progress · `[!]` blocked

---

## Stage 0 — Scaffolding ✅

- [x] Define the documentation structure from the PRD
- [x] Choose and pin the Python version (3.12) with recorded evidence
- [x] Verify the full dependency stack resolves to wheels on 3.12
- [x] `requirements.txt` / `requirements-dev.txt` pinned to verified versions
- [x] Documentation structure — 28 documents across `docs/` + root files
- [x] Record the decisions knowable now in DECISIONS.md
- [x] `pyproject.toml` — `requires-python`, ruff, mypy, pytest, coverage config
- [x] `.gitignore` (`.venv`, `.env`, `data/`, `checkpoints/`, `results/`, caches)
- [ ] `git init` + initial commit
- [ ] Source tree skeleton

## Stage 1 — Core loop

### Database
- [x] `docker-compose.yml` — Postgres 16 + pgvector, with a real healthcheck
- [x] Alembic init; migration 001: `CREATE EXTENSION vector`
- [x] Migration: `agent_meta` schema — `schema_elements`, `foreign_keys`, `sessions`, `session_turns`, `query_audit`
- [x] Migration: read-only role, grants, `REVOKE` on functions, role-level timeouts
- [x] **Negative tests: read-only role denied on write/DDL/`pg_read_file`/`agent_meta`** ← gates the stage
- [x] HNSW index on `schema_elements.embedding`
- [ ] Load one target dataset

### Schema ingestion
- [x] Introspect tables, columns, types, comments, foreign keys
- [x] Serialize elements (name + type + comment + sample values)
- [ ] Embed with the baseline model; write with `model_version` — *indexer and `model_version` done; the sentence-transformer adapter is written but has not been run against a downloaded model yet*
- [x] Startup check: vectors exist for the configured retriever — `assert_catalog_ready`. (There is deliberately no `RETRIEVER_MODEL_VERSION` variable: the version comes from the embedder so it cannot drift. See CONFIG.md §5.)

### Retrieval
- [x] `SchemaRetriever` over pgvector ANN
- [x] Return foreign-key edges alongside matched elements
- [x] `table_filter` support
- [ ] Latency check against the < 100 ms budget — *a guard against an accidental full scan is in place and green; the real p95 needs a realistic corpus and lands in Stage 6*

### Generation
- [ ] OpenAI-compatible client adapter, injected not imported (ADR-014) — covers Groq, Gemini, OpenRouter, Ollama, LM Studio via `base_url`
- [ ] `sql_gen` prompt v1 — dialect stated explicitly
- [ ] Stable prompt prefix for caching; verify `cache_read_input_tokens` > 0
- [x] Fake LLM client for tests

### Validation
- [ ] sqlglot parse → AST
- [ ] Single-statement check
- [ ] Read-only node check (`SELECT` / read-only `WITH`)
- [ ] Identifier resolution against the catalog
- [ ] `EXPLAIN` + estimated cost
- [ ] Structured error types with nearest-match suggestions
- [ ] Unit tests for every rejection path

### Execution
- [ ] Read-only connection pool
- [ ] Re-validate independently of the caller
- [ ] AST-level `LIMIT` injection, smaller-wins, clamped to ceiling
- [ ] `SET LOCAL statement_timeout`
- [ ] `truncated` flag returned
- [ ] Audit-log write

### API
- [ ] `Settings` via pydantic-settings; startup validation
- [ ] Startup assertion: the read-only role genuinely cannot write
- [ ] `POST /v1/query` (streaming + non-streaming)
- [ ] SSE event types per API.md
- [ ] `/health` and `/ready`
- [ ] Sanitized error envelope
- [ ] `.env.example`

### Stage 1 close-out
- [ ] End-to-end from clean checkout
- [ ] Fill in SYSTEM_ARCHITECTURE, API, CONFIG, PROMPTS with real content
- [ ] Architecture diagram committed
- [ ] First DEMO_SCRIPT entry verified
- [ ] CHANGELOG v0.1

## Stage 2 — Eval harness

- [ ] Dataset download script with checksum verification
- [ ] SQLite → Postgres conversion
- [ ] **Verify conversion: gold queries return identical results on both**
- [ ] Database-level splits (train / dev / held-out / smoke), committed as a file
- [ ] Result-set comparison with the rules in EVALUATION.md §1.1
- [ ] Recall@k computation
- [ ] Per-question artifact persistence
- [ ] `python -m evals.run` CLI
- [ ] Baseline runs: no-retrieval, retrieval-only, +validation
- [ ] Failure taxonomy with counts; gold errors counted separately
- [ ] BENCHMARKS rows + DATASETS/EVALUATION filled in
- [ ] CHANGELOG v0.2

## Stage 3 — MCP servers + client refactor

- [ ] `schema_search` server
- [ ] `validate_sql` server
- [ ] `execute_sql` server
- [ ] `profile_table` server (with mandatory truncation)
- [ ] Tool descriptions that state **when** to call, not just what
- [ ] Schema-enforced limits (`maximum`, `enum`, `required`) — enforced server-side, not just declared
- [ ] Structured `isError: true` responses, never protocol exceptions
- [ ] stdio + Streamable HTTP transports
- [ ] **All logging to stderr** — stdout is the JSON-RPC channel
- [ ] Agent refactored to MCP client with `tools/list` discovery
- [ ] Contract tests + schema snapshot diffing
- [ ] Degradation behaviour per MCP.md §7
- [ ] Claude Desktop config, verified working
- [ ] **Re-run eval — accuracy unchanged**
- [ ] MCP.md filled in; CHANGELOG v0.3

## Stage 4 — Agent layer

- [ ] **Write the multi-step eval set first** (~30–50 tasks with rubrics)
- [ ] Planner + decomposition
- [ ] Session memory (`sessions`, `session_turns`)
- [ ] Follow-up resolution against prior results
- [ ] Self-correction loop
- [ ] **Error-type-aware retry prompts** — a timeout is not retried verbatim
- [ ] Synthesis step
- [ ] Budget enforcement: tool calls, retries, decomposition steps, wall clock
- [ ] `profile_table`-driven disambiguation
- [ ] Metrics: `self_correction_success_total` vs `retry_budget_exhausted_total`
- [ ] Measure invalid-query rate pre- and post-correction; publish the gap
- [ ] BENCHMARKS rows; CHANGELOG v0.4

## Stage 5 — Fine-tuned schema linker

- [ ] Extract `(question, element)` pairs from gold SQL; resolve aliases
- [ ] Cleaning filters with removal counts recorded
- [ ] **Verify database-level split disjointness**
- [ ] Baseline Recall@1/5/10/20 on held-out
- [ ] Training script (typer CLI, seeded, reproducible)
- [ ] MultipleNegativesRankingLoss training run
- [ ] Hard-negative mining
- [ ] Multiple seeds; report variance
- [ ] Re-embed corpus under new `model_version`
- [ ] Ablations A1–A5
- [ ] **A5: does Recall@k improvement move execution accuracy?**
- [ ] Checkpoint export + storage
- [ ] TRAINING.md filled in; result published either way
- [ ] CHANGELOG v0.5

## Stage 6 — Hardening

- [ ] OpenTelemetry spans per OBSERVABILITY.md §1 — retries as sibling spans
- [ ] structlog with `request_id` / `trace_id` on every line
- [ ] Metrics per OBSERVABILITY.md §3
- [ ] Rate limiting: requests, tokens, concurrent streams
- [ ] Multi-stage Dockerfile (CPU-only torch)
- [ ] Compose: migrations as a one-shot service, healthcheck gating
- [ ] Load tests; saturation behaviour documented
- [ ] Benchmark regression guards
- [ ] Coverage ≥ 85%; security suite 100%
- [ ] `pip-audit` in CI
- [ ] CI pipeline
- [ ] PERFORMANCE.md measured results — **including missed targets**
- [ ] DEPLOYMENT / OBSERVABILITY / TESTING filled in
- [ ] Full DEMO_SCRIPT verified end to end
- [ ] README benchmarks table populated from BENCHMARKS.md
- [ ] Demo GIF + screenshots
- [ ] CHANGELOG v0.6 → v1.0

---

## Cross-cutting

- [ ] DECISIONS.md updated **when each decision is made**, not retroactively
- [ ] TROUBLESHOOTING.md updated when a problem is actually hit
- [ ] BENCHMARKS.md appended per measured run — never edited
- [ ] No number in the README that is not traceable to a BENCHMARKS row
