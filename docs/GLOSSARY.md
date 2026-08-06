# Glossary

Every term used across this documentation, defined once. Ordered roughly from protocol → retrieval → SQL → evaluation.

---

## Protocol and agent

**MCP (Model Context Protocol)**
An open protocol for exposing tools, resources, and prompts to LLM applications over JSON-RPC 2.0. Servers advertise capabilities; clients discover and invoke them. The point for this project: capabilities are described by the server at runtime rather than hardcoded into the agent.

**MCP server**
A process exposing a set of tools over MCP. This project runs four: `schema_search`, `validate_sql`, `execute_sql`, `profile_table`.

**MCP client**
The side that connects to servers, calls `tools/list` to discover what is available, and issues `tools/call`. Here, the agent is the client.

**`tools/list` / `tools/call`**
The discovery call and the invocation call. The client's capability set comes from the `tools/list` response rather than from a constant — which is what makes adding a fifth capability an operations change rather than a code change.

**stdio transport**
The host launches the server as a subprocess and speaks JSON-RPC over its stdin/stdout. **stdout is the protocol**, so anything else written there corrupts the stream.

**Tool error vs protocol error**
A tool error is an expected, readable outcome (`isError: true` plus structured content) that the agent corrects itself from. A protocol error is a JSON-RPC-level failure — a bug, not something to retry. Invalid *arguments* are a tool error here, not a protocol error, because they are written by a model.

**Structured content**
A typed payload returned alongside the text block of a tool result. Optional in MCP, so the text block must carry the whole payload on its own.

**MCP host**
An application that embeds an MCP client — this project's own `ToolRegistry`, the open-source MCP Inspector, an IDE, or a desktop app such as Claude Desktop. The reason MCP matters here: any host can point at these servers, and none of them is a requirement.

**Tool contract**
The name, description, and JSON Schema a server publishes for a tool. The contract is the actual interface design work — a tool wrapped in a protocol with a vague description is a wrapper, not a capability.

**Runtime tool discovery**
Fetching the tool list from the server on connect instead of compiling it into the client. Adding a capability then requires no agent change.

**Agent loop**
The cycle of: read state → decide on a tool call → execute → observe result → repeat until the task is done or a budget is exhausted.

**Decomposition**
Splitting a compound question ("compare Q3 vs Q4 growth by region and flag anomalies") into several sub-queries plus a synthesis step.

**Session memory**
Retained prior results within a conversation, so follow-ups ("now just the top three") resolve without re-running everything.

**Self-correction**
Feeding a database or validation error back into the agent as a structured observation so it can revise the SQL, rather than surfacing the error to the user.

**SSE (Server-Sent Events)**
A one-directional HTTP streaming protocol used here to push agent progress to the client as it happens.

---

## Retrieval and ML

**Schema linking**
Mapping natural-language phrases to the specific tables and columns they refer to. "Revenue last quarter by region" → `orders.total_amount`, `orders.created_at`, `customers.region`. The dominant failure mode in text-to-SQL on large schemas.

**Retriever**
The component that, given a question, returns the top-k candidate schema elements. Here it is a sentence-transformer bi-encoder over serialized column descriptions.

**Embedding**
A dense vector representation of text such that semantically similar text lands nearby in vector space.

**Bi-encoder**
An architecture that embeds query and document independently, so document embeddings can be precomputed and searched with a vector index. Fast; less accurate than a cross-encoder.

**Cross-encoder**
An architecture that scores a (query, document) pair jointly. More accurate, but cannot precompute — every candidate needs a forward pass.

**pgvector**
A PostgreSQL extension adding a `vector` column type and approximate-nearest-neighbour indexes (HNSW, IVFFlat). Lets schema embeddings live in the same database as everything else.

**Contrastive learning**
Training that pulls matched pairs together in embedding space and pushes mismatched pairs apart. Here: (question, correct column) as positives, (question, wrong column) as negatives.

**MultipleNegativesRankingLoss**
The contrastive objective planned for the schema linker: within a batch, every other example's positive serves as a negative for the current example. Efficient — no explicit negative mining required, though hard negatives improve it.

**Hard negative**
A wrong answer that is *nearly* right, so the model must learn a fine distinction. For schema linking: a column with a similar name in a different table.

**Recall@k**
Of the schema elements actually needed to answer the question, the fraction that appear in the retriever's top-k results. The headline retrieval metric here, because a column that never gets retrieved can never appear in correct SQL.

**Ablation**
Removing or swapping one component to isolate its contribution. The fine-tuned-vs-baseline retriever comparison is the ablation this project commits.

---

## SQL and execution

**AST (Abstract Syntax Tree)**
A structured tree representation of parsed code. Validating SQL against its AST catches structural problems and lets you inspect what the query *does* — which tables it touches, whether it mutates — without executing it.

