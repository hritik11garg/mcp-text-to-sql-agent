# Roadmap

Six stages over 5–6 weeks. **Every stage produces something demoable**, so a bad week costs a stage rather than the project.

Working checklist: [TASKS.md](TASKS.md).

---

## Progress

Percentages are checkbox counts from [TASKS.md](TASKS.md), not confidence — a half-done item counts half. **Stages have not been built in order**, and the two that jumped the queue each carry an explicit cost recorded in their section below.

| Stage | Output | Status | % |
|---|---|---|---|
| 0 | Scaffolding — docs, deps, interpreter pin | ✅ Done | 100% |
| 1 | **Core loop** — retrieval, generation, validation, execution, profiling, API, demo UI | 🚧 In progress | 71% |
| 2 | **Eval harness** — comparison, Recall@k, artifacts, resumption, benchmark loading, pipeline seam | 🚧 In progress | 82% |
| 3 | **MCP servers + client refactor** | 🚧 In progress | 91% |
| 4 | **Agent layer** — decomposition, session memory, self-correction | ⬜ Not started | 0% |
| 5 | **Fine-tuned schema linker** | ⬜ Not started | 0% |
| 6 | **Hardening** — limits, tracing, tests | ⬜ Not started | 0% |

**Two things that had blocked this roadmap for weeks are done.** The full-split run finished on 2026-08-08 — all 921 scoreable questions, all 20 databases, 79.9% — and the demo UI is built, served and verified in a browser. Neither is a blocker any more.

**What is genuinely blocking now, in order:** the **agent loop** (Stage 4), which is what makes the four MCP servers a system rather than four callable capabilities. Behind it, one measurement gap that remains cheap: a **`with-validation` run** over the same split, which measures the validation tier as a *gate*, not self-correction, because no baseline self-corrects. The MCP gap closed on 2026-08-10 — the retrieval hop is measured identical over 1,034 questions ([BENCHMARKS](../ml/BENCHMARKS.md) §8).

**Stage 1 went 75% → 59% → 69% → 88% → 71%.** It dropped once when the demo UI entered its scope, climbed as the pool, the endpoint, streaming and the UI landed — and **dropped again on 2026-08-10, without a single item being un-done.**

> **Recounted 2026-08-10, and the drop is the interesting part.** These percentages are checkbox counts, and 88% could not be reproduced from any count in the repository: Stage 1's rows in TASKS.md give **71%** (63 done, 4 partial, 24 open), and this page's own *Done when* list gives 67%. Stage 2 read 87% and counts 82%. Stage 3's 84% reproduced exactly, which is what showed the other two had drifted rather than the method being wrong.
>
> **Stage 3 is now 91%**, recounted the same day for the opposite reason: its last blocking gate closed. 13 done, 1 partial, 2 open became 14 done, 1 partial, 1 open when the MCP eval was measured. The recount is published with the change that caused it, which is the discipline the paragraph above exists to enforce — a percentage nobody recomputes drifts upward just as easily as it drifts down.
>
> **Nothing regressed. The denominator grew.** Every slice since 2026-08-08 closed items *and* wrote down gaps it had found — failure injection, property tests, the audit finding, the toolchain upgrade — and a checklist that gets more honest as work proceeds is a checklist whose percentage falls while the project improves. **A number that only moves up is a number nobody is recounting**, which is precisely how 88% survived four rounds of new open items.

What remains in Stage 1 is close-out work — screenshots, an architecture diagram, an end-to-end run from a clean checkout — plus the named gaps each recent slice added. Not components.

**Stage 1 is now "the core loop works end to end, and you can watch it".** Every component is built and tested, Spider's dev split is in the database converted and verified, the pipeline is connected to the harness, and it has produced a complete measured result. `POST /v1/query` answers from outside the harness in both response shapes. And the half a reader can **see** exists: a page that asks a question and draws retrieval, generation and execution against a real time axis as the events arrive. The distinction this project keeps insisting on — wired, then measured, then served, then *seen* — is closed for the single-query path.

> **What "seen" cost, and it is worth recording.** Building the UI immediately exposed an asymmetry in the stream nobody had noticed: `explain_only` announced a `validate` stage, but the ordinary path announced nothing for execution, so a client had to infer that phase from the arrival of a data event. Fixed by emitting it — the same accepted-and-ignored shape [ADR-038](../architecture/DECISIONS.md) is about, found by the first consumer that actually had to render progress.

---

## Stage 0 — Scaffolding ✅

Documentation structure, dependency set verified against PyPI for Python 3.12, interpreter pinned.

