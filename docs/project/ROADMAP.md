# Roadmap

Six stages over 5–6 weeks. **Every stage produces something demoable**, so a bad week costs a stage rather than the project.

Overall completion: **0%** (Stage 0 scaffolding done; no code yet)

Working checklist: [TASKS.md](TASKS.md).

---

## Progress

| Stage | Output | Weeks | Status | % |
|---|---|---|---|---|
| 0 | Scaffolding — docs, deps, interpreter pin | — | ✅ Done | 100% |
| 1 | **Core loop** — retrieval, generation, validation, sandboxed execution | 1.5–2 | 🚧 In progress | ~35% |
| 2 | **Eval harness** — Spider/BIRD subset, execution accuracy | 0.5 | ⬜ Not started | 0% |
| 3 | **MCP servers + client refactor** | 0.5–1 | ⬜ Not started | 0% |
| 4 | **Agent layer** — decomposition, session memory, self-correction | 1 | ⬜ Not started | 0% |
| 5 | **Fine-tuned schema linker** | 1 | ⬜ Not started | 0% |
| 6 | **Hardening** — limits, tracing, tests | 0.5 | ⬜ Not started | 0% |

---

## Stage 0 — Scaffolding ✅

Documentation structure, dependency set verified against PyPI for Python 3.12, interpreter pinned.

**Done when:** all 28 documents exist with real structure, `pip install -r requirements.txt` resolves cleanly, design decisions that are knowable now are recorded in [DECISIONS.md](../architecture/DECISIONS.md).

## Stage 1 — Core loop

**Output: working single-query text-to-SQL against a real database.**

The first demoable thing. Ask a question in English, get an answer from Postgres.

Scope: Postgres + pgvector up with roles and migrations; schema ingestion and embedding; baseline retrieval; SQL generation; sqlglot AST validation + `EXPLAIN`; sandboxed execution under limits; FastAPI + SSE.

**Demo:** a question over a loaded schema returns a correct answer, with progress streaming.

**Done when:**
- [ ] Runs end to end from a clean checkout per the README
- [x] **The read-only negative test suite is green** — this gates the stage, not Stage 6
- [ ] Row limits and statement timeouts are enforced and tested
- [ ] `.env.example` and [CONFIG.md](../operations/CONFIG.md) match the implementation
- [ ] Span boundaries are in place (instrumentation added later, structure now)

**Landed so far:** Postgres + pgvector with migrations and the read-only role (30 negative tests, green); the `LLMClient` and `Embedder` ports; typed settings with an SSRF guard; and the schema catalog — introspection, serialization, embedding, and an idempotent indexer.

**Still open:** retrieval over pgvector ANN, SQL generation, sqlglot validation, sandboxed execution, and FastAPI + SSE. Row limits and timeouts exist at the *role* level and are tested; the per-request clamps are written but not yet exercised end to end.

Security and limits belong here, not in Stage 6. Retrofitting containment into a working system means restructuring it; adding *tracing* to correct span boundaries is easy.

## Stage 2 — Eval harness

**Output: baseline numbers to improve against.**

Deliberately before MCP, the agent layer, and the fine-tune. Without a baseline, every later change is an unfalsifiable improvement claim.

Scope: Spider/BIRD acquisition and SQLite→Postgres conversion; database-level splits; execution-accuracy comparison; Recall@k; per-question artifact persistence; failure taxonomy.

**Demo:** one command produces a scored report over the held-out split.

**Done when:**
- [ ] Reproducible from a clean checkout via one command
- [ ] Conversion verified — gold queries return identical results on SQLite and Postgres
- [ ] Splits are database-disjoint and committed as a file
- [ ] Baseline rows in [../ml/BENCHMARKS.md](../ml/BENCHMARKS.md)
- [ ] Failure taxonomy populated with counts

## Stage 3 — MCP servers + client refactor

**Output: runnable from any MCP host.**

Scope: four MCP servers; agent becomes an MCP client with runtime discovery; stdio and HTTP transports; contract tests; Claude Desktop config.

**Demo:** point Claude Desktop at the servers and query a database from it.

**Done when:**
- [ ] All four servers pass contract tests
- [ ] Agent discovers tools at runtime — no hardcoded tool list
- [ ] `execute_sql` validates independently of the caller
- [ ] Verified working in Claude Desktop with copy-pasteable config
- [ ] **Eval re-run — accuracy unchanged.** A refactor that silently changed behaviour is a regression.

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
