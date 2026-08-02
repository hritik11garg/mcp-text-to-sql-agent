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
- [x] `git init` + initial commit
- [x] Source tree skeleton

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
- [x] OpenAI-compatible client adapter, injected not imported (ADR-014) — covers Groq, Gemini, OpenRouter, Ollama, LM Studio via `base_url`
- [x] `sql_gen` prompt v1 — dialect stated explicitly
- [~] Stable prompt prefix for caching — *the prefix is stable by construction and asserted in tests; `cache_read_tokens` is plumbed through `Usage` but has never been observed non-zero against a real provider*
- [x] Fake LLM client for tests

### Validation
- [x] sqlglot parse → AST
- [x] Single-statement check
- [x] Read-only node check — full tree walk, not just the root, so data-modifying CTEs and `SELECT ... INTO` are caught
- [x] Identifier resolution against the catalog
- [x] `EXPLAIN` + estimated cost, with a cost ceiling
- [x] Structured error types with nearest-match suggestions
- [x] Unit tests for every rejection path

### Execution
- [ ] Read-only connection pool — *the `ConnectionSource` seam exists and a single-connection adapter implements it; the real pool lands with the API, where a concurrent caller first exists*
- [x] Re-validate independently of the caller
- [x] AST-level `LIMIT` injection, smaller-wins, clamped to ceiling
- [x] `SET LOCAL statement_timeout`
- [x] `truncated` flag returned — distinguishes a server-imposed cut from a caller's own `LIMIT`
- [x] Audit-log write — as the owner, on a separate connection, result values never stored

### Profiling
- [x] `TableProfiler` — null fraction, distinct count, extremes, frequent values
- [x] Identifiers resolved against the catalog **before** any statement is composed
- [x] Small-cell threshold on reported values (ADR-016) — the control that covers a secret in an innocuously-named column
- [x] Extremes restricted to numeric and temporal types — `min(name)` is a cell, not a statistic
- [x] Raw sampling behind `PROFILE_ALLOW_VALUE_SAMPLING`, off by default and not openable by a caller
- [x] Mandatory truncation: value chars in SQL, column count per call, rows scanned per column
- [x] Suppression is reported with a reason, so silence and refusal are distinguishable
- [x] Per-column degradation — one unprofileable column does not fail the profile
- [ ] Wire the profiler into the self-correction loop — *lands with the agent layer, which is what will actually decide a column is ambiguous*

### API
- [ ] `Settings` via pydantic-settings; startup validation
- [ ] Startup assertion: the read-only role genuinely cannot write
- [ ] `POST /v1/query` (streaming + non-streaming)
- [ ] SSE event types per API.md
- [ ] `/health` and `/ready`
- [ ] Sanitized error envelope
- [ ] `.env.example`

### Demo UI
- [ ] Vite + React + TypeScript app under `web/`, no server-side rendering
- [ ] `POST /v1/query` over SSE — render each agent step as it arrives
- [ ] Show the **generated SQL**, not just the answer — it is the thing worth seeing
- [ ] Surface validation attempts and retries, since the self-correction loop is the point
- [ ] Surface `truncated` explicitly — never present a cut result as complete
- [ ] Served by FastAPI as static files in production; Vite dev server locally
- [ ] Screenshots + GIF for the README

### Stage 1 close-out
- [ ] End-to-end from clean checkout
- [ ] Fill in SYSTEM_ARCHITECTURE, API, CONFIG, PROMPTS with real content
- [ ] Architecture diagram committed
- [ ] First DEMO_SCRIPT entry verified
- [ ] CHANGELOG v0.1

## Stage 2 — Eval harness