**Done when:** all 28 documents exist with real structure, `pip install -r requirements.txt` resolves cleanly, design decisions that are knowable now are recorded in [DECISIONS.md](../architecture/DECISIONS.md).

## Stage 1 — Core loop

**Output: working single-query text-to-SQL against a real database.**

The first demoable thing. Ask a question in English, get an answer from Postgres.

Scope: Postgres + pgvector up with roles and migrations; schema ingestion and embedding; baseline retrieval; SQL generation; sqlglot AST validation + `EXPLAIN`; sandboxed execution under limits; FastAPI + SSE; **a React demo UI that consumes the stream**.

**Demo:** a question over a loaded schema returns a correct answer, with progress streaming.

**Done when:**
- [ ] Runs end to end from a clean checkout per the README
- [x] **The read-only negative test suite is green** — this gates the stage, not Stage 6
- [x] Row limits and statement timeouts are enforced and tested — at the role level *and* per request, the latter injected into the AST rather than requested in the prompt
- [x] **`.env.example` and [CONFIG.md](../operations/CONFIG.md) match the implementation** — and it is now *asserted* rather than reviewed. `tests/unit/test_settings.py` enumerates every field on every settings class and fails if one is missing from either file. Added after an audit found 18 of 50 settings had reached the code without reaching the template
- [ ] Span boundaries are in place (instrumentation added later, structure now)
- [x] **A browser can ask a question and watch it being answered.** Verified in Chrome against a real Spider schema on 2026-08-08: question in, generated SQL and rows out, with `stage`/`sql`/`rows`/`done` events rendered as they arrive and each phase drawn against a real time axis. Added after noticing the project had no surface a reader could see — the MCP servers are a capability, not a demo. The GIF and screenshots are close-out work below

**Landed so far:** Postgres + pgvector with migrations and the read-only role (30 negative tests, green); the `LLMClient` and `Embedder` ports; typed settings with an SSRF guard; the schema catalog — introspection, serialization, embedding, and an idempotent indexer; retrieval — ANN over pgvector with `table_filter`, join-path expansion and clamped limits; five-stage SQL validation with structured rejections and nearest-match suggestions; sandboxed execution with AST-level row limits, per-statement timeouts and an audit trail on a separate owner connection; SQL generation behind one OpenAI-compatible adapter with a model fallback chain; and table profiling under an explicit disclosure budget.

**The API's foundation landed:** `create_app()`, `/health`, `/ready`, the sanitized error envelope, request correlation, and the startup sequence — including the assertion that **proves** the read-only role cannot write rather than trusting `DATABASE_RO_URL` to name one ([ADR-033](../architecture/DECISIONS.md#adr-033--the-read-only-role-is-proved-at-startup-by-asking-rather-than-by-writing)). That was a finding, not a feature: nothing had ever checked, and the thirty negative tests gating this stage build their own role and never look at the one a deployment connects as.

**The demo UI landed on 2026-08-08.** Vite + React + TypeScript under `web/`, consuming `POST /v1/query` over SSE. Three parts of it are load-bearing rather than presentational. It **frames the stream itself** — `EventSource` only issues `GET`, and putting a question in a URL would log it at every intermediary, so the client reads `fetch`'s body and parses SSE with bounds on line and event size. It **never renders markup it did not write**: row values and generated SQL are both untrusted, so highlighting is a tokenizer returning React elements rather than a string of HTML. And it **distinguishes the two truncations** — the server's row limit clipping a result, and the browser showing fewer rows than it received — because collapsing them into one banner lets a reader take the wrong one away. Served by FastAPI behind `API_STATIC_DIR` with a CSP, and by the Vite dev server locally; both keep the API's CORS allowlist empty by keeping page and API on one origin.

**Still open:** loading a target dataset — which is what "runs end to end from a clean checkout" is waiting on. The read-only connection *pool* landed with the endpoint — `PoolConnectionSource` over `psycopg_pool`, every connection proved by `assert_read_only` as the pool opens it. Not a performance change: `statement_timeout` is transaction-scoped, so two concurrent requests on one connection would run one under the other's limit. Prompt-cache verification is still deferred — `cache_read_tokens` is plumbed through but has never been observed non-zero against a real provider. Retrieval latency is guarded against an accidental full scan but not yet measured against the p95 budget, which needs a realistic corpus (Stage 6).

