# Roadmap

Six stages over 5–6 weeks. **Every stage produces something demoable**, so a bad week costs a stage rather than the project.

Working checklist: [TASKS.md](TASKS.md).

---

## Progress

Percentages are checkbox counts, not confidence. **Stages have not been built in order**, and the two that jumped the queue each carry an explicit cost recorded in their section below.

| Stage | Output | Status | % |
|---|---|---|---|
| 0 | Scaffolding — docs, deps, interpreter pin | ✅ Done | 100% |
| 1 | **Core loop** — retrieval, generation, validation, execution, profiling, API, demo UI | 🚧 In progress | 58% |
| 2 | **Eval harness** — comparison, Recall@k, artifacts, resumption | 🚧 In progress | 43% |
| 3 | **MCP servers + client refactor** | 🚧 In progress | 81% |
| 4 | **Agent layer** — decomposition, session memory, self-correction | ⬜ Not started | 0% |
| 5 | **Fine-tuned schema linker** | ⬜ Not started | 0% |
| 6 | **Hardening** — limits, tracing, tests | ⬜ Not started | 0% |

**What is genuinely blocking, in order:** loading a benchmark dataset (blocks every number in Stages 2, 3 and 5), then the HTTP API and the demo UI it serves (blocks the Stage 1 close-out and any visual demo), then the agent loop.

**Stage 1 dropped from ~75% to 58%** when the demo UI was added to its scope. The percentage got worse because the plan got more honest, which is the direction it should move.

**Stage 1 is not "the core loop works end to end".** Every component is built and tested; nothing has been run against a real dataset from a clean checkout, because there is no dataset. That distinction is the difference between the percentage above and a working demo.

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
- [~] `.env.example` and [CONFIG.md](../operations/CONFIG.md) match the implementation — CONFIG.md tracks every shipped setting; `.env.example` is still outstanding
- [ ] Span boundaries are in place (instrumentation added later, structure now)
- [ ] **A browser can ask a question and watch it being answered.** Added after noticing the project had no surface a reader could see — the MCP servers are a capability, not a demo, and until this exists there is nothing to put in a README GIF either

**Landed so far:** Postgres + pgvector with migrations and the read-only role (30 negative tests, green); the `LLMClient` and `Embedder` ports; typed settings with an SSRF guard; the schema catalog — introspection, serialization, embedding, and an idempotent indexer; retrieval — ANN over pgvector with `table_filter`, join-path expansion and clamped limits; five-stage SQL validation with structured rejections and nearest-match suggestions; sandboxed execution with AST-level row limits, per-statement timeouts and an audit trail on a separate owner connection; SQL generation behind one OpenAI-compatible adapter with a model fallback chain; and table profiling under an explicit disclosure budget.

**Still open:** FastAPI + SSE, `.env.example`, and loading a target dataset — which is what "runs end to end from a clean checkout" is waiting on. Two smaller items are deliberately deferred with the seam already in place: the read-only connection *pool* (`ConnectionSource` exists; a pool with one client is machinery without a job until the API layer creates a second caller), and prompt-cache verification (`cache_read_tokens` is plumbed through but has never been observed non-zero against a real provider). Retrieval latency is guarded against an accidental full scan but not yet measured against the p95 budget, which needs a realistic corpus (Stage 6).

**What the last four versions changed about the stage's shape:** validation, execution, generation and profiling all landed as plain components with constructor injection rather than as MCP servers. That is deliberate — Stage 3 wraps them, and the refactor can then be proven behaviour-preserving against a Stage 2 baseline instead of being asserted. It also means every capability is unit-testable without a transport.

Security and limits belong here, not in Stage 6. Retrofitting containment into a working system means restructuring it; adding *tracing* to correct span boundaries is easy.

## Stage 2 — Eval harness

**Output: baseline numbers to improve against.**

Deliberately before MCP, the agent layer, and the fine-tune. Without a baseline, every later change is an unfalsifiable improvement claim.

Scope: Spider/BIRD acquisition and SQLite→Postgres conversion; database-level splits; execution-accuracy comparison; Recall@k; per-question artifact persistence; failure taxonomy.

**Demo:** one command produces a scored report over the held-out split.

**Done when:**
- [~] Reproducible from a clean checkout via one command — the command exists and refuses at the pipeline seam
- [ ] Conversion verified — gold queries return identical results on SQLite and Postgres
- [ ] Splits are database-disjoint and committed as a file
- [ ] Baseline rows in [../ml/BENCHMARKS.md](../ml/BENCHMARKS.md)
- [~] Failure taxonomy populated with counts — the taxonomy and its counting exist; there is nothing to count yet

**The measurement machinery landed before the data, deliberately.** Comparison, Recall@k, the failure taxonomy, artifacts and resumption are built and tested against synthetic cases — 84 tests, no model and no database. The order matters: the logic that decides what a number *means* is the part a benchmark cannot check, because a wrong comparison produces a plausible score rather than an error. Building it against a dataset would have meant debugging two unknowns at once.

**Resumability was added to this stage's scope.** It was not in the original plan and it is what makes the harness usable at all here: free-tier models cap tokens per model per day, so a full run spans most of a budget and being stopped partway is routine rather than exceptional.

## Stage 3 — MCP servers + client refactor

**Output: runnable from any MCP host.**

Scope: four MCP servers; agent becomes an MCP client with runtime discovery; stdio and HTTP transports; contract tests; host configuration for any stdio-speaking client.

**Demo:** any MCP host — Claude Desktop, or the project's own `ToolRegistry` — connects, discovers the four tools, and queries a database with none of them hardcoded.

> **This is a capability claim, not the project's demo.** Seeing it requires installing a host, editing a JSON config with absolute paths, a running Postgres and an indexed catalog. Nobody evaluating this repo will do that, and an earlier version of this line said "point Claude Desktop at the servers" as though they would. The thing a reader actually sees is the web UI in Stage 1's close-out.

**Done when:**
- [x] All four servers pass contract tests
- [x] Agent discovers tools at runtime — no hardcoded tool list
- [x] `execute_sql` validates independently of the caller
- [x] Copy-pasteable Claude Desktop config ([../architecture/MCP.md](../architecture/MCP.md) §9) — *written from the working stdio configuration; not yet run inside Claude Desktop itself*
- [ ] **Eval re-run — accuracy unchanged.** A refactor that silently changed behaviour is a regression.

**Built ahead of Stage 2, deliberately and with one cost.** The servers landed before the eval harness because all four capabilities existed to be wrapped and this is the project's headline claim. The cost is the last checkbox: there is no baseline to re-run against, so "accuracy unchanged" cannot be *measured* yet. What stands in for it is that every server is a thin adapter over a component that was already tested directly, with contract tests asserting the same behaviour over the wire — which is an argument, not a measurement, and the checkbox stays open until it is one.

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
