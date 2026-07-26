# System Architecture

> **Status: design intent — Stage 1 will confirm or correct it.** The component boundaries and data flow below are decided; the diagram image, measured latencies, and any structure that changes once code exists are marked `TBD`.

---

## 1. Architecture diagram

> **TBD — Stage 1.** Committed to `docs/assets/architecture.png` and embedded here. ASCII placeholder:

```
                    ┌──────────────────────────────────────┐
   NL question ───▶ │           FastAPI (HTTP)             │ ───▶ SSE progress
                    │  POST /query  ·  GET /health         │
                    └───────────────┬──────────────────────┘
                                    │
                    ┌───────────────▼──────────────────────┐
                    │              AGENT                   │
                    │  planner · executor · session memory │
                    │  self-correction loop · budgets      │
                    │        (an MCP *client*)             │
                    └───────────────┬──────────────────────┘
                                    │  JSON-RPC 2.0 over MCP
        ┌───────────────┬───────────┴───────────┬──────────────────┐
        ▼               ▼                       ▼                  ▼
 ┌─────────────┐ ┌─────────────┐        ┌──────────────┐  ┌───────────────┐
 │schema_search│ │validate_sql │        │ execute_sql  │  │ profile_table │
 │             │ │             │        │              │  │               │
 │ retriever   │ │ sqlglot AST │        │ read-only    │  │ column stats  │
 │ + pgvector  │ │ + EXPLAIN   │        │ role, limits │  │ sample rows   │
 └──────┬──────┘ └──────┬──────┘        └──────┬───────┘  └───────┬───────┘
        │               │                      │                  │
        └───────────────┴──────────┬───────────┴──────────────────┘
                                   ▼
                    ┌──────────────────────────────────────┐
                    │            PostgreSQL 16             │
                    │  target schema (read-only role)      │
                    │  pgvector: schema embeddings         │
                    └──────────────────────────────────────┘

   OpenTelemetry spans wrap every arrow above.
```

## 2. Components

### 2.1 API layer (FastAPI)

Thin. Accepts a question, opens an SSE stream, delegates to the agent, streams progress events, returns the final answer. Holds no business logic — everything meaningful is in the agent or behind an MCP boundary, so the same agent runs unchanged when an MCP host drives it instead of HTTP.

Responsibilities: request validation, session correlation, SSE framing, rate limiting, health/readiness probes.

### 2.2 Agent (MCP client)

The orchestrator. Not a fixed pipeline — it discovers tools at runtime via `tools/list` and decides which to call.

Sub-components:

| Sub-component | Responsibility |
|---|---|
| **Planner** | Decides whether a question is single-step or needs decomposition; produces a plan of sub-questions |
| **Executor** | Runs the agent loop: pick tool → call → observe → repeat |
| **Session memory** | Stores prior results and the resolved schema context for follow-ups |
| **Self-correction** | Turns validation/execution errors into structured observations and re-attempts, up to a bounded retry count |
| **Budget enforcement** | Caps tool calls, tokens, wall-clock time, and rows per request |

Model: `claude-opus-5` via the Anthropic SDK, with adaptive thinking. Prompt versions live in [../ml/PROMPTS.md](../ml/PROMPTS.md).

### 2.3 MCP servers

Four processes, each a real capability boundary rather than a wrapper around a function. Full contracts in [MCP.md](MCP.md).

| Server | Reads DB | Writes DB | Retryable | Notes |
|---|---|---|---|---|
| `schema_search` | Yes (embeddings) | No | Yes | Vector search over serialized schema elements |
| `validate_sql` | Yes (`EXPLAIN` only) | No | **Yes, freely** | Never executes the statement |
| `execute_sql` | Yes | **No** — read-only role | No | Row limits, statement timeout, cost cap |
| `profile_table` | Yes | No | Yes | Column stats and sampled rows for disambiguation |

### 2.4 Retrieval subsystem

Schema elements (tables and columns, serialized with names, types, comments, and a few representative values) are embedded and stored in pgvector. At query time the question is embedded and the top-k elements retrieved.

