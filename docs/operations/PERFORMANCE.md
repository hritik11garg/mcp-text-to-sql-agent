# Performance

> **Status: targets are set; results are TBD — Stage 6.** The targets below are budgets to design against, not measurements. Measured numbers go to [../ml/BENCHMARKS.md](../ml/BENCHMARKS.md) §5 and are reflected here.

---

## 1. Targets

| Component | Target | Measure | Rationale |
|---|---|---|---|
| Schema retrieval | **< 100 ms** p95 | `retrieval_duration_seconds` | ANN over a few thousand vectors; more than this means an index problem |
| SQL validation | **< 50 ms** p95 | `mcp.call validate_sql` | Parse + `EXPLAIN`, no execution. Called repeatedly, so it must stay cheap |
| Table profiling | **< 1 s** p95 | `mcp.call profile_table` | One bounded scan per column, so cost is `PROFILE_MAX_COLUMNS` × `PROFILE_SCAN_LIMIT`. Its own `PROFILE_TIMEOUT_MS` (10 s) is deliberately shorter than the executor's — profiling is a side quest during answering and should give up long before the real query would |
| Query execution | **< 2 s** p95 | `db_query_duration_seconds` | Analytical aggregates on benchmark-sized data |
| SSE first token | **< 500 ms** | Time to first event | Perceived responsiveness |
| End-to-end (single query) | **< 8 s** p95 | `request_duration_seconds` | Dominated by LLM generation |
| End-to-end (multi-step) | **< 25 s** p95 | — | N sub-queries plus synthesis |

**These are budgets, not predictions.** The interesting outcome is where they are missed — a missed budget points at the component to fix.

**First-token latency is the target most likely to be missed**, and the most user-visible. The first SSE event should be the `session` event, emitted before any model call — so a slow LLM cold start does not read as a hung request.

## 2. Expected latency distribution

Design assumption, to be confirmed or refuted:

```
LLM generation   ████████████████████████████  ~70%
Query execution  ██████                        ~15%
Retrieval        ██                             ~5%
Validation       █                              ~3%
Everything else  ███                            ~7%
```

If LLM generation is not dominant, something is wrong upstream and the optimization target changes completely. Confirming this ordering is the first Stage 6 measurement, because optimizing the wrong component is the default failure mode here.

## 3. Optimization levers, in order

Ordered by expected payoff. **None applied before measurement** — that is how effort gets spent on the 3% component.

| Lever | Targets | Cost |
|---|---|---|
| **Prompt caching** | LLM latency + spend | Prefix discipline; verify with `cache_read_tokens` |
| **Lower `effort` on simple questions** | LLM latency | Accuracy tradeoff — needs a measured comparison |
| **Fewer round trips** (better retrieval → fewer retries) | LLM latency | This is what the Stage 5 fine-tune buys, beyond accuracy |
| **HNSW `ef_search` tuning** | Retrieval | Recall tradeoff; sweep against Recall@k. Configurable as `HNSW_EF_SEARCH` |
| **HNSW `iterative_scan`** | Retrieval **correctness**, not just speed | Already on (`relaxed_order`). Turning it off is faster and silently returns fewer than `k` rows — see [../architecture/DATABASE.md](../architecture/DATABASE.md) §5.1 |
| **Connection pool sizing** | Execution queueing | Database load |
| **`MAX_ESTIMATED_COST`** | Tail latency | Rejects expensive queries pre-execution |
| **`PROFILE_SCAN_LIMIT` / `PROFILE_MAX_COLUMNS`** | Profiling, and context budget | Statistics get less accurate. Both are also disclosure bounds — lowering them is safe in both directions, raising them is a security change ([../operations/SECURITY.md](SECURITY.md) §14.2.6) |
| **Batching profiling into one statement per table** | Profiling round trips | Not done: types vary per column and frequent values need their own `GROUP BY`, so one composed statement over 30 columns is fragile. The first thing to revisit if profiling ever moves onto a hot path |
| **Cross-encoder reranking** | Retrieval quality | *Adds* latency — [FUTURE.md](../project/FUTURE.md), not v1 |

The second-order effect of better retrieval is worth naming: each avoided retry removes a full generation round trip. A fine-tune that reduces mean attempts from 1.8 to 1.3 improves p95 latency more than most direct latency work would.

## 4. Load characteristics

> **TBD — Stage 6**, via the `locust` suite in [../development/TESTING.md](../development/TESTING.md).

Questions to answer with numbers rather than intuition:

- Sustained throughput at which p95 stays within target.
- Concurrency at which the connection pool saturates and requests begin queueing.
- Behaviour past saturation — graceful queueing or collapse.
- Memory per replica with the embedding model loaded.
- Cost of one expensive query on concurrent request latency.

**Graceful degradation past saturation matters more than peak throughput.** A system that queues predictably is operable; one that collapses is not.

## 5. Measured results

> **TBD — Stage 6.** Populated from BENCHMARKS.md §5. Every row carries hardware — a latency number without it is not comparable to anything.

| Component | Target | p50 | p95 | p99 | Hardware | Status |
|---|---|---|---|---|---|---|
| Retrieval | < 100 ms | — | — | — | — | Not measured |
| Validation | < 50 ms | — | — | — | — | Not measured |
| Execution | < 2 s | — | — | — | — | Not measured |
| SSE first token | < 500 ms | — | — | — | — | Not measured |
| End-to-end (single) | < 8 s | — | — | — | — | Not measured |
| End-to-end (multi) | < 25 s | — | — | — | — | Not measured |

## 6. Known costs accepted

| Cost | Why accepted |
|---|---|
| Validation runs on every generated query | Cheap, side-effect-free, and it is the whole point of the tier |
| `execute_sql` re-validates independently | It cannot assume a well-behaved caller ([MCP.md](../architecture/MCP.md) §3.3) |
| MCP adds IPC latency vs direct calls | The price of a real tool boundary ([ADR-003](../architecture/DECISIONS.md#adr-003--mcp-for-the-tool-boundary)) |
| Embedding model in every replica's memory | Simpler than a shared embedding service at this scale |
| Adaptive thinking raises per-request latency | The task is reasoning-heavy; disabling it is an accuracy tradeoff, not a free win |
