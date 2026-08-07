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

**Tradeoff.** Process management, IPC latency, and a protocol dependency. Bought: anyone can point *any* stdio-speaking MCP host at these servers — including the client this project ships — and query their own database, and the tool contracts become real interface-design work rather than function signatures.

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

> Superseded the same day: no Anthropic API key is available, so pinning a single vendor SDK made the Stage 1 core loop undemoable. The reasoning below about *model capability* still holds; what changed is that the provider became a runtime choice rather than a hardcoded dependency. **A native Anthropic adapter is designed and unbuilt**, and its SDK is no longer pinned — see [ADR-014](#adr-014--provider-agnostic-llm-behind-an-llmclient-port) and the free-and-open-source constraint in [PROJECT.md](../../PROJECT.md).

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

**Two adapters, not seven.** Nearly every provider now exposes an OpenAI-compatible `/chat/completions` endpoint, so one adapter parameterized by `base_url` covers Groq, OpenRouter, Cerebras, Gemini's compatibility endpoint, and local Ollama / LM Studio. Writing one class per vendor would violate DRY for no gain — the differences are configuration, not behaviour. A second native adapter is *designed* for Anthropic, whose tool-use and thinking surface is genuinely different, and remains unbuilt: its SDK is no longer pinned either, because a dependency nothing imports, for a provider with no free tier, is the wrong default under the constraint in [PROJECT.md](../../PROJECT.md).

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

---

## ADR-016 — A frequency threshold, not a PII regex, decides which values a profile may reveal

**Status:** accepted · **Date:** 2026-08-01 · **Stage:** 1

**Context.** `profile_table` is the only component in this system whose output is row data *by design*. Everywhere else, real values either stay in the database (execution results, the audit log) or stay in a store the operator controls (the catalog's sampled values, which are persisted but never rendered into a prompt — [SECURITY.md](../operations/SECURITY.md) §14.2.5). A profile is made in order to be shown to a language model, which sends it to a third-party provider.

So the risk cannot be designed away. An agent cannot write `WHERE country = 'FI'` without learning that the column stores `'FI'` rather than `'Finland'`, and refusing all values pushes the model into guessing — which produces confidently wrong SQL rather than a visible failure. The question is not *whether* to send values but **which values carry the disambiguation signal without carrying a record with them.**

[SECURITY.md](../operations/SECURITY.md) §14.2 had proposed the obvious answer: detect and redact PII patterns before values enter a prompt.

**Decision.** Reject regex redaction. A value may be reported only if it occurs at least `PROFILE_MIN_VALUE_FREQUENCY` times (default 5) in the scanned rows — the small-cell rule from statistical disclosure control. A value seen once identifies whoever it belongs to; a value seen five hundred times is a category label. The floor of 2 is enforced in the type, so no deployment can configure a unique value into being reportable.

Two consequences follow, and both are the point:

- **Frequent values are on by default** while raw sampling stays off. Those look inconsistent until you notice they return different *kinds* of thing.
- **The threshold is the only gate that covers the residual case** — a secret in an innocuously-named column like `notes`, which the name-based denylist is openly admitted to miss.

**Alternatives.**
- *Regex PII redaction* — rejected, and this is the substance of the ADR. A pattern list catches `123-45-6789` and misses a customer name, an internal project codename, or a free-text complaint. Worse, **the appearance of a filter invites relying on it**: an operator who sees "PII is redacted" reasonably stops auditing columns, and the control is weakest exactly where they have stopped looking. A frequency threshold is a property of the data rather than of a pattern list, so it does not depend on having anticipated the format of the secret.
- *Return no values at all, only derived statistics* — rejected. It is the safest option and it removes the tool's reason to exist. Null fraction and distinct count answer "is this column populated?"; they do not answer "is the value `FI` or `Finland`?", which is the question that blocks a correct query.
- *Name-based denylist alone* — rejected as sufficient, kept as a layer. It is a heuristic over names and the risk is in values. Retained because it is the only control that stops the read from happening at all, which matters for columns where even one value in process memory is too many.
- *`k`-anonymity over the whole row rather than per column* — rejected as premature. It is the more rigorous framing, it requires deciding what a quasi-identifier is per schema, and there is no configuration surface for that yet. Named here so the gap is deliberate.

**Tradeoff.** The threshold does not catch a value that is both sensitive and common — a diagnosis code appearing 400 times in a column called `code` passes every gate and will be reported. Redaction would have caught a *frequent* SSN-shaped value; the threshold does not. That residual is real, is documented in [SECURITY.md](../operations/SECURITY.md) §14.2.6, and its honest mitigation is local inference rather than a better filter.

A second, smaller trade: `min`/`max` were described as statistics in the original threat model and are returned only for numeric and temporal types here. The lexicographic minimum of a `name` column is a verbatim cell, and calling it a statistic would have let real values out under a heading saying they were safe.

**Generalises to:** a control that is easy to describe and hard to bound ("we filter PII") is worse than a control that is narrow and provable ("a value under this frequency is never emitted") — because the first one changes operator behaviour and the second one does not.

**Revisit:** when Stage 4 sends query *results* to a model for synthesis. That path has no frequency to threshold on, so it needs a different answer rather than this one stretched.

---

## ADR-017 — Servers claim stdout, and validate arguments themselves

**Status:** accepted · **Date:** 2026-08-01 · **Stage:** 3

**Context.** Exposing four capabilities over stdio adds two failure modes the components underneath do not have, and both produce errors that name nothing about their cause.

**stdout is the JSON-RPC channel.** Anything else written there is a protocol violation. The host reports a JSON decode error; the actual cause is a `print` somewhere in the process — possibly in a dependency, possibly left behind after debugging.

**The SDK's catch-all returns `str(exc)`.** Any exception a handler raises becomes tool content the model reads. For a `psycopg` error that string can carry a connection string with its password, which is exactly what [MCP.md](MCP.md) §6 forbids crossing this boundary.

**Decision.** Three things, all in one shared module so a fifth server inherits them.

1. **`claim_stdout()` at startup.** The real stdout is handed to the transport and `sys.stdout` is repointed at stderr. A stray write becomes a log line instead of a corrupted stream. Logging is configured with `force=True` for the same reason — `basicConfig` is a no-op once any handler exists, so a library that configured logging onto stdout first would otherwise keep it.
2. **The dispatcher catches every exception before the SDK can.** Domain exceptions pass their message through, because those messages were written for the agent to read. Everything else becomes a fixed generic message, with the real exception on stderr.
3. **Arguments are validated in the dispatcher, not by the SDK's `validate_input`.** Same library, same schema; what changes is the shape of the failure.

**Alternatives.**
- *Rely on discipline for stdout* — rejected. The rule is easy to state and impossible to enforce by review, because the offending `print` may be in a dependency. A guard costs three lines and converts a session-killing failure into noise.
- *Let the SDK validate input* — rejected, and this is the substantive part. The SDK returns a bare text message on a schema violation, so that one failure would reach the agent in a different shape from every other failure. The agent dispatches on `error_type`; a hole in that dispatch at exactly the point where a *model* most often gets things wrong is the worst possible place for one. Validating in the dispatcher gives `invalid_arguments` in the standard envelope, naming the offending field.
- *Return schema violations as protocol errors* — rejected, and it required correcting [MCP.md](MCP.md) §6, which had filed "malformed params" under protocol errors. The arguments are written by a model, so an out-of-range value is an ordinary correctable mistake rather than a caller bug. A protocol error kills the call and gives the agent nothing to read.
- *Let handlers return dicts and accept the SDK's result construction* — rejected once measured. Returning a dict makes the SDK set `isError: false` unconditionally, so every tool failure would arrive flagged as a success. Handlers still return dicts; the dispatcher builds the result.

**Tradeoff.** Reimplementing argument validation is duplication in principle — mitigated by calling the same `jsonschema` with the same schema, so it is a wrapper rather than a second implementation. `jsonschema` becomes a direct dependency and is pinned in `requirements.txt` rather than relied on transitively through `mcp`.

Claiming stdout also means a genuinely useful `print` during development goes to stderr, which is mildly surprising. Stated in the module docstring; the alternative is worse.

**Generalises to:** when a library's default error path is *more* informative than your own, that is usually a leak, not a feature.

**Revisit:** when the Streamable HTTP transport lands. The stdout guard is stdio-specific and becomes inert there; the error contract is not, and must apply identically.

---

## ADR-018 — Result comparison rounds floats rather than applying a tolerance

**Status:** accepted · **Date:** 2026-08-01 · **Stage:** 2

**Context.** [EVALUATION.md](../ml/EVALUATION.md) §1.1 specifies float equality "within 1e-6", to absorb the drift that aggregation ordering produces. The obvious implementation is `abs(a - b) < 1e-6`.

It does not work, for a reason that has nothing to do with floats: **a tolerance-based equality is not transitive.** With a=1.0000000, b=1.0000005 and c=1.0000010, a≈b and b≈c but a≉c.

Two of the comparison rules require transitivity. Row order is ignored unless the gold query is ordered, so rows are compared as a **multiset** — which means hashing them. Column order and column *names* are both ignored, so columns can only be matched by content, which means sorting and grouping them. Neither operation is well-defined over a relation that is not an equivalence relation: the answer would depend on the order the rows happened to arrive in.

A benchmark whose verdict depends on row arrival order is not a benchmark.

**Decision.** Canonicalise every numeric value by rounding to 6 decimal places, then compare canonical forms with `==`. Transitive by construction, hashable, and sortable.

`int`, `float` and `Decimal` are unified in the same step, because `SUM(x)` returns a `Decimal` where `x` returns an `int` and no meaningful difference is being hidden. Strings are **not** unified with numbers: a query returning `'1'` where the reference returns `1` has a real defect, and the strict reading catches it.

**Alternatives.**
- *`math.isclose` / `abs(a-b) < tol`* — the specification's literal reading. Rejected above. Worth naming precisely because it is what anyone would write first, and the failure is silent: it produces slightly wrong scores on some runs and correct ones on others.
- *Sort, then compare adjacent pairs with a tolerance* — restores a usable ordering but not equality; two multisets that should match can still disagree depending on which representative each ended up next to.
- *Compare as strings with fixed formatting* — equivalent to rounding, and loses the ability to unify `Decimal('1.0')` with `1`.
- *Round to more places than the tolerance implies* — rejected as false precision. The two numbers should say the same thing, and 6 is what §1.1 already committed to.

**Tradeoff.** Rounding and a tolerance disagree for pairs straddling a rounding boundary: 1.0000004 and 1.0000006 differ by 2e-7 and are "equal" under a tolerance but not under rounding. Rounding is therefore the **stricter** of the two at the margin, which understates accuracy rather than inflating it — the safe direction for a correctness metric, and the direction a portfolio project should err in.

**Consequences worth recording.** Implementing this surfaced two rules §1.1 did not cover, both now added there: numeric-vs-string is distinct, and **boolean-vs-number is distinct**. The second needed real care — `bool` is a subclass of `int` *and* `True == 1.0` is true in Python, so skipping the numeric branch is not sufficient; the value has to be tagged or a boolean column silently matches a numeric one.

**Generalises to:** when a specification says "equal within ε", check what the code needs to *do* with equality. If it sorts, groups, or hashes, ε is not available and the specification means something it did not say.

**Revisit:** if a benchmark appears whose correct answers genuinely differ below 1e-6, which would make the whole rule wrong rather than its implementation.

---

## ADR-019 — Benchmark identifiers are folded to lower case, and ambiguity is refused

**Status:** accepted · **Date:** 2026-08-01 · **Stage:** 2

**Context.** Spider and BIRD ship SQLite databases whose table and column names are mixed case — `Stadium`, `Singer_ID`, `Order Details`. Their gold SQL is written for SQLite, where identifiers are **case-insensitive**: `SELECT Name FROM Stadium` and `SELECT name FROM stadium` are the same query.

PostgreSQL folds *unquoted* identifiers to lower case and then matches case-sensitively. So a converted schema that preserves `Stadium` can only be reached by a query that writes `"Stadium"` — and no gold query quotes anything.

**Decision.** Fold every source identifier to lower case, quote it with `sql.Identifier` at every composition site, and **refuse** any name that cannot be represented unambiguously rather than rewriting it.

Refused: names containing a double quote, a backslash, a control character, or any non-ASCII byte; empty names; and anything over PostgreSQL's 63-byte limit.

**Alternatives.**
- *Preserve case and quote both sides* — correct only if every gold query quotes every identifier. None do, so this fails every question.
- *Rewrite the gold SQL to quote identifiers* — moves the problem into a SQL rewriter that has to resolve every bare name against a schema, and gets the ambiguous cases wrong silently.
- *Sanitise unusable names into something safe* — the tempting one. `Order#1` and `Order.1` both become `order_1`, two tables merge, the load succeeds, and every question about either is scored against the wrong data. **Rejected on the grounds that the failure is invisible.**
- *Truncate names over 63 bytes* — this is what PostgreSQL itself does, which is exactly the problem: two long names sharing a prefix truncate to the same identifier and the server does not warn.

**Tradeoff.** A database with an unrepresentable name is refused entirely rather than partially converted. That costs coverage — one database out of two hundred — and buys the guarantee that a converted database means what the benchmark meant. The loader names which database and which identifier, so the cost is legible rather than mysterious.

**Consequences worth recording.** The collision guard this was designed around turned out to be unreachable from SQLite: SQLite compares table and column names case-insensitively too, so a source database holding both `Song` and `song` cannot exist. The guard stays — it is cheap, and it still covers a set of `db_id`s assembled from directory names on a case-sensitive filesystem — but it is defence *behind* the source engine rather than the only thing standing there. A test asserts the SQLite behaviour rather than a docstring asserting it.

The length case, by contrast, *is* reachable: SQLite has no identifier length limit at all.

**The refusal set was too narrow when it met real data, and that is a correction to this ADR rather than a footnote.** It was first written as the allowlist `[a-z0-9_ $-]`, which refused two whole Spider databases: `aircraft` over the column `%_Change_2007`, and `orchestra` over `Official_ratings_(millions)`. Neither `%` nor a parenthesis can escape `sql.Identifier`, which quotes whatever it is handed and doubles any quote inside it — so the narrow set bought no safety at all and cost data. **An allowlist doing usability work under a security label is worse than no allowlist**, because its cost is charged to the security argument and nobody re-examines it. What actually threatens the composition is a double quote (it can end the quoting), a backslash (`standard_conforming_strings` can be off), a control character (it can hide what a reviewer reads), and a non-ASCII byte (it is not decidably foldable). That is now the whole set, and it is small enough to justify each member.

**Generalises to:** when two systems disagree about identity, pick the stricter one and refuse the difference. Mapping the difference away produces a system that works until the day two things map to one.

**Revisit:** if a benchmark arrives whose schemas are genuinely non-ASCII, which would make refusal too expensive and force a documented transliteration with a collision check.

---

## ADR-020 — Benchmark archives are pinned by a committed lockfile, recorded on first use

**Status:** accepted · **Date:** 2026-08-01 · **Stage:** 2

**Context.** Every number this project reports is computed from a benchmark archive. Two ways that goes wrong, and they are unrelated.

A **silently different version**: Spider and BIRD have both been re-released with corrections. A run against March's `dev.json` and a run against September's are not comparable, and neither run records which one it used. No attacker required.

A **tampered archive**: the download is a zip that gets extracted onto the machine and a set of SQLite files that get parsed by a C library.

**Decision.** A `data/artifacts.lock.json` of observed SHA-256 digests, committed to the repository. The first acquisition records what it saw — explicitly, behind `--trust-on-first-use` — and every later one must match. Verification happens on the bytes on disk, *before* extraction, so a tampered archive never reaches the zip parser. The check runs even immediately after recording, which is what proves the recorded digest describes the file about to be extracted.

**Why the digests are not hardcoded in source.** They would be a fabrication. Nobody who wrote that file has downloaded these archives, and a constant claiming to be the SHA-256 of Spider without ever having been compared to one is *worse* than no constant: it fails every honest download and gets "fixed" by pasting in whatever the failing run reported — trust-on-first-use with extra steps and a false claim in the source tree.

**Alternatives.**
- *No verification* — the status quo almost everywhere. Rejected: this is A08, and the non-adversarial case is the common one.
- *Verify against a published checksum* — better, where one exists. Spider is distributed through Google Drive and BIRD through a project page; neither publishes a stable digest next to the file.
- *Re-download and compare* — costs gigabytes to detect a change the lockfile detects for free.
- *Update the lockfile automatically when it does not match* — this is the failure mode dressed as a feature. `record()` does not overwrite an existing entry, and the error message says explicitly not to edit it by hand to make a check pass.

**Tradeoff.** The first acquisition is trusted. That is unavoidable without a published digest, and it is made *visible*: it needs a flag, it logs a warning, and what it records is committed and reviewable in a diff. Trust-on-first-use is only dangerous when it is invisible.

**Also decided here.** There is no `--url` flag. Sources live in an allowlist in source code, so the thing being downloaded, extracted and parsed cannot be redirected by an argument an operator can be talked into.

**Generalises to:** pinning is a reproducibility control first and a security control second. The security framing gets it built; the reproducibility framing is why it earns its keep.

**Revisit:** if either benchmark starts publishing signed releases, in which case signature verification replaces first-use recording.

---

## ADR-021 — Splits are a hash of the database name, not a seeded shuffle

**Status:** accepted · **Date:** 2026-08-01 · **Stage:** 2

**Context.** [DATASETS.md](../ml/DATASETS.md) §5 requires splitting **by database, never by question** — a question-level split puts the same tables in train and eval, and the resulting Recall@k is high and meaningless. It also requires the assignment to be committed as a file.

The natural implementation is `random.Random(seed).shuffle(db_ids)` and slice.

**Decision.** Assign by hashing the database name: `blake2b(f"{seed}:{db_id}")` into a bucket, with fixed band boundaries deciding the split.

**The reason.** A seeded shuffle is reproducible only while the *input list* is unchanged. Add one database, load Spider and BIRD in the other order, or change how the list is sorted, and every database after the insertion point can move to a different split. Held-out databases become training databases; the split file looks exactly as deterministic as it did before; nothing downstream can detect it.

Hashing each name independently makes membership a property of the name alone. Adding databases never moves the ones already assigned.

`blake2b` rather than `hash()`, which is randomised per process by `PYTHONHASHSEED` — the exact failure this is meant to prevent, arriving through the one function that looks like it cannot fail.

**Tradeoff.** Proportions are approximate at small corpus sizes. A 68/12/20 split is a fine split; a held-out set that quietly absorbed three training databases is not a split at all.

**Consequences worth recording.** The first implementation had `SMOKE` as "the five lowest-bucket dev databases" — a count, and therefore a rank *within a set*, which is precisely what the rest of the design rejects. Adding a database with a lower bucket displaces one that was already in smoke, and the per-commit regression check silently starts measuring different databases. A test written for the general stability property caught it. Smoke is now a sub-band of dev expressed as a fraction, so every split boundary is a constant.

**Generalises to:** "deterministic given a seed" and "stable under change" are different properties, and the first is usually the only one that gets tested.

**Revisit:** if a benchmark ships an official split, which should be used instead — comparability with published numbers beats internal consistency.

---

## ADR-022 — The conversion is verified by the eval harness's own comparator

**Status:** accepted · **Date:** 2026-08-01 · **Stage:** 2

**Context.** [DATASETS.md](../ml/DATASETS.md) §3 requires that the SQLite→PostgreSQL conversion be *verified, not assumed*: every gold query run on both engines, results compared. The open question was which notion of "the same" to use.

A conversion defect does not raise. A column that silently became `text`, a foreign key that changed a join's row count, a date format that reorders a `MAX` — each lowers an accuracy number weeks later, and the investigation that follows looks at the model.

**Decision.** Compare with `evals.comparison.compare` — the same function that will score the eval — rather than a stricter equality written for this purpose.

**The reason.** The question is not "are these two databases identical". They are not; one is SQLite. The question is "will the eval score a correct answer as correct on the converted copy", and only the thing that will do the scoring can answer it. A stricter comparison here fails conversions the eval would have been perfectly happy with; a looser one passes conversions the eval will mark wrong.

**Decided alongside it.**
- A gold query that fails on its *own* SQLite database is a `gold_error` — a benchmark defect — and is excluded from the denominator rather than counted against the conversion. Same rule EVALUATION.md §5 already applies to scoring.
- A transpile failure is its own outcome, distinct from a mismatch. One says the benchmark holds a query this project cannot parse; the other says the data moved. Collapsing them sends the investigation to the wrong component.
- `verified` requires **every** comparable query to agree, not most of them. One disagreement is a class of data that moved, and which questions it affects is unknown until someone looks.
- The CLI exits **3** when a database fails verification, distinct from exit 1 for a tool failure. A verification failure that exited 0 would let a CI step pass while reporting that the data is wrong.

**Tradeoff.** Verification costs a full execution of every gold query on both engines — the most expensive thing the loader does, and it has to be re-run after any conversion change. `--per-database` caps it for a fast check; the full run is what licenses a published number.

**Generalises to:** verify a transformation against the *consumer's* definition of equality, not the strictest one available. The strictest one reports differences nobody would ever have noticed and hides the fact that you never checked the ones that matter.

**Consequences worth recording.** Five outcomes became seven when the first real corpus ran. `dialect_error` and `ambiguous_order` were both cases the original five *misattributed to the conversion* — see [ADR-026](#adr-026--gold-sql-is-repaired-for-sqlites-quoted-literal-rule-and-dialect-gaps-are-not-conversion-faults) and [ADR-027](#adr-027--an-undetermined-result-order-is-not-a-mismatch--in-verification-only). The lesson is about the taxonomy rather than either bug: **a classification with a bucket named after the component under test will absorb everything unexplained**, and every absorbed case reads as evidence against that component.

**Revisit:** never, while the comparator and the verifier share a definition. If they diverge, this ADR is the thing that broke.

---

## ADR-023 — An unrepresentable archive name is skipped and recorded; an escaping one refuses the archive

**Status:** accepted · **Date:** 2026-08-02 · **Stage:** 2

**Context.** [ADR-020](#adr-020--benchmark-archives-are-pinned-by-a-committed-lockfile-recorded-on-first-use) validates every zip member before a byte is written. The first implementation had one verdict: refuse the whole archive, and name the member. Then the real Spider archive arrived carrying `receipts (3:11:18, 5:53 PM)_original.csv`, and the entire benchmark was refused over a CSV the loader never reads — because a colon on Windows is a drive or alternate-data-stream separator.

**Decision.** Two verdicts, because there are two different facts:

| The member | Means | Verdict |
|---|---|---|
| Escapes the destination — `..`, absolute path, drive letter, backslash separator, symlink, special file | **The archive is not trustworthy.** Something in it is trying to write outside where it was told to | Refuse the whole archive |
| Cannot be named on *this* filesystem — a colon, `<>"\|?*`, a reserved device name, a trailing dot or space | **This filesystem cannot store it.** The same archive is fine on Linux | Skip it, record it in the extraction report |

With one exception, which is the part that matters: if the unrepresentable member is a **database file** (`.sqlite`, `.sqlite3`, `.db`), the archive is refused after all. Skipping it would silently change which databases exist in the corpus, and a corpus that quietly lost a database is exactly the "silently different dataset" failure ADR-020 exists to prevent.

**The reason for the split.** A traversal attempt is a statement about the *archive*. An unrepresentable name is a statement about the *host*. Collapsing them means either accusing a benign archive of an attack, or — far worse, and see below — excusing an attack as a portability problem.

**Alternatives.**
- *Keep one verdict and refuse.* What shipped. It makes the loader unable to read the benchmark it was written for, on the platform it was developed on.
- *Mangle the name into something representable.* Rejected for the same reason [ADR-019](#adr-019--benchmark-identifiers-are-folded-to-lower-case-and-ambiguity-is-refused) rejects sanitising identifiers: two names can mangle to one, and the collision is silent. Skipping is loud; renaming is not.
- *Skip everything unrepresentable, database files included.* Rejected — that is the silent-corpus-change case, and it would be invisible in every number computed afterwards.

**Consequences worth recording — a usability fix briefly disarmed the primary control.** The first version of this ran the representability check *before* the escape check. `..` is a path component ending in a dot; a trailing dot is unrepresentable on Windows; so a traversal member was being classified as a portability problem and **skipped instead of refused**. The whole traversal suite went red on the same run that fixed the colon, which is the only reason it was caught within a minute rather than at review.

Two things came out of it. The ordering in `acquire.extract` now carries a comment saying it is load-bearing, and the representability check explicitly excludes `.` and `..` because those are *path semantics*, not filenames. And a regression test asserts that a member which is both traversing and unrepresentable is refused rather than skipped.

**Generalises to:** when a check is relaxed to admit a benign case, the question is not "is the new case safe" but "what else now takes the new path". Adding a second verdict to a security check adds an edge the old test suite was not written to defend.

**Revisit:** if the loader ever runs somewhere with a materially different representability rule, in which case the rule belongs behind a platform interface rather than an `os.name` check.

---

## ADR-024 — Column types are inferred from SQLite's own `typeof()`, over the whole column

**Status:** accepted · **Date:** 2026-08-02 · **Stage:** 2

**Context.** SQLite does not enforce declared types, so the conversion infers each PostgreSQL type from the data ([DATASETS.md](../ml/DATASETS.md) §3). The first implementation read up to `BENCHMARK_TYPE_SCAN_ROWS` (200,000) rows per column and widened from what it saw.

Spider's `wta_1.rankings` has **510,437 rows and exactly one empty-string `player_id`, at rowid 1,593,272** — past the cap. The column was inferred `bigint`, the schema was created, `COPY` began, and the load died partway through on `invalid literal for int() with base 10: ''`, naming no database, table, column or row.

**Decision.** Ask SQLite. One statement per table:

```sql
SELECT group_concat(DISTINCT typeof("col1")), group_concat(DISTINCT typeof("col2")), … FROM "table"
```

`typeof()` returns the storage class of every value — the thing being inferred — and `DISTINCT` collapses it to the set. The answer is **exact and covers the whole column**, and it costs one scan per table rather than one per column.

`BENCHMARK_TYPE_SCAN_ROWS` is deleted rather than raised. It was never a tuning knob: every value it could hold other than "all of them" is a wrong answer waiting for a large enough table.

**Alternatives.**
- *Raise the cap.* Moves the failure to the next benchmark. BIRD's largest tables are bigger than Spider's.
- *Sample and widen defensively — infer `text` when unsure.* Turns every large numeric column into text, and then every gold query comparing it to a number fails. Trades a loud failure for a quiet one.
- *Catch the coercion failure during `COPY` and restart the table as `text`.* Doubles the load time for the affected table and leaves the schema decided by whichever row happened to be first. It also means the plan is no longer a plan.

**Tradeoff.** Inference now reads every row of every table instead of a prefix — on Spider, seconds. On a benchmark where that becomes expensive, the honest fix is a cheaper exact answer, not an inexact one.

**Consequences worth recording.** The failure that exposed this was *unhandleable by the operator*: `invalid literal for int() with base 10: ''` names nothing. Coercion failures now raise a `ConversionError` naming database, table, column and the offending value, so the residual case is diagnosable in one read.

**Generalises to:** a sample answers "what is in this data" only when the question tolerates being wrong. Schema inference does not — it is a claim about *every* row, and the one row that breaks it is by construction the row a sample is least likely to contain.

**Revisit:** if a corpus arrives where a full scan is genuinely too slow, at which point this becomes a documented, per-database opt-out with its inexactness recorded in the conversion report — not a global default.

---

## ADR-025 — A foreign key joining two types is unified toward the numeric side

**Status:** accepted · **Date:** 2026-08-02 · **Stage:** 2

**Context.** Spider declares `concert.Stadium_ID` as `TEXT` holding `'1'`, referencing `stadium.Stadium_ID`, an `INT` holding `1`. SQLite joins them: comparing a TEXT-affinity column to an INTEGER-affinity column applies **numeric affinity to the text operand**, so `'1' = 1` is true. PostgreSQL answers `operator does not exist: text = bigint`.

Measured across the full Spider corpus: **35 of 769 foreign keys, in 21 of 166 databases**, join two different inferred types. Every gold query that traverses one of them fails on the converted copy — not because the data moved, but because the two engines disagree about what the join *means*.

**Decision.** When a foreign key's two sides infer different types, give both the numeric type — but only if **every value on the text side converts losslessly**. If any does not, both sides keep their inferred types, the constraint is dropped, and both facts go in the conversion report.

**The direction is the decision, and it only goes one way.** Widening the numeric side to text would also make the join compile. It would also change which rows join: `'01' = 1` is **true** under SQLite's affinity rule and `'01' = '1'` is **false** as text. Unifying toward text produces a database that runs every gold query and silently returns fewer rows — the exact failure class [ADR-022](#adr-022--the-conversion-is-verified-by-the-eval-harnesss-own-comparator) exists to catch, arriving through the fix for a different problem.

**Alternatives.**
- *Leave the types alone and drop the constraint.* Loses the relationship for schema retrieval and join reasoning, on 21 databases, without fixing a single query — the gold SQL still compares text to a number.
- *Rewrite gold SQL to cast at the comparison.* A SQL rewriter that must decide, per comparison, which side to cast and in which direction. Same objection as ADR-019's rejected rewriter: it gets the ambiguous cases wrong silently.
- *Unify toward text (whichever side is text wins).* Rejected above. It is the version that always compiles.
- *Unify only when the declared types agree.* The declaration is exactly what SQLite does not enforce; that is the premise of ADR-024.

**Tradeoff.** A column the source declared `TEXT` becomes `bigint` in the converted copy, so the converted schema is not a faithful transcription of the source *declaration*. It is a faithful transcription of the source **semantics**, which is what is being measured. The report names every unification.

**Where it does not apply.** A dirty column — `'1'`, `'2'`, `'unknown'` — cannot be unified, because SQLite would coerce `'unknown'` to 0 in the comparison and no PostgreSQL type reproduces that. Types stay, constraint is dropped, both recorded. That is the honest outcome and it is rare.

**Generalises to:** when porting between engines, port the *behaviour the source exhibits*, not the schema the source declares — and when there are two ways to make something compile, pick by which one preserves the observable result, not by which one is less work.

**Revisit:** if a benchmark appears with text keys holding genuinely non-numeric values on both sides, which this leaves alone by construction.

---

## ADR-026 — Gold SQL is repaired for SQLite's quoted-literal rule, and dialect gaps are not conversion faults

**Status:** accepted · **Date:** 2026-08-02 · **Stage:** 2

**Context.** The first verification run over Spider dev reported **213 of 1034 questions** as `postgres_error` — a bucket whose meaning under ADR-022 is "the conversion produced a genuine type difference". Classifying all 213 by SQLSTATE showed every single one was `42703 undefined_column`, and every one of those was the same thing:

```sql
SELECT name FROM student WHERE course = "Math"      -- 42703: column "Math" does not exist
SELECT name FROM student WHERE name LIKE "%w%"      -- 42703: column "%w%" does not exist
```

**SQLite treats a double-quoted token that matches no column as a string literal.** It is a documented compatibility misfeature (`SQLITE_DQS`), the benchmark's gold SQL relies on it heavily, and PostgreSQL has no such fallback. Nothing about the conversion was wrong.

**Decision, part one.** Before transpiling, apply SQLite's own test: a double-quoted identifier that is unqualified and matches no table or column name in the schema becomes a string literal. Done on the parsed AST, not by regex.

The three guards are the whole design:
- **A quoted token that *is* a real name is left alone.** Spider has columns named `Official_ratings_(millions)`; rewriting one into a string turns a valid query into nonsense that still runs.
- **A qualified token — `s."zzz"` — is left alone.** SQLite's rule does not apply there either.
- **With no schema available, nothing is rewritten.** No evidence, no repair. Guessing without the name set is precisely the move that turns a real column into a string.

**Decision, part two.** Split PostgreSQL rejections by SQLSTATE, because they answer different questions:

| SQLSTATE | Outcome | Reasoning |
|---|---|---|
| `42P01` undefined table · `42703` undefined column · `3F000` invalid schema | `postgres_error` — **blames the conversion** | The names are what the conversion chose. If one is missing, the conversion is why |
| `42883` undefined function/operator · `42803` grouping error · `42804` datatype mismatch | `dialect_error` — **blames neither** | The gold query asks for something PostgreSQL does not offer. It would fail identically against a perfect conversion |

`dialect_error` is counted and **excluded from the denominator**, the same treatment `gold_error` already gets — because a question whose gold SQL has no PostgreSQL expression cannot be scored later either. Measured on Spider dev: 97 of 1034, of which 56 are `GROUP BY` rules SQLite does not enforce and 41 are type-affinity comparisons that survive ADR-025 because no foreign key is involved.

**Alternatives.**
- *Leave the 213 as conversion faults.* Reports a 20% conversion failure rate that is not real, and sends every investigation to the wrong component.
- *Rewrite every double-quoted token to a literal.* Simpler, and wrong on every database with a parenthesised or oddly-named column — of which Spider has several.
- *Regex the gold SQL.* Cannot tell a quoted identifier in a `SELECT` list from one inside a string literal, and cannot see qualification.
- *Set `SQLITE_DQS` off and treat the affected questions as gold errors.* Discards a fifth of the benchmark to avoid a 30-line repair, and the questions are not defective — they are valid SQLite.
- *Fold `dialect_error` into `gold_error`.* Tempting, since both leave the denominator. Rejected: a gold error is a benchmark defect and a dialect error is a portability gap, and only one of them is worth reporting upstream.

**Tradeoff.** The verifier now needs the schema before it can transpile, so transpilation is no longer a pure text-to-text step. That is the cost of applying a rule that is itself schema-dependent — SQLite's own rule is schema-dependent, and any repair that is not would be wrong somewhere.

**And the honest cost of the second half:** excluding dialect errors shrinks the scoreable set. 97 questions leave the denominator, and every published number must state that alongside the score, exactly as [DATASETS.md](../ml/DATASETS.md) §1 requires for the metric itself. An exclusion that is not reported is indistinguishable from cheating.

**A second rule of the same kind, found later — the title of this ADR understates it.** SQLite's `LIKE` is **case-insensitive for ASCII** by default (`case_sensitive_like` is off and nothing here turns it on); PostgreSQL's is case-sensitive. So `WHERE paragraph_text LIKE 'korea'` returns two rows on SQLite and none on PostgreSQL. Nothing raises — it silently returns different rows, which is why it surfaced as a conversion *mismatch* rather than an error. Measured: 3 of 1034 questions. `LIKE` is therefore rendered as `ILIKE`.

The principle is identical to the quoted-literal repair, so it lives here rather than in its own entry: **the gold SQL means what SQLite says it means, and a faithful rendering says that in PostgreSQL's vocabulary rather than passing the token through unchanged.** Two details are load-bearing. sqlglot models `NOT LIKE` as a `Like` node carrying `negate=True`, not as a `Not` wrapping a `Like` — so rebuilding the node from its two obvious arguments drops the negation and **inverts the predicate**, which is a worse bug than the one being fixed, and a test pins it. And `ILIKE` folds by collation while SQLite folds only ASCII, so a non-ASCII pattern can match where SQLite would not; recorded rather than closed, because the alternative is an ASCII-only fold around every operand for a case Spider does not contain.

**This correction cost an existing test its premise, which is the useful part.** An integration test asserted that `LIKE` case sensitivity was "a genuine engine difference the verifier catches". It was not an engine difference at all — it was a transpilation gap, attributed to the layer below the one that owned it, exactly like the 213. That test now uses the one difference no transpilation can close: a mixed-storage column that must become `text`.

**Generalises to:** before fixing a failure class, classify it. Two hundred failures with one cause and two hundred with fifty causes look identical in a summary count and call for completely different work.

**Revisit:** if a benchmark's gold SQL is written for PostgreSQL, in which case the repair should be off by default rather than schema-gated.

---

## ADR-027 — An undetermined result order is not a mismatch — in verification only

**Status:** accepted · **Date:** 2026-08-02 · **Stage:** 2

**Context.** Sixteen Spider dev questions verified as `mismatch` with identical rows in a different order. The shape is always the same:

```sql
SELECT name FROM employee ORDER BY age
```

with three employees aged 29. Both engines sort the ages identically. Neither promises anything about the *names* within a tie, and they choose differently.

**Decision.** In `benchmark.verify` only: when a strict comparison fails, re-compare with `order_matters=False`. If the rows are equal as a multiset, record `ambiguous_order` and count it as verified.

**Why this is sound here and nowhere else.** Verification runs **the same query** against both engines. If two runs of one query over the same data return the same rows in different orders, the order was never determined by that query — it is a property of the benchmark's SQL, not of the conversion. Nothing about the data moved.

**`evals.comparison` is deliberately untouched.** There, predicted and gold are *different queries*. A predicted query returning the right rows in the wrong order may well have omitted an `ORDER BY` the question asked for, and that is a real error. The same relaxation applied there would silently mark wrong answers correct — which is why this lives in the verifier and not in the shared comparator, even though both are comparing result sets.

**Alternatives.**
- *Count them as mismatches.* Fails 6 of 20 databases for something no conversion could fix, and the fix that suggests itself — reordering rows in PostgreSQL to match SQLite — is not achievable and would be meaningless if it were.
- *Relax the shared comparator.* Cheaper by one function, and it corrupts scoring. Two callers with genuinely different premises need two rules.
- *Add `ORDER BY` tiebreakers to the gold SQL.* Modifies the benchmark to suit the harness, and changes what the questions ask.

**Tradeoff.** `ambiguous_order` counts as verified, so a conversion that genuinely reordered rows *and* whose gold query had no total order would be missed. That intersection is what the outcome is separately counted and reported for — 16 on Spider dev — rather than being merged into `match`.

**Generalises to:** the same comparison in two places can need two rules, and the giveaway is whether the two sides being compared came from the same source. Sharing the strict one because sharing is tidy is how a correct rule ends up in a place where it is wrong.

**Revisit:** if the eval ever verifies with a query it also scores, which would collapse the distinction this rests on.

---

## ADR-028 — One connection-string form per consumer, converted at the driver

**Status:** accepted · **Date:** 2026-08-02 · **Stage:** 2

**Context.** `DATABASE_URL` is documented and shipped as `postgresql+psycopg://user:pw@host/db`. Alembic requires the `+driver` form — it is a SQLAlchemy URL. **psycopg cannot parse it**: `psycopg.connect` rejects the scheme outright.

Both facts had been true since Stage 1. `python -m benchmark.load convert` was the first code path to open an owner connection from a `.env` written by following `.env.example`, and it failed immediately. The MCP servers had never been startable that way either. No test caught it because `conftest` normalised the URL itself — **the workaround lived in the tests and the defect lived in production**, which is the specific way a fixture can hide a bug rather than expose it.

**Decision.** `core/dsn.py` owns the conversion. `libpq_dsn()` strips the `+driver` suffix and is called at every psycopg connection site; the configured value keeps the SQLAlchemy form, because that is what alembic reads and there is no second variable.

**Alternatives.**
- *Change `.env.example` to the plain form.* Breaks alembic, which is the one consumer that cannot convert.
- *Two variables — `DATABASE_URL` and `DATABASE_DSN`.* Two ways to say one thing, which can disagree. The one that is wrong is discovered by whichever tool is run second.
- *Convert in each caller.* Three call sites today, each a place to forget. The point of a boundary is that it is one place.
- *Let SQLAlchemy own all connections.* A much larger change to make a string parse, and the loader deliberately uses psycopg directly for `COPY`.

**Decided alongside it — the failure printed a password.** psycopg quotes the whole connection string in its *parse* errors, and the handler logged `str(exc)`. `redact_dsn()` now masks the password in anything derived from a driver exception before it reaches a log, a message, or a terminal. It deliberately keeps the **user name** visible, because "which role failed to connect" is the first thing anyone needs and it is not the secret. Full analysis in [SECURITY.md](../operations/SECURITY.md) §14.2.10.

A test asserts the *premise* rather than only the fix: it checks that psycopg really does put the DSN in a parse error. If a future version stops doing so, the redaction becomes belt-and-braces instead of load-bearing, and that is worth knowing rather than assuming either way.

**Generalises to:** when a value has two required formats, pick one canonical form, convert at the boundary that needs the other, and never let the two be configured separately. And when a test fixture massages configuration before use, it has stopped testing the configuration — the massaging is a defect report waiting to be read.

**Revisit:** if alembic gains support for plain libpq URLs, at which point the conversion can be deleted rather than moved.

---

## ADR-029 — A LIMIT that cuts a tie has no correct answer, and is excluded

**Status:** accepted · **Date:** 2026-08-02 · **Stage:** 2

**Context.** After [ADR-026](#adr-026--gold-sql-is-repaired-for-sqlites-quoted-literal-rule-and-dialect-gaps-are-not-conversion-faults) and [ADR-027](#adr-027--an-undetermined-result-order-is-not-a-mismatch--in-verification-only), 25 Spider dev questions still verified as `mismatch`. Diagnosed, they were three things, and only one of them was a conversion defect:

| Cause | Questions |
|---|---|
| `LIMIT n` where the `ORDER BY` key ties across the cut | 16 |
| SQLite's case-insensitive `LIKE` | 3 |
| `wta_1.players.birth_date` — 20,144 integers and 518 empty strings, so static typing forces `text` | 6 |

The 16 are all the same shape: `SELECT hometown FROM teacher GROUP BY hometown ORDER BY count(*) DESC LIMIT 1` where three towns are tied at the top. Every engine returns *a* correct answer and no two need agree on which.

**Decision.** A new outcome, `undetermined_limit`, counted and **excluded from the denominator** — the treatment `gold_error` and `dialect_error` already get, because a question with no unique correct answer cannot be scored later either.

**Why excluded rather than counted as agreement, unlike `ambiguous_order`.** They look similar and the difference decides the arithmetic. Under `ambiguous_order` the two engines return **the same rows** and disagree only about their order, so the data is provably intact and counting it as agreement is a statement about the data. Here they return **different rows** — nothing follows about the data in either direction, so calling it agreement would be a claim the evidence does not support.

**Detection requires two independent facts, and the second one is the entire safeguard.**

1. Without the `LIMIT`, both engines return identical multisets — the underlying data agrees.
2. On SQLite alone, the `ORDER BY` key at the cut equals the key of the first excluded row — the prefix genuinely is not determined.

Fact 1 alone is not sufficient, and assuming it was would have hidden the only real finding in the set. Two engines can also disagree about a prefix because they **order the same key differently** — which is precisely what a column wrongly converted to `text` causes, since SQLite orders integers numerically and PostgreSQL orders text lexicographically. Of the 18 questions that pass fact 1, **16 are ties and 2 are `wta_1.birth_date`**. A one-fact rule would have marked those 2 as benchmark ambiguity and reported a fidelity number that had quietly absorbed a conversion defect.

Fact 2 is answered by projecting the `ORDER BY` expressions into the select list, dropping the `LIMIT`, and reading the two rows either side of the cut — against **SQLite alone**, because the question "does this benchmark query determine its own answer" is a property of the original data and has nothing to do with the conversion.

**Conservative wherever it cannot be certain.** An unparseable query, a non-literal `LIMIT`, a `DISTINCT` whose row count the probe would change, or a `LIMIT` that never actually cut — each falls through to `mismatch`, which is the outcome that gets looked at. One specific trap is pinned by test: sqlglot answers `'1'` for the count in `LIMIT 1 + 1`, because `.name` returns the leftmost leaf, so reading it without a type check yields a wrong cut position with no error at all.

**Alternatives.**
- *Count them as mismatches.* What shipped. It fails 6 of 20 databases for questions that have no single right answer, and the fix it suggests — making PostgreSQL break ties the way SQLite does — is not achievable and would be meaningless if it were.
- *Count them as agreement, like `ambiguous_order`.* Overstates what was checked, and on this corpus would have swallowed the `birth_date` defect.
- *Use fact 1 alone.* Simpler, no probe query, and wrong on 2 of 18 in the first corpus it met.
- *Add a deterministic tiebreaker to both engines.* Changes the benchmark's queries to suit the harness.

**Tradeoff.** Detection costs an extra execution of the un-limited query on both engines, plus a probe on SQLite — and the un-limited form has no `LIMIT` by construction, so on a large table it materialises the full result. Bounded in practice by only running for questions that already mismatched, and by this being an offline operator tool; the PostgreSQL side inherits the session `statement_timeout`, the SQLite side has no equivalent. Accepted rather than hidden: if a future corpus makes this expensive, the fix is a row cap on the *probe* with the truncation recorded, not a cheaper rule.

**What this deliberately does not do.** It does not fix the remaining 6. A column holding 20,144 integers and 518 empty strings has no faithful static type: `bigint` cannot hold the empty strings, and `text` makes `SELECT birth_date` return `'19680831'` where SQLite returns `19680831`. Coercing the empty strings to `NULL` would make the column numeric and change what the data *is* — `WHERE birth_date = ''` would stop matching 518 rows. This is the consequence [DATASETS.md](../ml/DATASETS.md) §3 predicted in writing before any archive was downloaded, and reporting it is the correct outcome.

**Generalises to:** when two explanations produce identical symptoms and only one is your fault, the rule that tells them apart is worth more than the rule that handles either. Building only the cheap check is how a defect gets reclassified as someone else's ambiguity.

**Revisit:** if a benchmark ships orderedness or answer-uniqueness metadata per question, which would replace the inference entirely.

## ADR-030 — The eval runs the gold SQL verification produced, and never re-derives it

**Status:** accepted · **Date:** 2026-08-02 · **Stage:** 2

**Context.** The split files hold Spider's own SQL, which is SQLite. The eval runs against PostgreSQL. Something has to bridge them, and [ADR-026](#adr-026--gold-sql-is-repaired-for-sqlites-quoted-literal-rule-and-dialect-gaps-are-not-conversion-faults) already built the bridge: `transpile_to_postgres`, plus the quoted-literal repair and the `LIKE`→`ILIKE` rule. Calling it again at eval time is one import and looks obviously right.

**Decision.** The harness does not transpile. `benchmark.load verify --emit-gold` writes a JSONL of `{question_id, db_id, schema, outcome, scoreable, sql}` — the statement each gold query actually became, plus the result of comparing its output against SQLite — and `evals.run --gold` is **required**, not optional.

**Why re-deriving would be wrong even though it produces the same string today.** Verification's claim is not "this transpiles" but "this transpiles *and the two engines agreed on the rows*". That claim attaches to a specific statement. Re-transpiling at eval time means an edit to the transpiler silently changes every reference answer with nothing re-checking it — and a wrong gold query does not raise, it lowers an accuracy number, and the investigation that follows looks at the model. It is the same failure [ADR-022](#adr-022--the-conversion-is-verified-by-the-eval-harnesss-own-comparator) exists to prevent, one layer up.

**The denominator falls out of this, and that is the larger half.** `COMPARABLE` — `match`, `mismatch`, `ambiguous_order` — is defined once in `benchmark.verify` and used twice: conversion fidelity is *matched / comparable*, and execution accuracy is measured over exactly the questions `comparable` counts. On Spider dev that is **921 of 1034**; the other 113 are 97 `dialect_error`, 16 `undetermined_limit`, and the gold errors. Without this the harness would score against all 1034 and report a number depressed by roughly eleven points of questions that have no PostgreSQL answer at all.

**A question with no entry stops the run.** Not dropped, not scored. An unverified question is one nobody has checked the conversion behind, so scoring it reports a number about data of unknown fidelity, and dropping it shrinks a denominator for a reason that never appears anywhere. Both look exactly like a correct measurement.

**A `mismatch` stays scoreable.** The six `wta_1.players.birth_date` questions run on PostgreSQL and return *an* answer — just not SQLite's. Scoring against the converted copy stays internally consistent; what it is not is comparable to a published Spider number, which is [R-04](../project/RISKS.md#r-04--spiderbird--postgres-conversion-corrupts-the-benchmark)'s residual and is stated rather than excluded. Excluding them would delete the finding.

**Alternatives.**
- *Import `transpile_to_postgres` into the harness.* One line, and it makes `evals` depend on `benchmark` — the harness is meant to run against any corpus that can produce this file shape. It also loses the verification claim, above.
- *Rewrite the split files with PostgreSQL gold.* Tempting, and it conflates two things that change at different rates: the split is an assignment of databases, stable across conversions; the gold is an output of one conversion of one archive.
- *Make `--gold` optional and fall back to the split's SQL.* The fallback would run, produce a number, and be wrong by hundreds of quoted-literal failures — indistinguishable in the summary from a model that is bad at SQL.

**Tradeoff.** Two files must be kept in step, and a stale gold file against a re-converted database is a real operating hazard. Mitigated by the file being an output of the command that does the conversion check, and by an unverified question raising rather than passing. Not mitigated by a digest — that is the honest gap here, and the fix if it bites is to record the conversion report's hash in both.

**Generalises to:** a claim that took work to establish attaches to an artifact, not to a procedure. Re-running the procedure produces something that looks identical and carries no claim.

**Revisit:** if conversion becomes cheap enough to run inside the eval, at which point the gold file and the verification are the same pass.

## ADR-031 — One database, one schema, one catalog namespace, resolved per question

**Status:** accepted · **Date:** 2026-08-02 · **Stage:** 2

**Context.** Every component built in Stage 1 assumes a single schema: the retriever filters on one `dataset`, `SchemaCatalog` is loaded for one `dataset`, `SQLValidator` holds one catalog, and `EXPLAIN` resolves bare names through one session `search_path`. Spider dev is 20 databases, converted into 20 schemas, and questions from all of them sit in one split file — frequently with the same table names.

**Decision.** A `DatabaseScope` per `db_id`, built lazily and cached, bundling the schema name, the catalog, a retriever bound to that dataset, and a validator holding that catalog. The catalog namespace **is** the PostgreSQL schema name — `dataset = f"{prefix}{db_id}"`, the same string `schema_name_for` produced at conversion — rather than a second naming scheme.

**Why one name rather than two.** Two naming schemes have exactly one interesting failure: they disagree, retrieval returns another database's columns, the model writes plausible SQL against them, and `EXPLAIN` accepts it because those tables exist somewhere. Nothing raises. The conversion already chose a name and validated it as an identifier; reusing it makes the disagreement unrepresentable.

**Why the components are bundled rather than passed separately.** The failure mode is a *mismatch between two of them* — a catalog for one database while the session's `search_path` points at another — which produces SQL that validates and then reads the wrong tables. Grouping them means a question resolves one object and cannot half-switch.

**`search_path` is set twice, deliberately, at two different scopes.** The query runner sets it **per transaction** (`set_config(..., true)`), so one question cannot leak into the next. Validation sets it **per session** (`false`), because the validator opens its own transaction and a transaction-scoped setting would be reverted before `EXPLAIN` ran inside it. Both take the value as a bound parameter rather than composing a `SET`, since it derives from a benchmark-supplied `db_id`.

**Consequence worth stating: without the session-scoped set, the validation baseline measures nothing.** `EXPLAIN` on `SELECT id FROM concert` fails identically for correct SQL and for a hallucinated table if the session points elsewhere — so the invalid-query rate the baseline exists to measure would be 100% and entirely an artifact of wiring. It is asserted by a test that validates the *same statement* against two schemas and requires opposite answers.

**Alternatives.**
- *One catalog spanning all 20 databases.* Offers the model twenty databases of tables, and `singer` means two different things.
- *Qualify every generated table name with its schema.* Requires the model to know a naming convention that has nothing to do with the question, and gold SQL is unqualified anyway.
- *One PostgreSQL database per benchmark database.* Twenty connection strings and twenty pools to answer one split file.

**Tradeoff.** A run touching all 20 databases holds 20 catalogs and 20 retrievers. Small for Spider (a few hundred elements each); it is the first thing to reconsider on BIRD, where schemas are far larger. Lazy construction is what keeps a single-database run cheap.

**Generalises to:** when a component's single-instance assumption meets a plural corpus, scope it explicitly rather than widening it. Widening turns a would-be error into a wrong answer.

**Revisit:** on BIRD, where per-database catalog memory becomes measurable.

## ADR-032 — The eval's query runner is not the production executor

**Status:** accepted · **Date:** 2026-08-02 · **Stage:** 2

**Context.** `SQLExecutor` already runs SQL under a row limit, a statement timeout, an audit write and a re-validation. The eval needs something that runs SQL. Reusing it is the obvious move, and [EVALUATION.md](../ml/EVALUATION.md) §3 requires gold and predicted to go through the *same* runner.

**Decision.** A separate `SchemaScopedQueryRunner`: read-only role, statement timeout, per-transaction `search_path`, and a row cap that **refuses rather than truncates**. No re-validation, no `LIMIT` injection, no audit row.

**Why not the executor.** Its controls are for *model output*, and the runner's rule is that gold goes through the identical path. Applying them to gold breaks the measurement in two specific ways. A reference query rejected by the cost ceiling would be recorded as a gold error, which it is not. And `apply_row_limit` injecting `LIMIT 500` into both sides is worse than it looks: two queries returning the same rows in an unspecified order, cut at the same length, are cut in **different places** — so the comparison reports a value mismatch for a correct answer. That is the failure the whole harness exists to avoid, introduced by a control meant to prevent a different one.

**So the cap refuses.** Above `MAX_RESULT_ROWS` (100,000, far above any Spider result) the query raises and the runner records an execution failure — which is what actually happened — instead of silently producing a comparison of two arbitrary prefixes.

**What is *not* dropped: the read-only role and the timeout.** Those are controls about the database rather than about the query's authorship, and a benchmark run is the run most likely to execute a statement nobody has read.

**Security consequence, stated rather than discovered later.** In the `full-schema` and `retrieval-only` baselines, model-authored SQL reaches the database **with no validation tier in front of it** — that absence is the ablation. Containment is unchanged, because the validation tier was never the boundary ([SECURITY.md](../operations/SECURITY.md) §5): the role holds, and an integration test asserts an `INSERT` through this runner is refused by PostgreSQL. Recorded in SECURITY.md §14.2.11 with severity and scenario.

**Alternatives.**
- *Use `SQLExecutor` for both.* Truncation-induced false mismatches, and gold subject to the agent's cost ceiling.
- *Use `SQLExecutor` for predicted, plain execution for gold.* Two paths, which is precisely the `Decimal('1')` vs `1.0` failure EVALUATION.md §3 forbids.
- *Truncate at the cap instead of refusing.* Cheaper, and produces wrong verdicts that look like model errors.

**Tradeoff.** The eval does not exercise the production executor, so nothing in a benchmark run would catch a regression in row limiting or audit writing. Those have their own integration tests; what is genuinely lost is that the number is measured through a slightly different path than the one the API will serve, and the difference is exactly the two controls named above.

**Generalises to:** a safety control and a measurement control are not interchangeable, and the one that quietly changes the data is the dangerous one to reuse.

**Revisit:** at Stage 4, when self-correction needs the executor's structured errors — the retry loop may want the executor's taxonomy even where the scoring path does not.

---

## ADR-033 — The read-only role is proved at startup, by asking rather than by writing

**Status:** accepted · **Date:** 2026-08-05 · **Stage:** 1

**Context.** Every containment claim in [SECURITY.md](../operations/SECURITY.md) rests on `DATABASE_RO_URL` naming a role that cannot write — §2 calls it "the one that actually holds" and says the layers above only reduce how often it is tested. Nothing verified it. The only check compared the two DSN **strings** for inequality, and `postgresql://postgres:pw@localhost/db` and `postgresql://postgres:pw@127.0.0.1/db` are different strings and the same superuser.

Thirty negative tests in `tests/security/test_readonly_role.py` assert what `sql_agent_ro` may do. All thirty build that role from migration 002 inside a testcontainer; none of them looks at the role the application connects as. The suite proves the migration is correct and says nothing about the deployment.

**Decision.** `composition.assert_read_only(connection)` runs on **first open of the read-only connection** and refuses to hand it out otherwise. It asks PostgreSQL's own privilege functions: `has_table_privilege` for INSERT/UPDATE/DELETE/TRUNCATE across every non-system schema, `has_schema_privilege(..., 'CREATE')`, and the four role attributes that bypass grants entirely (`rolsuper`, `rolcreatedb`, `rolcreaterole`, `rolbypassrls`).

**Why ask rather than attempt.** The failure this exists to catch is precisely the one where a probe `INSERT` would be **accepted**. A startup check whose negative result is a mutation of the operator's database is not a check worth running. Two lesser reasons: it needs no table to aim at, so an empty target schema cannot produce a vacuous pass; and `has_table_privilege` accounts for role inheritance, `PUBLIC` grants, column-level grants and superuser bypass in one answer — four rules this code would otherwise reimplement and keep correct across PostgreSQL versions.

**Why on connection open rather than in each entrypoint.** Four MCP servers and an HTTP API is five places to remember, and the one that forgets is the one that ships. A component holding this connection has, by construction, proved it — which is how `execute_sql`, the server that actually runs generated SQL, gained the check without being modified.

**Alternatives.**
- *`INSERT` inside a rolled-back transaction.* The negative result is a mutation. Rollback does not undo trigger side effects, sequence advances, or WAL.
- *Check `default_transaction_read_only` only.* Any session can `SET` it off. It is a second barrier, not the boundary — so it is **reported** in the success log rather than enforced, because migration 002 claims two barriers and a check that only ever looked at one should not be the evidence for that claim.
- *Compare `current_user` to `DB_READONLY_ROLE`.* Name equality is not privilege. A role can be renamed, or granted more later.
- *An opt-out setting.* Rejected outright: a variable that turned the boundary off would be set in exactly the deployment that needed it most.

**Tradeoff.** Three extra round trips per process start, and a point-in-time snapshot — a `GRANT` issued while the process runs is not detected until restart. Accepted because grants change at migration frequency and processes restart on deploy. Continuous verification is [FUTURE.md](../project/FUTURE.md) work.

**Generalises to:** a control tested only against the fixture that satisfies it has been tested against itself. When a control has tests, the question is not *is it tested* but *what does the fixture hold fixed* — here it held fixed the one thing that could be wrong in production.

**Revisit:** when authentication lands and per-tenant roles become possible, at which point "the role" is no longer a single answer.

---

## ADR-034 — The API refuses to bind beyond loopback while it has no authentication

**Status:** accepted · **Date:** 2026-08-05 · **Stage:** 1

**Context.** [CONFIG.md](../operations/CONFIG.md) §6 has specified since Stage 0 that "binding to `0.0.0.0` without `API_KEY` set is a startup error, not a warning." `API_KEY` does not exist — authentication is Stage 6 — so the condition is vacuously true today, which means the rule as written enforces nothing.

**Decision.** `APISettings` raises `ConfigurationError` for any `API_HOST` that is not loopback. Every loopback spelling is accepted (`127.0.0.1`, `127.0.0.2`, `::1`, `localhost`); anything that does not resolve as an IP literal fails **closed**. The same validator pattern refuses `API_CORS_ORIGINS=*`.

**Why an error and not a warning.** Nobody reads a log line on a service that started successfully. The failure modes are ordinary: a developer sets `0.0.0.0` to test from a phone and then joins a conference network; a compose file publishes the port on a host with a public IP. In both, an endpoint that runs model-generated SQL against the target database and spends the LLM budget is reachable by anyone who can route a packet.

**Why every loopback spelling.** A control that wrongly blocks a legitimate configuration is a control somebody removes. If `::1` were rejected, the fix a frustrated developer reaches for is deleting the validator, not changing the address.

**Why a validator rather than a safe default.** A default is something you change; a validator is something you argue with. The CORS case makes the difference concrete: the dangerous configuration is a wildcard origin *combined with credentials*, and the usual mitigation is to hardcode `allow_credentials=False` and let the wildcard through. That works until someone enables credentials later for a good reason and the two halves recombine. Refusing `*` where it is written means the combination is never representable.

**Alternatives.**
- *Warn and continue.* The audience for a warning on a working service is nobody.
- *Allow non-loopback when `API_KEY` is set.* There is no authentication for a key to attach to. A setting that gates nothing is worse than no setting, because it reads like a control.
- *A `--dev` flag that permits it.* Makes the dangerous configuration and the convenient one the same flag.

**Tradeoff.** Deploying requires publishing the port from a container runtime (`-p 8000:8000`) or fronting the service with an authenticating proxy. That is one line of configuration, and it moves the decision to expose the service to whoever is deploying it rather than to a default.

**Generalises to:** when a documented control is conditioned on something that does not exist yet, the honest implementation is the stricter one — not the vacuous one.

**Revisit:** when authentication lands. At that point the original Stage 0 rule becomes implementable as written, and this validator relaxes to it.

---

## ADR-035 — The composition root is its own package, because entrypoints are peers

**Status:** accepted · **Date:** 2026-08-05 · **Stage:** 1

**Context.** `Resources` — both database connections, the catalog, the retriever, built once per process and injected — lived in `src/mcp_servers/resources.py`. The HTTP API needs the same graph.

**Decision.** Moved to `src/composition/`. The API and the four MCP servers both import it; neither imports the other.

**Why not just import it from `mcp_servers`.** They are **peers** — both adapters over the same components, both entrypoints. An HTTP layer that depends on the MCP layer to open a database connection is a dependency in the wrong direction, and it would make `mcp_servers` un-deletable: removing the MCP servers would break the API for no reason anyone could explain.

**Why not `core/`.** `core` is the innermost layer and holds the ports everything else depends on. It cannot depend on `schema` and `adapters`, which the graph must construct.

**Why not a separate `ApiResources`.** The API needs a superset of what the servers need, not a different thing. Two classes would duplicate connection opening, catalog loading and the retriever build — and would have duplicated `assert_read_only` (ADR-033) or, more likely, omitted it from one of them.

**Consequence.** `composition` is the one package allowed to know about every layer at once, and nothing depends on it. That is what a composition root is: constructing the object graph is the job, and every other module receives what it needs rather than building it.

**Tradeoff.** One more top-level package for one file. Accepted because the alternative encodes a false hierarchy between two things that are the same kind of thing.

---

## ADR-036 — The shared answering path stops at generated SQL, and raises

**Status:** accepted · **Date:** 2026-08-04 · **Stage:** 1→2

**Context.** The eval harness answers a question by retrieving schema context and generating SQL against it. The HTTP API is about to do the same. Two implementations of the same two steps is how a published accuracy number ends up describing a system nobody can query — and the size of that risk is measurable here: `RETRIEVAL_TOP_K=10` against Spider schemas holding 10–67 elements was worth **thirty points** of execution accuracy.

**Decision.** `src/answering/` exposes `retrieve()`, `generate()`, and `candidate()` — the composition of the two. It **raises** rather than returning a failure value, and it stops at generated SQL.

**Why all three are public.** The eval needs the intermediate: Recall@k is computed from the retrieved elements *whether or not generation succeeded*, and dropping them on failure would measure recall and accuracy over different question sets — making the correlation between them, which is the entire argument for the Stage 5 fine-tune, an artefact of the model's success rate. The composition is public so it can be **asserted**: a test runs both routes with identical fakes and requires equal results. A caller that sequences the phases itself is a caller that can sequence them wrongly.

**Why it raises.** The conversion only runs one way. An exception converts to the eval's `Attempt` cleanly; an `Attempt` cannot be recovered back into an HTTP status code. The two callers also need different distinctions — a spent LLM quota is `infrastructure` to the eval and leaves the scored denominator, and a `429` with `Retry-After` to the API. A shared layer that flattened first would have to be unflattened by both.

**Why it stops at generated SQL.** Execution cannot be shared — [ADR-032](#adr-032--the-evals-query-runner-is-not-the-production-executor). This is stated in the package docstring because otherwise the next reader will helpfully "finish" the abstraction.

**Alternatives.**
- *Return `Result[Candidate, Failure]`.* A third vocabulary neither caller wants, and both would immediately translate out of.
- *Only expose `candidate()`.* Tried first. Unusable by the caller that already existed, because the eval needs `retrieved` on the generation-*failure* path and the composed call raises before returning anything. The abstraction had been designed from the caller not yet written.
- *Make the eval's `Attempt` the shared return type.* Puts benchmark concepts — `error_type`, gold comparison — into the request path.

**Tradeoff.** A seam built one commit before its second caller, which is the shape YAGNI warns about. Accepted narrowly: the eval was being refactored anyway, the second caller was the next slice rather than a maybe, and the cost of divergence is a benchmark number that describes nothing. Also two parameters (`feedback`, `previous_sql`) that no caller passes yet — they are the Stage 4 retry shape, and re-retrieving on a retry answers a subtly different question and makes the retry's effect unattributable.

**Generalises to:** share the part where a difference would invalidate the measurement; keep separate the part where sameness would.

---

## ADR-037 — Resumption skips answered questions, not recorded ones

**Status:** accepted · **Date:** 2026-08-06 · **Stage:** 2

**Context.** The artifact store exists so that a spent daily token budget stops being a lost run — its module docstring says so, and [EVALUATION.md](../ml/EVALUATION.md) §3 calls resumability "the requirement that shaped the design". Every question is written as it completes, and `resume()` returns what is already on disk so a re-run skips it.

It returned **every** recorded id, without looking at why the question was recorded.

The first full-split attempt made that concrete. Groq's daily cap arrived at question ~395 of 1034, and the loop kept going, recording 308 questions as `llm_failed`. Every one of those was thereafter permanently done. The run could not be finished by resuming it — only restarted from nothing, spending the whole budget again — and the second day would have hit the same cap in the same place. **A budget spent failing was still a lost run; it just looked like a completed one.**

The same set already existed elsewhere, doing the same job under a different name: `FailureCategory.INFRASTRUCTURE` means *the system under test never got to answer*, and those questions are excluded from the scored denominator for exactly the reason they should be retried.

**Decision.** `resume()` treats a question as done only if it was **answered**. An artifact whose `error_type` is in the infrastructure set — `llm_failed`, `scope_unavailable`, `retrieval_failed`, `internal_error` — is re-attempted, and the retry overwrites the record in place because `artifact_filename` is deterministic.

Both decisions read `evals.taxonomy.is_infrastructure`. They are the same claim, and a test asserts that the predicate and the scoring exclusion agree.

**Why not retry every failure.** `unanswerable` and `execution_failed` are things the run *learned* about the model. Re-asking them spends budget re-deriving results already in hand, which on a free tier is the same defect with the sign reversed.

**Why the error type rather than the recorded category.** `failure_category` is derived; an artifact may have been written by an older taxonomy. The error type is what the component actually reported.

**The consequence to know about.** A question failing for a *durable* infrastructure reason is retried on every resume and never retires. That is the intended reading: a database that is still not indexed is a deployment fault the operator should keep seeing, not a question the harness should quietly give up on. The halt below bounds what that costs.

**And the run now stops at a wall.** Ten consecutive infrastructure failures end the run (`--halt-after`, `0` disables). A spent budget does not recover inside a run, so continuing means asking a dead provider several hundred more times: it costs wall clock, buries the cause under identical records, and leaves the summary describing a directory that is mostly noise. Consecutive rather than total, because a blip the provider recovers from must not stop a run that is making progress — and by the time an `llm_failed` reaches the runner the client's own retries are already exhausted.

The threshold is deliberately low enough to trip on a whole database failing as `scope_unavailable`. That is a deployment fault, and stopping to report it beats scoring around it — which is what the same run did with 84 questions.

**Alternatives.**
- *Delete the failed artifacts by hand before resuming.* What I did once. It works, it is undocumented, and it depends on remembering which of five error types are safe to delete.
- *Never record an infrastructure failure.* Loses the evidence. The 308 records are how the cause was diagnosed, and the 74 `scope_unavailable` records are how a separate bug was found.
- *Halt on total rather than consecutive failures.* Ends long runs over accumulated transient blips, which is the failure mode the retries already handle.
- *Clamp `halt_after=0` to "off".* Zero reads as "never halt" to one person and "halt immediately" to the comparison. Refused instead.

**Tradeoff.** A resumed run now re-executes gold SQL for the retried questions, so a durable fault costs a little database work on each attempt. Cheap next to the alternative, which is a benchmark that can only ever be run in one sitting on hardware that cannot provide one.

**Generalises to:** "we recorded it" and "we learned something from it" are different predicates, and a store that conflates them turns an outage into data.

---

## ADR-038 — The served request accepts only fields that do something

**Status:** accepted · **Date:** 2026-08-06 · **Stage:** 1

**Context.** [API.md](../architecture/API.md) specifies `POST /v1/query` with `question`, `session_id`, `stream` and three options. The slice that serves it builds none of the streaming and none of the session memory — both are Stage 4 — so the endpoint had to decide what to do with two fields it cannot honour.

The tempting answer is to accept them and ignore them: the request parses, existing clients keep working, and nothing breaks today.

**Decision.** The served request model contains `question` and `options.{max_rows, timeout_ms, explain_only}`, with `extra="forbid"`. Sending `stream` or `session_id` is a `400` naming the field.

**Why.** This project has already paid for the other choice. Three variables in `.env.example` that nothing read cost a slice ([ADR-036](#adr-036--the-shared-answering-path-stops-at-generated-sql-and-raises) is the neighbouring decision; the finding is in the CHANGELOG under *dead settings*), and the lesson was that **a missing feature produces a gap the caller can see, and an accepted-and-ignored field produces confidence.** An HTTP field is the same object one layer out.

`session_id` is the sharper of the two, and the reason this is not merely tidiness. Ignoring it means a caller's follow-up question is answered *without* the previous turn's context — and because the system will happily answer it, what comes back is **plausible** rather than obviously wrong. There is no error, no warning, and no way for the caller to tell.

The response omits `answer` for the same reason. API.md's example carries prose — *"Revenue in Q4 2025 was highest in EMEA"* — which requires a synthesis step that does not exist. An empty string in that field is a claim the system cannot back.

**Alternatives.**
- *Accept and ignore.* The default, and the one this ADR exists to refuse.
- *Accept `stream: false` and reject only `true`.* Reads better in a client that always sends the field. Rejected because it means the request model carries a field with exactly one legal value, which is a field that does nothing wearing a permission.
- *`501 Not Implemented` for `stream: true`.* A defensible answer and a second code to publish. `extra="forbid"` already names the field in a `400`, which tells the caller the same thing with nothing added to the error table.
- *Serve `answer` as an empty string.* Keeps the documented shape. Trades a visible gap for an invisible one.

**Tradeoff.** A client written against the full API.md contract fails against the served endpoint instead of degrading. That is the intended direction: it fails immediately, at the field, with the name in the message — rather than succeeding and returning an answer built without the context it asked for.

**Generalises to:** an interface should accept exactly what it honours. Where it cannot, the gap belongs in front of the caller, not behind them.

---

## ADR-039 — A stream is admitted before it is a stream

**Status:** accepted · **Date:** 2026-08-07 · **Stage:** 1

**Context.** `POST /v1/query` gained `stream: true`. The endpoint already had an in-flight cap that refuses over-limit requests with `429` ([ADR-038](#adr-038--the-served-request-accepts-only-fields-that-do-something) is the neighbouring decision on the request shape).

A streaming response makes that cap harder in a way that is easy to miss: **once the response has begun, `429` is no longer expressible.** The status line and headers are gone. The only remaining way to refuse is an `error` event on a response that has already claimed `200 OK`, which tells a client the request succeeded and then that it did not.

The natural implementation walks straight into it. An `async def` generator does not execute its own body until the first `__anext__`, which happens *after* the route has returned and the response has started. Anything the generator does about admission, it does too late.

`asyncio.Semaphore` cannot help, because its `acquire` is a coroutine: taking a slot means awaiting, and awaiting means returning. Checking `locked()` first and acquiring later leaves a gap with an `await` in it — which is precisely the window two concurrent requests both pass through.

**Decision.** `QueryService.stream()` is **not** `async`. It is a plain function that admits the request synchronously and then returns the async generator that does the work. Admission is a plain integer counter (`_Admission`), shared by both response shapes.

**Why a counter and not a semaphore.** Synchronous `acquire` is atomic against the event loop — there is no suspension point between the test and the increment, so no second request can observe the state in between. That is a property of *asyncio being single-threaded*, not of the class, which is why it is documented as not thread-safe rather than made thread-safe for a caller that does not exist.

**One cap, not two.** The streaming path does not get its own allowance. A second counter would be a second policy, and the effective limit would be whichever one happened to be checked. The same argument applies to error rendering: a stream cannot use the exception handlers, so it renders failures itself — through `errors.published`, the one function that decides which of this project's messages a caller may read. Two renderers, one rule. Left inline in the handler, the stream would have re-derived that judgement, and re-deriving a "which of our messages are publishable" call is how the strict copy and the lenient copy end up in one process.

**Alternatives.**
- *Refuse with an `error` event on a `200`.* Expressible, and it lies in the status line. A client's retry logic reads status codes.
- *`asyncio.Semaphore` with `locked()` checked in the route.* The current non-streaming shape, extended. Rejected: the check and the acquire are separated by the response starting.
- *A larger cap and queue the overflow.* Moves the cliff without removing it, and converts refusal into latency — the failure mode clients handle worst.

**Tradeoff.** Admission happens before the generator's `try`, so the release lives in the generator's `finally` and the two are textually apart. Nothing between them may raise; constructing an async generator cannot, which is what makes the arrangement safe rather than merely short. The slot is returned on completion, on failure, and on the client hanging up — Starlette closes the generator, which raises `GeneratorExit` at the `yield`. **A slot returned only on success is a slot a client can consume permanently by disconnecting**, which is a denial of service costing the attacker one connection each.

**Generalises to:** a control that must be expressible in the response has to run before the response exists.

---

## ADR-040 — Startup opens the model, because naming it is not loading it

**Status:** accepted · **Date:** 2026-08-07 · **Stage:** 1

**Context.** `api.app._lifespan` states its purpose at the top of the module: touching every accessor eagerly moves configuration failures *"from the first request to the moment the process starts, where a deployment is watching and a rollback is still cheap."*

It touched `resources.retriever` and logged `retriever.model_version`. That property returns the configured model **name** — a string, from settings, loading nothing. `SentenceTransformerEmbedder` holds `self._model = None` until its first `embed()`.

So the process logged `ready`, `/ready` answered `ready`, and the checkpoint had never been opened.

**What it cost, in two parts, and the second is what hid the first.**

A missing, corrupt or undownloadable checkpoint surfaced on the **first request** rather than at startup — the exact failure the eager lifespan exists to prevent, in the one dependency that reaches the network to initialise.

And the load costs roughly twenty seconds of CPU, paid by whoever asked first. That was invisible in the `steps` array, because retrieval and generation are timed together as a single `answer` stage. It went unnoticed long enough to be **recorded in PERFORMANCE.md as the cost of a model round trip** — a 29 s measurement attributed almost entirely to a rate-limited provider, on the strength of an aggregate that could not distinguish the two.

**Decision.** The lifespan reads `retriever.dimensions`, which has no answer available without reading the checkpoint. The value is logged next to `model_version`, so the log line now reports something that could have failed.

**Why `dimensions` rather than a `warm()` method.** It is the smallest honest way to say *"and actually load it"*: the property genuinely cannot be answered from configuration. A `warm()` on the port would be a method whose only purpose is its side effect, added to an interface whose docstring says it turns text into vectors and nothing more. `dimensions` is also the value worth having at startup for an independent reason — `RetrievalSettings.retriever_model` documents that *"a model of a different width is a migration, not a config change,"* and until now nothing read the width at all.

**How it was found.** Not by reading the code. The per-stage `stage` events added for streaming split `answer` into `retrieve` and `generate`, and the first request showed retrieval taking twenty seconds against generation's two. Measured live: **21.8 s first request before, 2.9 s after, with steady-state answers between 0.6 s and 1.8 s.** This is the third time in this project a defect has been invisible in the code and obvious from the operation, and the second time the fix was a consequence of building instrumentation for something else.

**Tradeoff.** Startup is now about twenty seconds slower, and it is honest about it: that time was always being spent, by a caller, inside a request that had no way to report it. A readiness probe that answers late is a deployment concern with an established answer; a first request that takes twenty seconds is a user-visible defect with none.

**Generalises to:** an eager-initialisation check must touch something that can fail. A property answered from configuration proves the configuration was read, not that the thing it names exists.
