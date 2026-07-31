# Engineering Decisions

An append-only log. Each entry records what was decided, what else was considered, and what was traded away. Entries are written **when the decision is made** — a rationale reconstructed six weeks later is a rationalization.

Status values: `accepted` · `superseded by ADR-NNN` · `revisit at Stage N`

---

## ADR-001 — PostgreSQL as the only datastore

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Context.** The system needs relational target data, vector search over schema embeddings, session state, and an audit log.

**Decision.** One PostgreSQL 16 instance for all four, with pgvector for the vector workload.

**Alternatives.**
- *SQLite* — the Spider benchmark ships SQLite databases, so it is the path of least resistance. Rejected: no real role system, so the read-only containment story — the security centrepiece of this project — cannot be demonstrated. A single-file database with no privilege model makes "bounded blast radius" unprovable.
- *Postgres + a dedicated vector DB (Qdrant/Weaviate)* — better ANN tuning. Rejected: a second datastore adds a deployment dependency, a second backup story, and cross-store consistency problems, for a corpus of a few thousand vectors.

**Tradeoff.** Fewer ANN knobs, and a Spider→Postgres load step that SQLite would not need. Bought: a real privilege model, one backup, transactional consistency between the catalog and its embeddings.

---

## ADR-002 — Validation and execution are separate MCP servers

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Context.** The self-correction loop may generate and check three or four candidate queries before one is correct.

**Decision.** `validate_sql` (sqlglot AST + `EXPLAIN`, never executes) and `execute_sql` (runs under the read-only role) are distinct servers with distinct contracts.

**Alternatives.**
- *One `run_sql` tool with a `dry_run` flag* — fewer moving parts. Rejected: it makes an operation's safety a runtime argument rather than a property of the capability. A caller that omits the flag executes; a boolean is a weak boundary for the difference between "free to retry" and "costs a real query."
- *Validate inline in the agent, expose only execution* — rejected: validation stops being independently callable by another MCP host, and the retry loop stops being observable as tool calls in a trace.

**Consequences.**
- Validation can be retried freely; execution cannot. The policies live with the capabilities.
- `execute_sql` still re-runs validation internally — it must not assume a well-behaved caller, since another host can call it directly.
- Traces show the validate→fail→revalidate→execute sequence explicitly, which is exactly the behaviour worth demonstrating.

**Revisit:** no.

---

## ADR-003 — MCP for the tool boundary

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Context.** The four capabilities could be plain Python functions the agent imports.

**Decision.** Expose them as MCP servers; make the agent an MCP client with runtime tool discovery.

**Alternatives.**
- *Direct function calls* — simpler, faster, no IPC. Rejected: nobody else can run the capabilities, and the tool boundary stops being a designed contract.
- *REST endpoints per capability* — usable by others, but no discovery. The agent would need a hardcoded client per endpoint, which is the thing being avoided.

**Tradeoff.** Process management, IPC latency, and a protocol dependency. Bought: anyone can point Claude Desktop at these servers and query their own database, and the tool contracts become real interface-design work rather than function signatures.

**Note.** The risk here is shipping "three functions in a protocol wrapper." The mitigation is contract quality — descriptions that say *when* to call, schema-enforced limits, structured errors the agent can act on. See [MCP.md](MCP.md) §3.

---

## ADR-004 — sqlglot for AST validation

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Decision.** Parse generated SQL with sqlglot and inspect the AST before the database sees it.

**Alternatives.**
- *Regex / keyword blocklist* (`if "DROP" in sql.upper()`) — rejected outright. Blocklists on a language with comments, string literals, and nested statements are defeated by construction, and they reject legitimate queries containing the word in a string.
- *`EXPLAIN` only, no parse* — rejected as the sole check: `EXPLAIN` on a stacked statement or a DML statement is not the safe operation it appears to be, and a syntax error from the planner is less actionable than one with an AST position.
- *`pglast`* (real libpg_query bindings, so exact Postgres grammar) — genuinely more accurate. Rejected: heavier install story, and sqlglot's dialect breadth is useful for the Spider/BIRD ingestion path.

**Why AST, not string checks.** The AST answers structural questions directly and reliably: is this exactly one statement, is the root node a `SELECT`, which identifiers does it reference, does it already have a `LIMIT`. It is also where `LIMIT` gets *injected* — see ADR-005.

**Tradeoff.** sqlglot's Postgres coverage is very good but not identical to the real grammar. Mitigation: `EXPLAIN` runs after the AST checks as the authoritative second opinion. Neither layer is trusted alone.