**sqlglot**
A Python SQL parser, transpiler, and optimizer. Used here to parse generated SQL into an AST, verify it is a single read-only statement, and check identifiers against the real schema.

**EXPLAIN**
A PostgreSQL command that returns the planner's execution plan without running the query. Catches unknown tables/columns and type errors, and exposes estimated cost — all without touching data.

**Side-effect-free**
A capability that changes no state and can be retried arbitrarily. `validate_sql` is side-effect-free by construction; `execute_sql` is not. That asymmetry is why they are separate MCP servers.

**Read-only role**
A PostgreSQL role granted `SELECT` only, with no write, DDL, or function-execution privileges. The outermost containment boundary — it holds even if every layer above it is compromised.

**Statement timeout**
A per-connection PostgreSQL setting (`statement_timeout`) that aborts a query exceeding a wall-clock limit. Prevents one pathological query from occupying a connection indefinitely.

**Row limit**
A cap on returned rows, enforced by injecting/enforcing `LIMIT` at the AST level rather than trusting the model to include one.

**Blast radius**
The maximum damage a compromised or malfunctioning component can do. Bounded here by the read-only role, timeouts, row limits, and cost caps acting together.

**SQL injection**
Executing attacker-controlled SQL by string-concatenating untrusted input into a query. Structurally different from this project's core risk — see prompt injection.

**Prompt injection**
Text that manipulates the model into taking unintended actions. Relevant here because a question, and even schema comments or column *values*, can carry instructions. Mitigation is containment (read-only role, validation tier), not filtering — see [SECURITY.md](operations/SECURITY.md).

**Disclosure budget**
The bounds governing what a component may *reveal* about the data it reads, as distinct from what it is permitted to read. `profile_table` is the only component needing one, because it is the only one whose output is row-derived by design.

**Small-cell rule**
A statistical disclosure control: suppress any value whose count falls below a threshold. A value occurring once identifies whoever it belongs to; a value occurring five hundred times is a category label. Configured as `PROFILE_MIN_VALUE_FREQUENCY`, floored at 2 in the type. See [SECURITY.md](operations/SECURITY.md) §14.2.6 and ADR-016.

**Fail closed**
An unrecognised input gets the restrictive treatment, not the permissive one. Profiling applies it to type eligibility: a type absent from the ordered-types allowlist gets no `min`/`max`, so an extension type added after that list was written cannot leak a verbatim cell labelled as a statistic.

**Allowlist (identifiers)**
The set of names a caller may reference at all, as opposed to whether a name is correctly *escaped*. `sql.Identifier("pg_authid")` quotes that name perfectly and then reads it — quoting answers "is this escaped?", only an allowlist answers "may this be named?". The schema catalog serves as both here.

---

## Evaluation

**Spider**
A large, cross-domain text-to-SQL benchmark: ~10k questions over 200 databases, with the test databases held out from training. The standard generalization benchmark.

**BIRD**
A text-to-SQL benchmark on larger, dirtier, more realistic databases. Requires external knowledge and reasoning over messy values, so scores run well below Spider.

**Execution accuracy**
The fraction of generated queries whose *result set* matches the gold query's result set. Robust to a query being written differently but correctly — the metric that matters for this project.

**Exact match accuracy**
String or AST equality against the gold query. Penalizes correct-but-differently-written SQL, so it is reported only as a secondary signal, if at all.

**Task success (multi-step)**
For compound questions, whether the final synthesized answer is correct — not whether each intermediate query was.

**Invalid-query rate**
The fraction of generated queries that fail to parse or fail `EXPLAIN`. The number the validation tier plus self-correction loop is designed to drive down.

**Held-out set**
Data never seen during training or prompt tuning, used for the final reported numbers. Kept separate from the development set used for iteration.

**Gold error**
A benchmark reference query that is itself wrong, ambiguous, or does not run on the database it shipped with. Counted and reported rather than discarded — dropping them quietly inflates every score computed afterwards, because they cap what is achievable.

**Infrastructure failure**
A question the system under test was never asked: a spent provider budget, a schema that was never indexed, a retriever that fell over, a bug in the harness. Sibling to *gold error* and excluded from the denominator for the same reason — nothing about the model follows from a question it never saw. The consequence is easy to miss: a run with many of these does not report a *bad* score, it reports a **smaller measurement**, at the same apparent confidence.