- [x] Result-set comparison with the rules in EVALUATION.md §1.1 — plus three the rules did not cover (numeric-type unification, number-vs-string, boolean-vs-number), and rounding rather than a tolerance because equality has to be transitive (ADR-018)
- [x] Recall@k computation — gold elements extracted from the reference SQL with alias resolution; unresolvable references counted and reported rather than dropped
- [x] Per-question artifact persistence — including which model actually answered, since a fallback chain can switch mid-run
- [x] Failure taxonomy with counts; gold errors counted separately and excluded from the denominator
- [x] **Resumable runs** — not on the original list, and the requirement that shaped the design. A spent daily token cap costs the questions still outstanding, not the run
- [x] `python -m evals.run` CLI — complete except the pipeline seam, which it refuses at rather than reporting 0%
- [x] Dataset download script with checksum verification — a committed lockfile recorded on first use, because a hardcoded digest nobody has compared to a real download is a fabrication (ADR-020); the archive is hashed *before* extraction, and extraction refuses traversal, symlinks and bombs
- [x] SQLite → Postgres conversion — one schema per database, types inferred from the **data** rather than the declaration, constraints added after the load and skipped-with-a-reason where the data cannot satisfy them
- [x] **Verify conversion: gold queries return identical results on both** — using the eval harness's own comparator, not a stricter one (ADR-022); exits 3 when a database fails, so CI cannot pass while reporting the data is wrong
- [x] Database-level splits (train / dev / held-out / smoke), committed as a file — assigned by hashing the database name, after a test caught that the first implementation's smoke set moved when the corpus grew (ADR-021)
- [x] **Acquire and load a real archive** — Spider `spider_data.zip` pinned at `sha256:00636695…`, 20 dev databases converted, 97.3% of comparable gold results reproduced. Nine defects found that synthetic tests could not, two of them shipped Stage 1 bugs (ADR-023 → ADR-028)
- [x] **Diagnose the 25 verification mismatches** — three causes, one of them real. 3 were SQLite's case-insensitive `LIKE` (transpilation, fixed); 16 were a `LIMIT` cutting a tie (no single correct answer, excluded — ADR-029); 6 are `wta_1.players.birth_date`, a column with no faithful static type. Fidelity 97.3% → **99.3%**, verified databases 10 → **19**
- [ ] **Decide the split question** — this project's hash-based split cuts across Spider's own train/dev boundary, so a score from it is not comparable to published Spider numbers. Either accept that and say so on every row, or adopt Spider's dev set as held-out (DATASETS.md §5)
- [x] **Wire the pipeline into the answerer seam** — it was not one line. A benchmark is 20 databases and every component was single-schema, so the seam is a per-database scope (ADR-031); gold SQL must be the statement verification produced rather than one re-derived at run time (ADR-030); and the eval's query runner cannot be the production executor without truncating both sides of a comparison into a false mismatch (ADR-032). Three baselines are wired: `full-schema`, `retrieval-only`, `with-validation`
- [x] **`benchmark.load index`** — builds the schema catalog per converted database, introspecting as the read-only role. Retrieval, identifier resolution and the full-schema prompt all read it; without it every question fails for the same uninformative reason
- [x] **Run it** — Spider dev indexed (20 databases, 519 catalog elements) and answered against. The first runs found five more defects, two worth 30 accuracy points each: a `<think>` block submitted as SQL, and `RETRIEVAL_TOP_K=10` starving a schema that has 10–67 elements
- [~] Baseline runs: no-retrieval, retrieval-only, +validation — `retrieval-only` measured at k=10 and k=30 over **150 of 921 questions / 3 of 20 databases**; `full-schema` and `with-validation` are wired and unrun
- [ ] **A full-split run** — every number so far is a 3-database smoke sample, and two of the four rows are a blend of two models because the free tier's daily cap moved the fallback chain mid-run
- [~] BENCHMARKS rows + DATASETS/EVALUATION filled in — §0 fidelity, §1 execution accuracy, §2 recall and §3 invalid-query rate all carry measured values; every §1 row states the 3-database bound that stops it being a benchmark result
- [ ] CHANGELOG v0.2

## Stage 3 — MCP servers + client refactor

- [x] `schema_search` server
- [x] `validate_sql` server
- [x] `execute_sql` server
- [x] `profile_table` server (with mandatory truncation)
- [x] Tool descriptions that state **when** to call, not just what
- [x] Schema-enforced limits (`maximum`, `enum`, `required`) — enforced server-side, not just declared, and the published ceilings are *imported from* the components that clamp so the two cannot drift
- [x] Structured `isError: true` responses, never protocol exceptions — including argument validation, which MCP.md had wrongly filed under protocol errors
- [~] stdio + Streamable HTTP transports — *stdio ships; Streamable HTTP lands with the API layer, where a network-reachable endpoint first needs authentication*
- [x] **All logging to stderr** — and stdout is claimed for the protocol at startup, so a stray `print` is noise rather than a corrupted stream
- [x] MCP client with `tools/list` discovery (`ToolRegistry`) — *the agent loop that drives it is Stage 4*
- [x] Contract tests + schema snapshot diffing
- [x] Degradation behaviour per MCP.md §7 — a server that fails to start is recorded and skipped
- [x] Claude Desktop config — MCP.md §9, with the four failure modes that produce confusing errors
- [ ] **Re-run eval — accuracy unchanged** — *blocked on the Stage 2 harness; this is the one Stage 3 gate that cannot be closed out of order*
- [x] MCP.md filled in
- [ ] CHANGELOG v0.3

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