---

## ADR-005 — Limits enforced at the AST level, not by prompting

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Decision.** Row limits are applied by rewriting the AST server-side. Statement timeouts are set on the role *and* per transaction.

**Rejected.** Instructing the model to "always include `LIMIT 500`." A prompt is a request, not an enforcement mechanism. It will hold most of the time, which is worse than failing loudly — the one time it does not, an unbounded result set reaches the client.

**Consequence.** If a query already carries a smaller `LIMIT`, the smaller wins. `truncated` is returned to the caller so a clipped result is never presented as complete.

---

## ADR-006 — Fine-tune the schema linker rather than retrieve more candidates

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0 · **Validated:** Stage 5

**Context.** Schema linking is the dominant failure mode on large schemas. Raising `k` is the free alternative to training anything.

**Decision.** Fine-tune a sentence-transformer with contrastive learning on question→column pairs; measure Recall@k against the off-the-shelf baseline.

**Why not just raise k.** It trades a retrieval problem for a context-precision problem. Forty candidate columns instead of ten does raise Recall@k — and gives the generator forty chances to pick a plausible wrong column. Precision at the generation step degrades, and prompt cost rises on every single query. Retrieval quality is the thing to fix; k is a knob that hides the problem.

**Falsifiable.** If the ablation shows the fine-tuned retriever does not beat baseline-at-equal-k, this decision was wrong and gets recorded as such in [../ml/TRAINING.md](../ml/TRAINING.md). Committing to a number before measuring it is the point of having a baseline stage.

**Tradeoff.** A training pipeline, checkpoint management, and a `model_version` dimension in the embeddings table. Bought: a measured ML contribution rather than a hyperparameter.

---

## ADR-007 — FastAPI for the HTTP layer

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Decision.** FastAPI + uvicorn.

**Rationale.** Native async (the whole I/O path is async), Pydantic validation shared with the settings layer, and OpenAPI generation that keeps [API.md](API.md) honest. `sse-starlette` handles SSE framing without hand-rolling it.

**Alternatives.** Flask (sync-first; async is bolted on, and this workload is I/O-bound end to end). Litestar (fine, smaller ecosystem, no advantage that matters here).

---

## ADR-008 — SSE instead of WebSockets

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Decision.** Stream agent progress over Server-Sent Events.

**Rationale.** The data flow is genuinely unidirectional — server pushes progress, client listens. SSE is plain HTTP: it survives proxies and load balancers that mishandle WebSocket upgrades, reconnects natively via `EventSource`, and needs no separate protocol handling.

**Rejected.** WebSockets — bidirectional capability this does not need, plus upgrade handling, ping/pong keepalives, and more infrastructure that can go wrong.

**Tradeoff accepted.** The client cannot send messages mid-stream, so "cancel this query" and "actually, filter by region instead" require a separate HTTP call rather than an inline message. Acceptable now; **revisit at Stage 6** if interactive steering becomes a requirement.

---

## ADR-009 — Anthropic SDK, `claude-opus-5`

