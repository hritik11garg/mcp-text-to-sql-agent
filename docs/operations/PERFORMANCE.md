# Performance

> **Status: targets are set; results are TBD — Stage 6.** The targets below are budgets to design against, not measurements. Measured numbers go to [../ml/BENCHMARKS.md](../ml/BENCHMARKS.md) §5 and are reflected here.

---

## 1. Targets

| Component | Target | Measure | Rationale |
|---|---|---|---|
| Schema retrieval | **< 100 ms** p95 | `retrieval_duration_seconds` | ANN over a few thousand vectors; more than this means an index problem |
| SQL validation | **< 50 ms** p95 | `mcp.call validate_sql` | Parse + `EXPLAIN`, no execution. Called repeatedly, so it must stay cheap |
| Table profiling | **< 1 s** p95 | `mcp.call profile_table` | One bounded scan per column, so cost is `PROFILE_MAX_COLUMNS` × `PROFILE_SCAN_LIMIT`. Its own `PROFILE_TIMEOUT_MS` (10 s) is deliberately shorter than the executor's — profiling is a side quest during answering and should give up long before the real query would |
| Query execution | **< 2 s** p95 | `db_query_duration_seconds` | Analytical aggregates on benchmark-sized data. **First measurements: 3–4 ms** on Spider-sized schemas, from `agent_meta.query_audit` — three orders under budget, and that is a statement about the data rather than the code |
| MCP call overhead | **< 20 ms** p95 | `mcp.call` minus tool duration | Framing plus a local pipe. Anything larger means the cost is in serialization, not the protocol |
| MCP server startup | **< 3 s** | Process launch → `tools/list` returned | Four subprocesses, each opening two connections and loading the catalog. Paid once per session, and it is why the catalog is a snapshot rather than re-read per call |
| API startup | **< 5 s** | Process launch → `/ready` returns 200 | Two connections, the read-only assertion (three round trips), the catalog, and the embedder. All eager, deliberately: a lazy startup moves these costs onto the first request and moves configuration errors past the deploy |
| `/health` | **< 5 ms** p95 | — | Serializes a fixed dict. It touches nothing, so anything larger is framework overhead worth looking at |
| `/ready` | **< 20 ms** p95 uncached | — | Two `SELECT 1`s on held connections, then cached 5 s. The cache is the control that stops an unauthenticated endpoint being a load generator, not an optimization |
| SSE first event | **< 500 ms** | Time to first event | Perceived responsiveness. **Measured: ~600–900 ms** for the `retrieve` stage event — see below |
| End-to-end (single query) | **< 8 s** p95 | `request_duration_seconds` | **Met: 0.6–1.8 s** warm. The 29 s first recorded here was a cold start, not a provider cost — see below |
| End-to-end (multi-step) | **< 25 s** p95 | — | N sub-queries plus synthesis |

**These are budgets, not predictions.** The interesting outcome is where they are missed — a missed budget points at the component to fix.

### Correction: the 29 s measurement was a cold start, and it was attributed to the wrong thing

**The previous version of this section was wrong, and it is worth recording how rather than quietly replacing it.** It reported one served request as `answer` 29,081 ms against `execute` 28 ms, concluded that *"everything this project controls is already fast and the budget is spent somewhere it does not own"*, and identified the 29 seconds as one round trip to a rate-limited free tier.

The split was real. The **attribution** was not. `answer` bundles retrieval and generation into a single timed stage, so an aggregate of 29 s was consistent with a slow provider and equally consistent with something slow in retrieval — and nothing in the measurement could tell them apart.

The per-stage `stage` events added for SSE separated them, and the first streamed request showed this:

```
[20:05:33]  (stream opens)
[20:05:48]  : keepalive            <- 15 s of silence, before retrieval finished
[20:05:53]  event: stage  {"stage":"retrieve"}   ~20 s
[20:05:55]  event: stage  {"stage":"generate"}   ~2 s
[20:05:55]  event: rows
```