Two retriever variants ship, selected by config:
- **Baseline** — off-the-shelf sentence-transformer.
- **Fine-tuned** — contrastive fine-tune on question→column pairs.

Both are exercised by the same eval harness so the Recall@k delta is a clean ablation. See [../ml/TRAINING.md](../ml/TRAINING.md).

### 2.5 Database

PostgreSQL 16 with pgvector. Two roles: an owner role used by migrations and the embedding indexer, and a `SELECT`-only role used by `execute_sql`. Details in [DATABASE.md](DATABASE.md).

## 3. Data flow

### 3.1 Single-query path

1. `POST /query` with a natural-language question; SSE stream opens.
2. Agent connects to MCP servers, discovers tools.
3. Agent calls `schema_search` → top-k tables/columns.
4. *(Conditional)* Ambiguity detected → `profile_table` on the contested tables.
5. Agent generates SQL from the question plus retrieved schema.
6. Agent calls `validate_sql` → AST parse + `EXPLAIN`.
   - On failure, the error is fed back and step 5 repeats (bounded retries).
7. Agent calls `execute_sql` under row limit and statement timeout.
   - On database error, feed back and return to step 5.
8. Agent formats results into a natural-language answer; SSE closes.

### 3.2 Multi-step path

Steps 1–2 as above, then: planner decomposes the question into sub-questions → each sub-question runs the single-query path → results land in session memory → a synthesis step composes the final answer from the sub-results.

## 4. Sequence diagrams

> **TBD — Stage 1 / Stage 4.** Mermaid diagrams for: (a) the happy single-query path, (b) validation failure with self-correction, (c) statement timeout during execution, (d) multi-step decomposition with synthesis.

The distinction worth diagramming carefully: what the agent observes on a **timeout** versus a **syntax error**. A syntax error is deterministic and structural — retry with a fixed query. A timeout is a resource signal — retry with a narrower query, a tighter filter, or give up and report. Conflating them makes the self-correction loop retry the wrong thing.

## 5. Design decisions

Recorded with alternatives and tradeoffs in [DECISIONS.md](DECISIONS.md). The load-bearing ones:

1. **Validation and execution are separate MCP servers**, because validation is side-effect-free and freely retryable while execution is neither. Merging them would force the retry policy of the unsafe operation onto the safe one.
2. **The agent is an MCP client, not a caller of local functions**, so the capabilities are usable by any host and the tool boundary is a real contract.
3. **Row limits are enforced at the AST level**, not by prompting the model to include `LIMIT`. A model instruction is not an enforcement mechanism.
4. **Schema retrieval is fine-tuned rather than over-retrieved**, because dumping more candidate columns into the prompt trades a retrieval problem for a context-precision problem and degrades generation.

## 6. Tradeoffs accepted

| Decision | Gained | Paid |
|---|---|---|
| Four separate MCP servers | Clean capability boundaries; independent retry/permission policy | Process overhead; more moving parts to deploy |
| Bi-encoder retrieval | Precomputable, fast, indexable | Lower ceiling than a cross-encoder reranker |
| pgvector over a dedicated vector DB | One datastore, one backup story, transactional consistency with the catalog | Fewer ANN tuning knobs at very large scale |
| AST validation before execution | Catches most invalid SQL with zero database load | sqlglot's dialect coverage becomes a dependency |
| SSE over WebSockets | Simpler; unidirectional is all this needs; survives proxies | No client→server messages mid-stream |

## 7. Scalability

> **TBD — Stage 6**, with numbers from the load tests in [../development/TESTING.md](../development/TESTING.md).

Planned analysis:

- **Stateless agent** — sessions live in a store, not in process memory, so the API layer scales horizontally.
- **Connection pooling** — `execute_sql` is the contended resource. Pool sizing bounds concurrent queries; excess requests queue rather than exhausting the database.
- **Embedding index** — HNSW build time and memory grow with schema size; measured on the largest BIRD schema.
- **Bottleneck ranking** — expected order: LLM generation latency ≫ query execution > retrieval > validation. To be confirmed rather than assumed.
- **Backpressure** — SSE streams hold connections open for the request's lifetime; concurrent-stream limits are needed before this is production-shaped.