**Status:** ~~accepted~~ **superseded by [ADR-014](#adr-014--provider-agnostic-llm-behind-an-llmclient-port)** · **Date:** 2026-07-26 · **Stage:** 0

> Superseded the same day: no Anthropic API key is available, so pinning a single vendor SDK made the Stage 1 core loop undemoable. The reasoning below about *model capability* still holds and is why Anthropic remains a supported adapter — what changed is that the provider is now a runtime choice rather than a hardcoded dependency.

**Decision.** The official `anthropic` Python SDK, default model `claude-opus-5`, adaptive thinking enabled.

**Rationale.** The workload is long-horizon agentic tool use — decompose, call tools, read errors, correct, synthesize — which is what this model tier is strongest at. Adaptive thinking lets the model spend reasoning where the question is hard without a fixed budget to tune.

**Note.** The model is a config value (`LLM_MODEL`), not a constant, so the eval harness can sweep models and record cost/accuracy tradeoffs per [../ml/EVALUATION.md](../ml/EVALUATION.md). **Do not lower the default to save cost without a measured accuracy comparison** — that is a benchmark result, not a default.

---

## ADR-010 — Python 3.12

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Context.** 3.11, 3.12, 3.13, and 3.14 are all installed on the dev machine.

**Decision.** Pin 3.12 in `.python-version` and `requires-python = ">=3.12,<3.13"`.

**Evidence.** A `pip install --dry-run` of the full stack (mcp, fastapi, uvicorn, sqlglot, psycopg, pgvector, sentence-transformers, torch, datasets, opentelemetry, sse-starlette, pytest) resolves cleanly on **3.12, 3.13, and 3.14 alike** — 99 packages, every one a binary wheel, torch 2.13.0 on all three. Wheel availability does not distinguish them.

**So the reason is narrower than it first appears.** What separates them is the long tail the dry-run did not cover:
- The **Spider/BIRD eval harnesses** are unmaintained, unpinned research repos, and they are load-bearing for the Stage 2 baseline. They are the most likely thing to break on a new interpreter.
- `python:3.12-slim` is the best-trodden Docker base image, which matters for [../operations/DEPLOYMENT.md](../operations/DEPLOYMENT.md).

3.13 is close to a coin flip and would be a defensible choice. **3.14 is the one to avoid**, on ecosystem-maturity grounds — not on wheel availability, which is fine.

**Revisit:** Stage 6, when the Docker image is built.

---

## ADR-011 — HNSW over IVFFlat for the vector index

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Decision.** HNSW index on `schema_elements.embedding`.

**Rationale.** Better recall at a given latency, and no training step — IVFFlat needs a populated table before its lists can be built, which complicates the bootstrap path. HNSW's higher build cost and memory footprint are irrelevant at this corpus size (thousands of elements).

**Revisit:** if a target schema turns out to be far larger than expected.

---

## ADR-012 — Documentation written per stage, not up front

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Context.** All 28 documents are scaffolded from day one.

**Decision.** Each is created as a structured stub with an explicit `TBD — Stage N` marker, and filled in when its stage lands.

**Rationale.** Documents whose content is *design-time knowledge* (this file, MCP contracts, security threat model, code style, glossary) can be written now and are. Documents whose content is *measurement* (benchmarks, performance, training results, evaluation) cannot be — writing them up front produces plausible fiction that then has to be found and corrected. The stub keeps the structure stable without inviting invented numbers.

**Consequence.** A `TBD` marker in this repo is a real signal, not filler. If a section is unmarked, its content is believed accurate.

---

## ADR-013 — Dependencies pinned in `requirements.txt`, not `pyproject.toml`

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0

**Context.** `pyproject.toml` exists for `requires-python`, packaging, and tool configuration. The conventional next step is to also declare runtime dependencies in `[project].dependencies`.

**Decision.** `[project].dependencies` is left empty. Exact pins live solely in `requirements.txt`.

**Alternatives.**
- *Abstract ranges in `pyproject.toml` + exact pins in `requirements.txt`* — the standard application pattern, and correct for a team with a lockfile tool. Rejected here: it creates two lists of the same dependencies that must be kept consistent by hand, and the failure mode is silent drift.
- *Everything in `pyproject.toml`, no `requirements.txt`* — rejected: `pip install -r` against exact pins is the reproducibility record for every number in [../ml/BENCHMARKS.md](../ml/BENCHMARKS.md). A benchmark run against a floating range is not reproducible.

**Consequence.** `pip install -e . --no-deps` installs the package for imports; `pip install -r requirements.txt` installs the environment. Two commands instead of one, and one source of truth instead of two.

**Revisit:** if the project adopts `uv` or Poetry, whose lockfiles make the dual declaration safe.

---

## ADR-014 — Provider-agnostic LLM behind an `LLMClient` port

**Status:** accepted · **Date:** 2026-07-26 · **Stage:** 0 · **Supersedes:** ADR-009

**Context.** No Anthropic API key is available, and none is budgeted. A hardcoded vendor SDK makes the Stage 1 core loop undemoable. Separately, [EVALUATION.md](../ml/EVALUATION.md) requires sweeping models to record the cost/accuracy tradeoff — which a single-vendor binding also prevents.

**Decision.** The agent depends on an `LLMClient` **protocol** defined in `core/`, never on a vendor SDK. Concrete adapters are selected at startup by `LLM_PROVIDER` and injected. This is the Dependency Inversion Principle applied literally: the high-level agent policy and the low-level vendor detail both depend on the abstraction, and neither depends on the other.

```
        agent  ──depends on──▶  LLMClient (Protocol, core/)
                                     ▲
                    ┌────────────────┼────────────────┐
          OpenAICompatibleAdapter   AnthropicAdapter  FakeLLMClient
                    │                                      └─ used by every test
      base_url selects: Groq · OpenRouter · Cerebras
                        Gemini · Ollama · LM Studio
```

**Two adapters, not seven.** Nearly every provider now exposes an OpenAI-compatible `/chat/completions` endpoint, so one adapter parameterized by `base_url` covers Groq, OpenRouter, Cerebras, Gemini's compatibility endpoint, and local Ollama / LM Studio. Writing one class per vendor would violate DRY for no gain — the differences are configuration, not behaviour. A second native adapter exists for Anthropic because its tool-use and thinking surface is genuinely different, and it is the target if a key becomes available.

**Alternatives.**
- *LangChain / LiteLLM as the abstraction* — rejected. Both are large dependencies that would own the agent loop, and the tool-boundary design is the substance of this project ([ADR-003](#adr-003--mcp-for-the-tool-boundary)). Delegating it to a framework hides exactly the work worth showing. A protocol plus two adapters is ~150 lines and fully under test.
- *Hardcode one free provider* — rejected: free tiers change terms and rate limits, and it reintroduces the same lock-in one vendor later.

**Consequences.**
- **The `FakeLLMClient` falls out for free.** It is just another implementation of the port, which is what makes the deterministic test strategy in [TESTING.md](../development/TESTING.md) §1 possible without mocking a vendor SDK's internals.
- **Tool-calling support varies by provider and model.** The agent must degrade to prompt-based structured output where native tool calling is absent. The port exposes a `supports_tool_calling` capability flag rather than assuming.
- Model quality varies enormously across free tiers, so **execution accuracy must be reported per provider/model** in [BENCHMARKS.md](../ml/BENCHMARKS.md), never as a single number.
- Two new attack surfaces are introduced and are handled in [SECURITY.md](../operations/SECURITY.md) §14: **SSRF via a configurable `base_url`**, and **third-party data exposure** when schema and sampled row values are sent to a free-tier provider that may train on them.

**Revisit:** never — provider independence is now a design property, not a workaround.

---

## ADR-015 — HNSW iterative scan is always on, and is not configurable

**Status:** accepted · **Date:** 2026-07-31 · **Stage:** 1

**Context.** Every retrieval query filters on `(dataset, model_version)` — mandatory, because vectors from different models are not comparable and mixing them degrades retrieval without erroring ([ADR-011](#adr-011--hnsw-over-ivfflat-for-the-vector-index), [DATABASE.md](DATABASE.md) §3).

That predicate reads like a pre-filter. It is not. `EXPLAIN` shows it as a `Filter` applied to rows the HNSW scan has already returned, and with pgvector's default `hnsw.iterative_scan = off` the scan stops once its candidate list is exhausted. A filter that discards most candidates therefore leaves fewer than `k` rows — silently. Measured at 6 rows for `k=10` across two datasets, and **0 of 10** when the filter correlates with position in vector space, which is the normal case rather than the exotic one: a second dataset has its own vocabulary, and a re-index under a new `model_version` puts an entire second corpus in its own region by construction. Full measurements in [DATABASE.md](DATABASE.md) §5.1.

**Decision.** `SchemaRetriever` sets `hnsw.iterative_scan = relaxed_order` on every search. It is **not** exposed as a configuration variable. Support is detected by querying `pg_settings`; on pgvector older than 0.8 the retriever logs a warning at construction rather than degrading silently.

**Alternatives.**
- *Leave pgvector's default* — rejected. It returns fewer results than requested with no error, and Recall@k is the ceiling on execution accuracy, so the damage lands on the project's primary metric while presenting as nothing.
- *Expose `HNSW_ITERATIVE_SCAN` as a setting* — rejected, and this is the substantive part of the decision. It reads like a performance knob and is a correctness one. Someone tuning p95 would turn it off and be *correct about latency*; retrieval quality would drop, and the symptom would surface two components downstream as the model referencing columns it was never shown — where it would be diagnosed as a prompt problem.
- *`strict_order`* — rejected. It buys an ordering guarantee that is already provided more cheaply: results are re-sorted by score in application code, so the ordering `strict_order` pays for is discarded.
- *Parse `extversion` to detect support* — rejected. String comparison of version numbers is wrong at the edges, and setting an unknown GUC under a registered prefix warns rather than fails, so a naive attempt would hide the degradation in exactly the deployment that has it.

**Tradeoff.** `relaxed_order` deliberately increases worst-case work per search — correctness bought with availability, bounded by pgvector's own `hnsw.max_scan_tuples`. `ef_search` remains configurable (`HNSW_EF_SEARCH`) because *its* wrong setting is visible as latency or as measurable recall.

**Generalises to:** a knob whose wrong setting fails silently should not be a knob.

**Revisit:** if the cost of `relaxed_order` at a realistic corpus size proves material under the Stage 6 performance work.