**And the API has no authentication**, which is Stage 6 work. Until then `APISettings` refuses to bind anything but loopback, so the gap is enforced rather than noted ([ADR-034](../architecture/DECISIONS.md#adr-034--the-api-refuses-to-bind-beyond-loopback-while-it-has-no-authentication)). [SECURITY.md](../operations/SECURITY.md) §13.9 lists the controls that must land with the first endpoint that accepts a request body.

**What the last four versions changed about the stage's shape:** validation, execution, generation and profiling all landed as plain components with constructor injection rather than as MCP servers. That is deliberate — Stage 3 wraps them, and the refactor can then be proven behaviour-preserving against a Stage 2 baseline instead of being asserted. It also means every capability is unit-testable without a transport.

Security and limits belong here, not in Stage 6. Retrofitting containment into a working system means restructuring it; adding *tracing* to correct span boundaries is easy.

## Stage 2 — Eval harness

**Output: baseline numbers to improve against.**

Deliberately before MCP, the agent layer, and the fine-tune. Without a baseline, every later change is an unfalsifiable improvement claim.

Scope: Spider/BIRD acquisition and SQLite→Postgres conversion; database-level splits; execution-accuracy comparison; Recall@k; per-question artifact persistence; failure taxonomy.

**Demo:** one command produces a scored report over the held-out split.

**Done when:**
- [~] Reproducible from a clean checkout via one command — three commands rather than one (verify, index, run), all now executed end to end against a real Postgres
- [x] Conversion verified — gold queries return identical results on SQLite and Postgres, compared with the harness's own comparator, exiting 3 when they do not
- [x] Splits are database-disjoint and committed as a file — and stable when the corpus grows, which a seeded shuffle is not
- [x] Baseline rows in [../ml/BENCHMARKS.md](../ml/BENCHMARKS.md) — §0 fidelity, §1 accuracy, §2 recall, §3 invalid-query rate, §5 latency and §6 cost all carry measured values, and §1.1 is now a **complete** full-split run: 921 of 921 questions, 20 of 20 databases, one model, zero infrastructure errors. Every row states what its own sample covers
- [~] Failure taxonomy populated with counts — real counts from real runs, and the first one found that four of the runner's own error types had no category and were landing in `uncategorised`

**The measurement machinery landed before the data, deliberately.** Comparison, Recall@k, the failure taxonomy, artifacts and resumption are built and tested against synthetic cases — 84 tests, no model and no database. The order matters: the logic that decides what a number *means* is the part a benchmark cannot check, because a wrong comparison produces a plausible score rather than an error. Building it against a dataset would have meant debugging two unknowns at once.

**Spider is loaded, and the real archive found nine defects the synthetic tests could not.** Acquisition, conversion, verification and splits had all been built and tested — against synthetic SQLite for the logic, a real PostgreSQL for the conversion — and were nonetheless wrong in nine places that only real data exposes: a sampling cap that missed one bad value in 510,437 rows, an identifier allowlist that refused two databases over characters that cannot escape a quoted identifier, foreign keys joining two types in 35 of 769 cases, a filename this filesystem cannot store, and 213 gold queries relying on a SQLite quirk. Two of the nine were **shipped bugs in Stage 1 code**, both in the same failure: `DATABASE_URL` in the form the example ships could not open a psycopg connection at all, and the resulting error printed the password.

The pattern is worth keeping. Every one of those was invisible to a test suite that generated its own inputs — **synthetic tests check the logic you thought of; real data checks the assumptions you did not know you had made.** DATASETS.md §1 and BENCHMARKS.md §0 now carry measured values instead of TBD. BIRD has still not been downloaded, and its rows stay TBD for the same reason they always did.

**What the load bought and what it did not.** 20 of 20 dev databases convert; **915 of 921 comparable gold results reproduce (99.3%); 19 of 20 databases reproduce every one.** That is a precondition for a baseline, not a baseline — **at that point** no question had been answered by the system itself. The runs described below are what changed it.

**Diagnosing the last 25 mismatches changed the number and, more usefully, the taxonomy.** Only 6 were conversion differences, and all 6 are one column that has no faithful static type. 3 were a transpilation gap — SQLite's `LIKE` folds case — and 16 were questions with no single correct answer, where a `LIMIT` cut through a tie. Two more looked exactly like those 16 and were the real defect; the rule that separates them is what keeps the 99.3% from being a number that had absorbed a fault. That pattern has now repeated three times in this stage: **a failure bucket named after the component under test collects everything unexplained, and each thing it collects reads as evidence against that component.**

**The pipeline ran, and the first run found five defects in four days-old code.** A question id used as a filename (Spider's contain colons, which Windows rejects — and BIRD ships ids that could traverse); a schema declaring the same foreign key twice; `index` refusing the 146 databases a split does not convert; HTTP 413 reported as "the provider could not be reached"; and a refusal's tokens going uncounted. Then two more that were each worth thirty accuracy points: `RETRIEVAL_TOP_K=10` starving a schema that holds 10–67 elements, so the model honestly refused half the questions, and a reasoning model's `<think>` block being submitted as SQL.

**The second of those is the one to remember.** It was invisible for as long as the configured model answered every question. It only appeared once a rate limit moved the fallback chain to a model that emits reasoning in `content` — so a mechanism added to *absorb* a provider failure is what exposed a defect, and would equally have hidden it. Execution accuracy went 42.7% → 75.3% across these fixes with no change to the prompt or the model.

**Resumability was added to this stage's scope.** It was not in the original plan and it is what makes the harness usable at all here: free-tier models cap tokens per model per day, so a full run spans most of a budget and being stopped partway is routine rather than exceptional.

**It paid for itself, and the receipt is the completed run.** The full split took three days and three daily budgets, resuming into the same directory each time. Days 1 and 2 each ended with 12 questions failed against a spent quota; day 3 re-attempted rather than retiring them, and all 12 succeeded — so the final result has **zero** infrastructure errors and `scored` equals `total` for the first time. Without resumption this corpus was simply not measurable on a free tier, which is the difference between a benchmark and an aspiration.

**The cost it charged was real too.** The configuration fingerprint includes the commit, so every day's resume was refused until it was run from a detached `git worktree` at the recorded commit. Day 3 also found that a worktree alone is not sufficient — the editable install puts the *main* checkout's `src/` on `sys.path`, so the guard can pass while the wrong code runs. Both are recorded in [BENCHMARKS §1.3](../ml/BENCHMARKS.md), and fingerprinting the loaded modules rather than the commit is now unblocked.

**What is still unmeasured here, stated plainly.** The completed run is `retrieval-only`, so no validator executed — `validation_attempts` is 0 on all 921 artifacts by construction, and the 13 invalid queries reached PostgreSQL and were refused there. It also runs the direct answering path, so **no accuracy figure measures the MCP servers** — but as of 2026-08-10 that gap is quantified rather than open: the retrieval hop returns identical elements over the wire on all 1,034 questions ([BENCHMARKS](../ml/BENCHMARKS.md) §8).

**Self-correction is unmeasured and no eval baseline can measure it.** `with-validation` is a *gate* — one validation pass, and a failing query is dropped rather than fed back to the model. `validation_attempts` is therefore 0 or 1 and never more. An earlier version of this section said a validation run would give self-correction a number; that was wrong, and the retry loop is Stage 4 work that does not exist yet.

## Stage 3 — MCP servers + client refactor

**Output: runnable from any MCP host.**

Scope: four MCP servers; agent becomes an MCP client with runtime discovery; stdio and HTTP transports; contract tests; host configuration for any stdio-speaking client.

**Demo:** any MCP host — Claude Desktop, or the project's own `ToolRegistry` — connects, discovers the four tools, and queries a database with none of them hardcoded.

> **This is a capability claim, not the project's demo.** Seeing it requires installing a host, editing a JSON config with absolute paths, a running Postgres and an indexed catalog. Nobody evaluating this repo will do that, and an earlier version of this line said "point a desktop host at the servers" as though they would. **The thing a reader actually sees is the web UI, and it now exists** — but note what that means for this stage: the UI talks to `POST /v1/query`, which calls the answering components *directly*. It does not go through the MCP servers. The demo a reader sees and the capability this stage claims are two different paths through the same components.

**Done when:**
- [x] All four servers pass contract tests
- [x] Agent discovers tools at runtime — no hardcoded tool list
- [x] `execute_sql` validates independently of the caller
- [x] Copy-pasteable Claude Desktop config ([../architecture/MCP.md](../architecture/MCP.md) §9) — *written from the working stdio configuration; not yet run inside Claude Desktop itself*
- [x] **Eval re-run — behaviour unchanged.** Measured 2026-08-10: over all 1,034 dev questions, retrieval through `search_schema` on a subprocess returned **byte-identical ordered element lists** to retrieval in process — 1,034 of 1,034, zero differences ([BENCHMARKS](../ml/BENCHMARKS.md) §8). Closed as an *identity* rather than an accuracy figure, and §8.2 says why that is the stronger result.

**Built ahead of Stage 2, deliberately and with one cost.** The servers landed before the eval harness because all four capabilities existed to be wrapped and this is the project's headline claim. The cost is the last checkbox: there was no baseline to re-run against, so "accuracy unchanged" could not be *measured*. What stands in for it is that every server is a thin adapter over a component that was already tested directly, with contract tests asserting the same behaviour over the wire — which is an argument, not a measurement, and the checkbox stays open until it is one.

**As of 2026-08-10 it is a measurement.** The paragraph above stood for two days and its prediction was right about *where* to look and wrong about *what to measure*. The servers are indeed the only place a serialization or clamping difference could hide — so the check went straight at that, comparing the retrieved elements themselves rather than the accuracy downstream of them. Every one of 1,034 questions matched exactly, and the cost of the hop came out at a constant **+7.8 ms** per call.

**Reproducing 79.9% was the wrong target, and that is worth saying rather than quietly not doing it.** An accuracy run affordable in a day is about 100 questions, which at this accuracy carries an interval near ±8 points — wide enough to contain any regression worth finding. Meanwhile everything downstream of retrieval is literally the same code, so identical context means the paths differ by nothing that reaches the model, and a measured accuracy gap would have been **provider sampling noise published as a finding about MCP**. The `mcp-retrieval` baseline is built, smoke-run end to end and available; what it would add now is confirmation of something already determined.

The `search_schema` hop is what was measured. `validate_sql`, `execute_sql` and `profile_table` cross no benchmark yet — see [BENCHMARKS](../ml/BENCHMARKS.md) §8.3.

The risk here is shipping a protocol wrapper. Contract quality — descriptions that say *when* to call, schema-enforced limits, structured errors — is the actual work. See [../architecture/MCP.md](../architecture/MCP.md) §3.

## Stage 4 — Agent layer

**Output: multi-step task success metric.**

Scope: planner and decomposition; session memory; self-correction with error-type-aware retry prompts; budget enforcement; custom multi-step eval set.

**Demo:** "compare Q3 vs Q4 growth by region and flag anomalies" — several queries plus synthesis, streamed.

**Done when:**
- [ ] Multi-step eval set exists and is graded (**written before the feature**, to limit bias)
- [ ] Self-correction measurably reduces invalid-query rate — with the pre/post gap published
- [ ] `MAX_TOOL_CALLS_PER_REQUEST` enforced and tested
- [ ] Follow-up questions resolve against session memory
- [ ] Retry prompts branch on error type — a timeout is not retried verbatim

The number to watch is self-correction *recovery* rate, not retry rate. A loop that retries often and recovers rarely is expensive theatre.

## Stage 5 — Fine-tuned schema linker

**Output: Recall@k before/after ablation.**

Scope: pair extraction from gold SQL; hard-negative mining; contrastive fine-tune; re-embedding under a new `model_version`; the five ablations in [../ml/TRAINING.md](../ml/TRAINING.md) §8.

**Demo:** side-by-side retrieval on a question where the baseline misses and the fine-tune hits.

**Done when:**
- [ ] Training reproducible from a recorded command
- [ ] A1 (baseline vs fine-tuned) and A2 (fine-tuned@5 vs baseline@20) reported
- [ ] **A5 reported** — does better Recall@k actually improve execution accuracy?
- [ ] Multiple seeds run; variance reported, not one lucky number
- [ ] Result published **whichever way it goes**

A5 is the honest one and the easiest to skip. If retrieval improves and end-to-end accuracy does not, retrieval was not the bottleneck — worth knowing and worth publishing.

## Stage 6 — Hardening

**Output: production-style repo.**

Scope: OpenTelemetry instrumentation; rate limiting; cost caps; Docker + Compose; load tests; dependency scanning; coverage to target; final performance numbers.

**Done when:**
- [ ] Traces show the full agent → MCP → database path with retries as sibling spans
- [ ] Load tests establish throughput and saturation behaviour
- [ ] Performance targets measured — including the ones that were missed
- [ ] Coverage ≥ 85%, security suite at 100%
- [ ] `docker compose up` works from a clean clone
- [ ] Demo script verified end to end

---

## Sequencing rationale

Two orderings that look wrong and are deliberate:

**Eval before MCP.** The MCP refactor should not change behaviour — but "should not" is only checkable with a baseline. Building eval first makes the refactor verifiable.

**Fine-tune second-to-last.** It is the highest-variance stage: it may not work. Putting it after everything else means a null result costs one stage, not the project — and a null result is still publishable ([ADR-006](../architecture/DECISIONS.md#adr-006--fine-tune-the-schema-linker-rather-than-retrieve-more-candidates)).

Risks that could disrupt this: [RISKS.md](RISKS.md).