**Answered vs recorded**
The distinction resumption turns on. Every question is written to disk as it completes, including the ones that failed — the records are the evidence. But only a question the model actually *answered* is skipped on a re-run; an infrastructure failure is re-attempted. Conflating them means a spent daily budget permanently retires the questions it failed, so the run can only ever be restarted from nothing. See [ADR-037](architecture/DECISIONS.md#adr-037--resumption-skips-answered-questions-not-recorded-ones).

**Type affinity**
SQLite's rule for what a declared column type *suggests*, given that SQLite does not enforce types: a column declared `INTEGER` can hold `'unknown'`. The conversion treats the declaration as a hint and the data as the evidence, which is why a benchmark column can arrive in PostgreSQL as `text`.

**Conversion verification**
Executing every gold query against both the original SQLite database and the converted PostgreSQL copy and comparing the results, using the eval harness's own comparator. A conversion defect never raises; it lowers an accuracy number, and the investigation that follows looks at the model.

**Conversion fidelity**
The share of comparable gold queries the converted copy reproduces. It is the ceiling on any accuracy measured against that copy, which is why [BENCHMARKS.md](ml/BENCHMARKS.md) §0 records it before any accuracy row exists.

**Dialect error**
A gold query PostgreSQL rejects for a reason no conversion could fix — a `GROUP BY` rule SQLite does not enforce, or a comparison relying on type affinity. It would fail identically against a perfect conversion, so it is excluded from the denominator like a gold error, and the exclusion is reported.

**Ambiguous order**
Two engines returning the *same rows* in a different order because the gold `ORDER BY` never determined one — three employees tied at the same age. Counted as agreement, since the data is provably identical.

**Undetermined limit**
Two engines returning *different rows* because a `LIMIT` cut through a tie in the `ORDER BY` — several answers are equally correct and no comparison can score the question. Excluded from the denominator. **Not the same as an ambiguous order**, and the difference decides the counting: there the rows match, here they do not, so nothing about the data follows. Establishing it requires proving the key at the cut is genuinely tied — two engines can also disagree about a prefix because they *order the same key differently*, which is a conversion defect wearing the same symptoms.

**Verified gold**
The PostgreSQL statement a benchmark's reference query became, carried out of conversion verification together with the outcome of comparing its results against the original SQLite. The eval runs it rather than re-transpiling, because verification's claim — *these two engines agreed on these rows* — attaches to that statement and not to the procedure that produced it. See [ADR-030](architecture/DECISIONS.md#adr-030--the-eval-runs-the-gold-sql-verification-produced-and-never-re-derives-it).

**Database scope**
The bundle of components a question is answered with: one converted schema, one catalog namespace, one retriever, one validator, resolved from the question's `db_id`. Exists because a benchmark is many databases and every component was built for one — and because Spider's databases share table names, so the failure mode of getting it wrong is a plausible answer rather than an error.

**Answerer**
Anything that turns a question into candidate SQL. The seam the eval harness varies at: each baseline in [EVALUATION.md](ml/EVALUATION.md) §4 is a different answerer over one orchestration, which is why adding one changes nothing in the runner.

**Trust on first use**
Recording an artifact's digest the first time it is seen, then requiring every later copy to match. Used for benchmark archives because neither Spider nor BIRD publishes a stable checksum. Safe only when it is *visible* — it requires a flag, logs a warning, and what it records is committed.

**Split stability**
The property that adding databases to a corpus does not move the ones already assigned to a split. Distinct from "deterministic given a seed", which a shuffle also satisfies while silently rearranging everything when the input list changes.

**Composition root**
The one place in a codebase that constructs the object graph — connections, adapters, components — so every other module receives what it needs rather than building it. `src/composition/` here. It is allowed to know about every layer at once precisely because nothing depends on it.

**Liveness vs readiness**
Two questions an orchestrator asks for two different reasons. Liveness (`/health`) means *is this process alive* — failing it triggers a **restart**. Readiness (`/ready`) means *can it serve* — failing it **removes the replica from the load balancer** and leaves it running. Conflating them is how a thirty-second database blip becomes a fleet restart.

**Deselected**
A test that a marker expression did not match. Distinct from *skipped*: a skip is reported on its own line with a reason, a deselection produces no output at all. The third state, and the reason `pytest -m security` was gating on 156 of 206 tests while reporting green.

---

## Observability

**OpenTelemetry**
A vendor-neutral standard for traces, metrics, and logs.

**Span**
One timed unit of work inside a trace — a retrieval call, a validation attempt, a database execution.

**Trace**
The full tree of spans for one request, showing where the time went across agent, MCP servers, and database.

**`request_id`**
The correlation key on every log line, response body and `X-Request-Id` header. Honoured from the caller when it matches an allowlist so a gateway's trace survives this hop, and **replaced** when it does not — a newline in that header writes a log line the sender chose. See [operations/OBSERVABILITY.md](operations/OBSERVABILITY.md) §2a.

**Log injection**
Writing attacker-chosen content into a log by embedding a newline in a value that reaches it (CWE-117). Matters more than it sounds: it defeats the first step of incident response, which is reading the record.

**Fail closed**
Choosing the restrictive branch when a check cannot decide. `_is_loopback_host` treats an unresolvable address as non-loopback; `Readiness` treats an unconfigured probe set as not-ready. The opposite — failing open — is how a control becomes a formality under exactly the conditions it was written for.