**Retrieval was taking twenty seconds and generation two.** The cause is [ADR-040](../architecture/DECISIONS.md#adr-040--startup-opens-the-model-because-naming-it-is-not-loading-it): `SentenceTransformerEmbedder` loads its checkpoint lazily, the lifespan only read `model_version` — a configured string that loads nothing — and so **the first caller paid the model load inside their request.**

### The measurements, after the fix

Freshly started process, Spider `concert_singer`, `k=30`, `openai/gpt-oss-120b` on a free tier:

| | Before ADR-040 | **After** |
|---|---|---|
| First request, `answer` | **21,845 ms** | **2,869 ms** |
| Warm request, `answer` | — | **621 / 776 / 920 / 1,783 ms** |
| Warm request, `execute` | — | **15.0 / 15.6 / 17.0 / 20.5 ms** |
| Model load | inside the first request | **~19 s inside startup** |

**The `< 8 s` budget is met, comfortably, and the earlier "missed by 3.6×" was measuring process startup.** Warm end-to-end is under two seconds including a live model call.

Three things follow:

- **`execute` at 15–21 ms confirms the original conclusion for the part it was actually about.** Validation and query execution are three orders of magnitude under budget. That part of the old section survives; what does not is the claim that the *remaining* 29 seconds belonged to the provider.
- **An unattributed aggregate is not a measurement, it is a number.** A single `answer` stage could not distinguish a slow retriever from a slow provider — opposite problems with opposite fixes — and it produced a confident, documented, wrong diagnosis. The finer split shipped for a user-facing reason and immediately corrected a benchmark, which is the strongest argument this project has for instrumenting at component boundaries rather than at the request boundary.
- **The streaming argument is weaker on latency and unchanged on cold start.** *"29 seconds of silence is indistinguishable from a hang"* was true of the run that was measured, and that run was pathological. A 1.8 s answer does not need streaming to look alive. What streaming still buys is the `sql` event arriving before execution, visible progress on the slow schemas, and — the actual reason it earned its place here — **the per-stage split that found this defect.**

**What is not yet measured:** p95 of anything. These are single observations on one small schema, which is the correct amount of measurement for a component that started serving two days ago and the wrong amount to put in a benchmark row. `/health`, `/ready`, MCP overhead and retrieval latency against a *realistic* corpus all remain unmeasured — the 15–21 ms execution figure is against benchmark-sized tables and says nothing about a large one.

**First-event latency is the target most likely to be missed**, and the most user-visible. It is currently the `retrieve` stage event at roughly 600–900 ms, which is over the 500 ms budget and now dominated by embedding the question rather than by anything remote.

## 2. Expected latency distribution

Design assumption, written before anything served a request:

```
LLM generation   ████████████████████████████  ~70%
Query execution  ██████                        ~15%
Retrieval        ██                             ~5%
Validation       █                              ~3%
Everything else  ███                            ~7%
```

**The first real split is roughly the right shape with query execution far too high.** Over the four warm requests in §1, `execute` is 15–21 ms against an `answer` of 621–1,783 ms — so execution is nearer **2%** than 15%, and generation correspondingly more dominant than assumed. That is against benchmark-sized tables, which is exactly the condition under which execution *should* be negligible; the assumption was written with a realistic corpus in mind and has not been tested against one.

Retrieval is the number to watch. At ~600–900 ms for the first stage event it is already larger than the ~5% assumed, and that is over a schema with four tables. It is question-embedding cost, which is roughly constant, plus an ANN search, which is not — so the share will move in both directions as the corpus grows.

If LLM generation stops being dominant, something is wrong upstream and the optimization target changes completely. Confirming this ordering **on a realistic corpus** is the first Stage 6 measurement, because optimizing the wrong component is the default failure mode here — and §1 is a worked example of how confidently that can happen.

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
- **Concurrency at which the model provider rate-limits, which is probably lower than any of the above.**

**Graceful degradation past saturation matters more than peak throughput.** A system that queues predictably is operable; one that collapses is not.

**The last question was added because the assumption behind the other five is already known to be wrong.** Every one of them treats the database as the scarce resource. In practice the *model provider* saturated first, and it did so under conditions that should have been the safest possible: the eval harness is sequential, single-user, and one question at a time, and it still hit HTTP 429s against a free tier's daily cap.

Two consequences for any load test:

- **Rate-limit behaviour is a correctness concern, not only a latency one.** The fallback chain responds to a 429 by switching model, so the system under sustained load is answering with a *different model* than the one under light load — worth tens of accuracy points, per [../ml/BENCHMARKS.md](../ml/BENCHMARKS.md) §1. A load test measuring only latency would report this as success.
- **Parallelism does not help here and probably hurts.** The binding constraint is tokens per minute, not wall-clock, so more concurrency reaches the cap sooner and produces more blended runs. This is why the eval harness is deliberately sequential.

Provider quota therefore belongs in the capacity model alongside `DB_POOL_MAX_SIZE` — see [../project/FUTURE.md](../project/FUTURE.md) § *Provider-side rate budgeting*.

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
