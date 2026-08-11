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
- [x] `Settings` via pydantic-settings; startup validation — `APISettings`, with the closed defaults as *validators* rather than defaults: a non-loopback bind and a `*` CORS origin are both startup errors, because there is no authentication yet and a default is not a control
- [x] **Startup assertion: the read-only role genuinely cannot write** — `composition.assert_read_only`. Nothing had ever checked; the only existing check compared the two DSN *strings*, which two spellings of the same superuser pass. Asks PostgreSQL's privilege functions rather than attempting a write, because the case this catches is the one where a probe INSERT would be *accepted*. Runs on first open of the read-only connection, so all five entrypoints inherit it
- [x] `POST /v1/query` (streaming + non-streaming)
- [x] **SSE event types** — the ones that have something behind them: `stage`, `sql`, `rows`, `done`, `error`. `session`, `plan`, `tool_call`, `tool_result` and `answer_delta` are not emitted, because emitting an event with fabricated contents is the accepted-and-ignored field wearing an event name. `stage` is an addition to API.md's list, and it is the one that carries the progress
- [x] `/health` and `/ready` — liveness deliberately checks nothing, so a database blip cannot restart the fleet; readiness reports two fixed words per dependency and never the driver's reason, which carries the DSN
- [x] Sanitized error envelope — four handlers, covering the three paths no route author writes: an unrouted 404, a framework validation failure, and an unhandled exception. A domain message is publishable, everything else is one fixed string
- [x] `.env.example` — committed, and its coverage of CONFIG.md is asserted by `tests/unit/test_settings.py` rather than reviewed. An audit found 18 of 50 settings had reached the code without reaching it, including two security controls whose safe defaults are exactly what made the omission invisible
- [x] **Inject a connection pool** — `PoolConnectionSource` over `psycopg_pool`, every connection proved by `assert_read_only` as the pool opens it. Not a throughput change: `statement_timeout` is transaction-scoped, so two concurrent requests on one connection would run one under the other's limit
- [x] **A global in-flight cap** — `429` immediately rather than queued, shared by both response shapes. For a stream it is taken *before* the response begins, because a `429` is not expressible after a `200` (ADR-039). Per-*caller* admission is the row below and needs authentication
- [x] **Run database work off the event loop** — `asyncio.to_thread` for execution, validation and retrieval. The last was a defect this work found rather than introduced: `QuestionAnswerer.candidate()` was `async` and called a blocking pgvector query inline, harmless for exactly as long as its only caller was the sequential eval harness. See CODE_STYLE.md section 6
- [x] **Request body size cap** — enforced before parsing, as pure ASGI middleware. `Content-Length` is checked first and not trusted; received bytes are counted too, because the request that matters is the one that understates it
- [x] **`question` length bound, concurrency cap, connection pool, blocking work off the event loop** — landed with `POST /v1/query`. See SECURITY.md §13.9 for what is done and what is honestly partial
- [ ] **A *per-client* in-flight cap** — today's cap is process-wide, so one caller can consume the whole allowance. Needs an identity to key on, which needs authentication
- [ ] **Close the `explain_only` timing channel** — the message no longer names identifiers, but a real name still reaches `EXPLAIN` and a determined caller may distinguish it by latency. Also needs authentication
- [x] **Fingerprint the code the harness loads, not the commit** — **done 2026-08-11** ([ADR-046](../architecture/DECISIONS.md#adr-046--the-resume-guard-fingerprints-the-code-that-was-loaded-not-the-commit)). `config_fingerprint` now hashes the source of the answering-path modules, read through each module's own `__file__`, and the commit stays in the manifest as provenance. This closes both holes at once: a documentation commit no longer refuses a resume (days 2 and 3 of the full-split run each needed a detached `git worktree` to work around that), and an editable install importing `src/` from a different checkout is now a different digest rather than a matching one. The module list is checked against the filesystem by `tests/unit/test_code_digest.py`, so it cannot go stale quietly
- [x] **Derive `retriever_model_version` from the catalog** — **done 2026-08-11.** It is now taken from the configured embedder, which is what `SchemaRetriever` binds into its `(dataset, model_version)` predicate and therefore the vector space the run actually reads. `--retriever` survives as an **assertion**: a value disagreeing with the embedder is refused rather than recorded. It previously defaulted to the empty string, so the guard between a baseline run and a fine-tuned one resuming into each other was inert — and would first have been needed at the exact moment it was first useless
- [~] **A `with-validation` run over the full split** — `spider-validation-20260808`, halted on the daily cap at **120 of 921**, resumable. The result it was run for is already in: **the validator rejected 0 of 110 queries and passed both that PostgreSQL then refused** (BENCHMARKS §3.1). Finishing it is confirmation rather than discovery, since the error classes validation catches are all zero across the completed baseline
- [ ] **Verify the prompt cache against a real provider** — a stable prefix is built and asserted, and `cache_read_tokens` is plumbed through `Usage`, but it has never been observed non-zero. Until it is, [BENCHMARKS §6](../ml/BENCHMARKS.md) claims no cache discount and the input-token half of every cost figure is priced at full rate
- [ ] **Decide whether finishing that run is worth a day's budget** — the zero-rejection finding is robust on 110 questions and the categories it depends on are empty across all 921. A completed run buys a firmer count and one more sample of generation noise
- [ ] **Error-feedback self-correction** — Stage 4, and further away than this list previously implied. **No baseline self-corrects.** `with-validation` validates once and drops a failing query; there is no retry and no feedback to the model, so `validation_attempts` is 0 or 1 in every code path and never more. The `Invalid (post)` column in BENCHMARKS §3 cannot be filled by any run that exists today, and the UI has a "self-corrected" state nothing can currently reach
- [ ] **An eval that routes through the MCP servers** — today's 79.9% measures the direct answering path. Stage 3's last checkbox ("accuracy unchanged") has been unmeasurable for want of a baseline; the baseline now exists. The servers are the only place a serialization or limit-clamping difference could hide, because they are the only place the components are reached over a wire
- [x] **Continuous integration** — `.github/workflows/ci.yml`, four jobs on every push and PR. A missing Docker daemon now *fails* in CI rather than skipping, so the security gate cannot report green over tests it never ran; the guard has its own tests
- [x] **`SECURITY_INVARIANTS.md`** — now **eleven** claims, each mapped to the test that proves it, with residuals named per invariant rather than implied. I-11 (a payload cannot forge a second SSE event) was added 2026-08-09: the mechanism predated it, but nothing here claimed it, so nothing would have noticed its removal
- [~] **Property-based tests** — four of the five properties in ENGINEERING_MATRIX §38, **54 tests over two languages**: `hypothesis` for 39 in Python, `fast-check` for 15 in `web/`, 500 examples each in CI on both sides. **Found an unhandled `TokenError` on the first run**: an unterminated string literal — what a generation cut off by an output-token cap produces — crashed validation instead of being refused, and escaped before the executor's rejection audit, so the attempt left no trail. The one not done is named: the read-only privilege property needs a live database per example
- [x] **Indirect prompt injection through `profile_table`** — an instruction in a value common enough to be reported. Found that `profile_max_value_chars` doubles as an injection-payload cap
- [x] **Failure-injection tests** — 38 tests across the three behaviours ENGINEERING_MATRIX §30 named. **One of them was false**: `mcp_servers.schema_search` started cleanly against an unreachable database and exited 0, while its own docstring promised the process would die — it reached for `resources.retriever` inside the handler, and `Resources` connects on first use. Third instance of the lazy-resource shape here. The MCP half runs real subprocesses and needs no Docker; the rest injects failures at the fake seam, because the testcontainers Postgres is session-scoped and stopping it would poison every later test
- [x] **Resolve the `locust` pin** — the test was written *and* the pin removed. `tests/unit/test_concurrency.py` asserts the property deterministically and in-process, so no load generator was needed; dropping the pin dropped fifteen transitive dependencies including Flask, gevent and socketio. **Verified by mutation** — removing the cap, no-op'ing `release()`, and forcing serialisation each turn a subset red, and doing that found two of the tests were weaker than they read
- [ ] **Load and soak tests** — Stage 6. Throughput, saturation against a real database, memory over hours. Needs a running server and a pipeline; the `locust` pin can come back with the file that imports it
- [x] **Fuzz the parsers** — all three targets. The SQL validator (found the `TokenError` crash), the request body (found a reflected field name, SECURITY.md §13.16 — `_fields` stripped pydantic's `input` and then joined the field *path* verbatim, which with `extra="forbid"` is the caller's own text), and the **SSE parser** in `web/`, where the claim is that *where the chunks fall cannot change what comes out*. **Verified by mutation** — seven deliberate breaks to the two `web/` parsers, and the sixth survived the first draft because the generator wrote `data:value` with no space, which no server sends. §37 stays 🟡: three shallow generators are not a campaign
- [ ] **Wire the coverage floor into CI** — `fail_under = 85` sits configured in `pyproject.toml` and nothing runs it
- [ ] **Dependency, secret and container scanning in CI** — `pip-audit`, `npm audit`, and an image scan once §22's Dockerfile exists. The first manual `npm audit`, run 2026-08-10, found five advisories in the dev toolchain (SECURITY.md §10.1) — none reaching a deployed user, none fixable without a major-version bump
- [ ] **Remove eight production pins with no importer** — the four `opentelemetry-*`, `sse-starlette`, `structlog`, `tenacity`, `rich`, plus dev's `datasets` and `accelerate`. Every one justified by a comment, which is the test ADR-014 says a dependency does not pass; found 2026-08-10 by a documentation sweep, written up as SECURITY.md §10.2. Needs a clean-environment install and a full suite behind it, so it is its own slice
- [ ] **A server-side ADR for the hand-rolled SSE encoder** — `src/api/sse.py` was written as a security control and invariant I-11 rests on it, and the decision register never learned about it. ADR-007 still claimed `sse-starlette` did the framing until it was amended on 2026-08-10
- [ ] **Upgrade `vite` 5 → 7 and `vitest` 2 → 3** — the fix for those five advisories, and its own regression surface. Until then the mitigation in force is that the dev server is not required to run anything: `vite build` output is served by FastAPI, and the whole suite runs without it
- [ ] **Branch protection and required status checks** — repository settings rather than a file, and the half that makes a green run mean something
- [ ] **Authentication** — the API has none. `API_HOST` is refused on anything but loopback until it does, which is a containment measure, not a substitute

### Demo UI
- [x] Vite + React + TypeScript app under `web/`, no server-side rendering — 127 tests, `tsc --noEmit` strict with `noUncheckedIndexedAccess`
- [x] `POST /v1/query` over SSE — render each phase as it arrives. **The client frames the stream itself**: `EventSource` is `GET`-only and a question in a URL is logged by every intermediary, so the body is read from `fetch` and parsed by a bounded incremental parser (a chunk boundary is not a line boundary, and an unterminated line is a refusal rather than a truncation)
- [x] Show the **generated SQL**, not just the answer — highlighted by a tokenizer returning React elements, because every highlighter that returns markup would need `dangerouslySetInnerHTML` on text a model wrote from a stranger's question
- [x] Surface `truncated` explicitly — never present a cut result as complete. The browser's own display limit is reported **separately**, since "the database had more rows" and "this page is showing fewer than it received" are different facts
- [x] Served by FastAPI as static files behind `API_STATIC_DIR`, with a CSP; Vite dev server proxies locally. Both keep page and API on one origin, so `API_CORS_ORIGINS` stays empty
- [x] **Every phase drawn against a real time axis** — not on the original list. Added because a single `answer` aggregate once hid a 20-second model load, and a stepper with checkmarks would hide it again
- [~] Surface validation attempts and retries, since the self-correction loop is the point — the UI renders `attempt > 1` as "self-corrected", but **no run has ever produced one**: `retrieval-only` executes no validator. Unverifiable against real data until a `with-validation` run exists
- [x] **Screenshots + GIF for the README** — `docs/assets/demo.gif` plus two stills, recorded 2026-08-08 against `spider_concert_singer`. No overlays and no tooling watermark, so the asset shows the product and nothing else

### Stage 1 close-out
- [ ] End-to-end from clean checkout — the last component blocker is loading a target dataset
- [x] Fill in SYSTEM_ARCHITECTURE, API, CONFIG, PROMPTS with real content
- [ ] Architecture diagram committed — `docs/assets/` now holds the demo GIF and two screenshots, but no diagram
- [x] **First DEMO_SCRIPT entry verified** — re-recorded 2026-08-08 against the current code, in both response shapes and in a browser
- [ ] CHANGELOG v0.1
- [x] Screenshots and a GIF — captured 2026-08-08, embedded in README and DEMO_SCRIPT 1c

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
- [x] **Decide the split question** — **decided 2026-08-11** ([ADR-047](../architecture/DECISIONS.md#adr-047--spiders-own-split-is-the-evaluation-boundary-and-the-training-set-is-carved-around-it)). Spider's official `dev.json` is the reported evaluation set, and the hash split is carved around it. **The audit that settled it found a defect**: 11 of Spider's 20 dev databases hashed into this project's `train` band carrying 605 questions, so a Stage 5 fine-tune would have fitted the retriever to 11 of the 20 schemas it is scored on — visible only as a *better* Recall@k. `assign(reserved=...)` and `leaked_databases()` close it, the split is regenerated, and a test runs the check against the committed assignment
- [x] **Wire the pipeline into the answerer seam** — it was not one line. A benchmark is 20 databases and every component was single-schema, so the seam is a per-database scope (ADR-031); gold SQL must be the statement verification produced rather than one re-derived at run time (ADR-030); and the eval's query runner cannot be the production executor without truncating both sides of a comparison into a false mismatch (ADR-032). Three baselines are wired: `full-schema`, `retrieval-only`, `with-validation`
- [x] **`benchmark.load index`** — builds the schema catalog per converted database, introspecting as the read-only role. Retrieval, identifier resolution and the full-schema prompt all read it; without it every question fails for the same uninformative reason
- [x] **Run it** — Spider dev indexed (20 databases, 519 catalog elements) and answered against. The first runs found five more defects, two worth 30 accuracy points each: a `<think>` block submitted as SQL, and `RETRIEVAL_TOP_K=10` starving a schema that has 10–67 elements
- [~] Baseline runs: no-retrieval, retrieval-only, +validation — `retrieval-only` measured over the **whole split, 921 of 921 questions and 20 of 20 databases** at k=30, single model; `full-schema` and `with-validation` are wired and unrun
- [x] **A full-split run** — **complete 2026-08-08** in `results/spider-full-20260806`: 921 of 921 scoreable questions, 20 of 20 databases, **79.9%**, single model with the fallback chain disabled so a cap could not silently produce a blend. Three days and three daily budgets — 379 → 744 → 921 — resuming into the same directory each time, with days 2 and 3 run from a `git worktree` at the manifest's commit because the fingerprint refused a resume from a moved tree (BENCHMARKS.md §1.3). **Zero infrastructure errors in the final result**: each of days 1 and 2 ended with 12 quota failures, and day 3 re-attempted rather than retired them
- [~] BENCHMARKS rows + DATASETS/EVALUATION filled in — §0 fidelity, §1 execution accuracy, §2 recall and §3 invalid-query rate all carry measured values; every row states what its own sample covers. EVALUATION §6 and the README table restate them instead of contradicting them
- [x] **Make a run's commit trustworthy** — every recorded run named the commit *before* the code that produced it, because all five were made from working trees whose fixes were not yet committed. `current_commit()` now marks `-dirty` and `-unverified`
- [x] **Derive `retriever_model_version` from the catalog instead of a CLI flag** — **done 2026-08-11**; the same item as the Stage 1 entry above, which is worth noting on its own: **one task was open in two stages and would have been closed in one.** Derived from the configured embedder, with `--retriever` demoted to an assertion that fails on disagreement
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
- [x] **Re-run eval — behaviour unchanged** — 2026-08-10: `mcp-retrieval` baseline built, and retrieval over the wire returns **byte-identical** element lists on all 1,034 dev questions at +7.8 ms per call ([BENCHMARKS](../ml/BENCHMARKS.md) §8, [ADR-045](../architecture/DECISIONS.md#adr-045--the-mcp-baseline-scopes-servers-by-process-because-the-tool-contract-has-no-dataset)). Closed as an identity, not an accuracy row — §8.2 says why
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
- [x] **Verify database-level split disjointness** — **done 2026-08-11**, ahead of Stage 5 and for good reason: the check found 11 evaluated databases in the training band the moment it was first run (ADR-047). `leaked_databases()` plus `tests/unit/test_split_disjointness.py`
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
