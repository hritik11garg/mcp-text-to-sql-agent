# Future

Ideas deliberately **not** in v1. Each records why it was cut, so "not implemented" is distinguishable from "not thought about."

Cut reasons: `scope` (good, doesn't fit 6 weeks) · `unproven` (needs a measurement v1 will produce) · `complexity` (cost exceeds benefit at this scale) · `blocked` (needs something v1 lacks)

---

## Retrieval

### Cross-encoder reranking
`unproven` · Rerank the bi-encoder's top-50 with a cross-encoder that scores query and element jointly. Higher precision at the top.

**Why not v1.** It *adds* latency to a component with a < 100 ms budget, and it only pays off if retrieval precision is the bottleneck. Ablation A5 answers that. Doing it before A5 would be optimizing a component that may not be limiting anything.

### Hybrid retrieval (BM25 + dense)
`unproven` · Fuse lexical and semantic scores.

**Why not v1.** Dense retrieval handles paraphrase; BM25 handles exact identifier matches — a question naming a column literally should retrieve it trivially. Plausibly complementary, but the failure taxonomy from Stage 2 should say whether exact-match misses are actually happening before building for them.

### GraphRAG over the schema graph
`complexity` · Treat the schema as a graph and traverse foreign keys during retrieval rather than returning flat elements.

**Why not v1.** The cheap version — returning FK edges alongside matched elements — is already in v1 and captures most of the value. Full graph traversal is a substantial subsystem, justified only if join errors dominate the failure taxonomy.

### Schema summarization
`scope` · LLM-generated column descriptions where comments are absent or useless.

**Why not v1.** Retrieval quality depends heavily on serialized text, and BIRD columns often have cryptic names with no comments. Ablation A4 measures how much comments contribute — if a lot, this becomes high-priority.

---

## Generation

### Fine-tuned SQL generator
`scope` · Fine-tune a smaller model on question→SQL.

**Why not v1.** A different order of magnitude of work than the retriever fine-tune, and the frontier model is not the bottleneck at this stage. Interesting mainly as a cost play once accuracy is established.

### Few-shot example retrieval
`scope` · Retrieve similar solved questions and include them as examples.

**Why not v1.** Likely a real accuracy win and genuinely cheap — this is the strongest candidate for the first post-v1 addition. Cut only because it needs a corpus of verified question→SQL pairs, which Stage 2 produces as a by-product.

### Query plan feedback
`scope` · Feed the `EXPLAIN` plan back so the model can rewrite an expensive query rather than having it rejected.

**Why not v1.** v1 uses cost only as a bail-out signal. Turning it into a rewriting loop is a genuinely interesting extension of self-correction — the model reasoning about *its own query's cost* rather than its correctness.

---

## Execution

### Result caching
`scope` · Cache result sets keyed by normalized SQL.

**Why not v1.** Correctness depends on cache invalidation, and this service does not know when the target data changes. A stale analytical answer presented confidently is worse than a slow correct one. Needs a TTL policy or a change-detection mechanism.

### Distributed execution
`complexity` · Route queries across read replicas.

**Why not v1.** Solves a scale problem that does not exist yet. The load tests in Stage 6 will say whether a single database is actually the constraint.

### Incremental / streaming results
`scope` · Stream rows as they arrive rather than materializing the full set.

**Why not v1.** Meaningful only for large result sets, which the row limit prevents by design.

---

## Scale and concurrency

Everything in v1 is bounded for a **single caller**. That is a deliberate consequence of the deployment model rather than an oversight, and this section records what changes when there is more than one.

**The deployment shape is already decided, and it is not per-machine.** A desktop install needs `DATABASE_RO_URL` and `LLM_API_KEY` on every workstation, which distributes the credentials the entire containment argument in [../operations/SECURITY.md](../operations/SECURITY.md) rests on, with no revocation story and an audit table written by clients nobody controls. The intended shape is one centrally deployed service holding the credentials, reached over HTTP/SSE — which is why [../architecture/MCP.md](../architecture/MCP.md) §2 puts Streamable HTTP with the API layer, "which is where a network-reachable endpoint first needs authentication anyway." The stdio transport keeps one legitimate audience: a developer pointing the tools at their own database from their own MCP host. Both configurations run the same code because the tool boundary is a protocol ([ADR-003](../architecture/DECISIONS.md#adr-003--mcp-for-the-tool-boundary)).

### A real connection pool
`blocked` · Hand each concurrent query its own connection.

**Why not v1.** No concurrent caller exists — the API layer is Stage 1 and the MCP servers are stdio, one subprocess per client. The seam is cut and waiting: `SQLExecutor` depends on `ConnectionSource`, whose only method is `connection() -> AbstractContextManager[Connection]`, which is exactly `psycopg_pool.ConnectionPool.connection()`'s signature. Introducing a pool is a wiring change rather than a rewrite, and until then "a pool with one client is machinery without a job." **Until it lands, two concurrent `execute_sql` calls would contend on a single connection** — a real bug, currently unreachable.

### Two-tier execution: interactive and batch
`scope` · Route a query to a background job when its planned cost says it cannot finish interactively.

**Why not v1.** v1 has one tier and hard ceilings — `STATEMENT_TIMEOUT_CEILING_MS` 60 s, `AGENT_TIMEOUT_MS` 120 s, both clamps rather than defaults — so an hour-long join cannot happen. That is correct for a synchronous API, where one slow query holds a pool slot and a handful of them are an outage. It is also insufficient for real analytics, where some questions are legitimately slow and "narrow your query" is not always an available answer.

**The routing signal already exists and is currently thrown away.** `SQLValidator` computes `estimated_cost` from `EXPLAIN` on every query and uses it only to *reject* — `cost_exceeded` above `MAX_ESTIMATED_COST`. The same number routes: below the ceiling, today's synchronous path; above it, offer a job rather than a refusal, run it on a **separate worker pool** with its own much larger timeout, and notify on completion. The separate pool is the point — it is what stops one user's long job from consuming an interactive slot.

There is a third answer worth building before either: the plan is already parsed, so the agent can say *"this is a full scan of 40M rows because there is no index on `orders.created_at`"*, which is more useful than running it or refusing it.

### Per-user admission control
`scope` · Cap in-flight queries and request rate per user, not just globally.

**Why not v1.** No users to distinguish. It is nonetheless **the highest-value concurrency control**, above any amount of pool tuning: total simultaneous queries is `replicas × DB_POOL_MAX_SIZE`, and with no per-user cap one client firing twenty questions holds every slot. A per-user in-flight limit converts "one user degrades everyone" into "one user degrades themselves." [../architecture/API.md](../architecture/API.md) already specifies the `429 rate_limited` response and [../operations/DEPLOYMENT.md](../operations/DEPLOYMENT.md) has the unchecked box; neither is built.

### Provider-side rate budgeting
`unproven` · Treat the model provider's tokens-per-minute as the scarce resource and schedule against it.

**Why not v1.** Because it was assumed to be a scale problem and turned out to be a *v1* problem — measured, not predicted. The eval harness is sequential and single-user, and it still hit HTTP 429s against a free tier's daily cap. With concurrent users the provider saturates well before ten database connections do, and the fallback chain then changes *which model answers*, which [../ml/BENCHMARKS.md](../ml/BENCHMARKS.md) §1 shows changes accuracy by tens of points. Any concurrency design has to budget the provider, not just the pool — and [../operations/PERFORMANCE.md](../operations/PERFORMANCE.md) §4 currently asks only about the pool.

### Role-level resource limits
`scope` · Set the timeout on the database role as well as the session.

**Why not v1.** `ALTER ROLE sql_agent_ro SET statement_timeout = '60s'` means an application bug cannot exceed the ceiling either. Defence in depth, and the pattern is already proven here: migration 002 sets `default_transaction_read_only` on the role, and a Stage 2 test confirmed it fires *ahead* of the privilege check — two independent controls, outer one first. Cut from v1 only because the session-level clamp is sufficient for a single caller.

---

## Agent

### Clarifying-question loop
`blocked` · When a question is genuinely ambiguous, ask the user rather than guessing.

**Why not v1.** Blocked by [ADR-008](../architecture/DECISIONS.md#adr-008--sse-instead-of-websockets) — SSE is unidirectional, so mid-stream client input is not possible. Would need a separate endpoint or a transport change. v1 detects ambiguity and reports it; it cannot resolve it interactively.

### Persistent cross-session memory
`scope` · Remember a user's preferred metric definitions and common filters across sessions.

**Why not v1.** Session memory is scoped to one conversation. Cross-session memory raises real questions about staleness and about what it means for the answer when the agent remembers a definition the user has since changed.

### Multi-database queries
`complexity` · Answer questions spanning several databases.

**Why not v1.** Requires federation or a join layer above the databases. Large enough to be its own project.

### Visualization
`scope` · Emit chart specs alongside result sets.

**Why not v1.** Genuinely useful for analytics and a strong demo addition. Cut purely for time.

---

## Security and multi-tenancy

### Row-level security / per-tenant roles
`scope` · Restrict which rows a given user can see.

**Why not v1.** The **most significant limitation of v1** and the one that would matter most in production. Today, one read-only role sees the entire target schema, so anyone who can call the API can query anything it can read. Documented as a limitation in [../operations/SECURITY.md](../operations/SECURITY.md) §4 rather than left implicit.

### Column-level masking
`scope` · Redact PII columns from retrieval and results.

**Why not v1.** Same reason, and it interacts with retrieval: a masked column should arguably not be retrievable at all, or the model will write SQL against something it cannot read.

### Tenant-aware retrieval
`scope` · Scope the schema catalog to what the caller is entitled to see, not just what they may execute against.

**Why not v1.** Single-tenant, so there is nothing to scope to. Recording it because it is the part of multi-tenancy that is easy to miss: **the catalog is itself a disclosure surface.** Row-level security and per-tenant roles constrain *execution*, and retrieval happens before execution — so a user can learn that a `salaries` table exists, and what its columns are called, from a search that never runs a query. The model will describe it helpfully before anything refuses it.

Isolation therefore has to reach the retrieval layer, which is a filter on `schema_elements` and an authorization input the search path does not currently take. Much cheaper to design now than to retrofit: `dataset` is already the scoping column, and [../operations/SECURITY.md](../operations/SECURITY.md) §14.2.10 already notes that if `dataset` ever becomes a per-request value it turns into a tenant-isolation control and needs authorization behind it. This is that control, named.

### Query approval workflow
`scope` · Human review before expensive or sensitive queries execute.

**Why not v1.** The architecture already supports it — `validate_sql` and `execute_sql` are separate capabilities, so an approval gate slots naturally between them. This is a case where a v1 design decision made a future feature cheap.

---

## Evaluation

### LLM-as-judge for multi-step grading
`unproven` · Automate rubric grading of compound-question answers.

**Why not v1.** Needs validation against human grading before it can be trusted, and v1's multi-step eval set is small enough to grade by hand. An unvalidated automatic grader produces numbers that look rigorous and are not.

### Spider 2.0-DBT as a multi-step benchmark
`scope` · 68 repository-level tasks over DuckDB, run locally with no account and no cost.

**Why not v1.** Spider 2.0's expected output is CSV files rather than SQL, and it releases only a small amount of gold SQL — so neither execution accuracy nor Recall@k, both computed from a reference query, can be produced from it as the harness stands. It also needs the agent layer to exist: solutions are multi-query workflows over 1,000+ column schemas. Worth revisiting at Stage 4 as an alternative to the hand-authored compound set in [DATASETS.md](../ml/DATASETS.md) §6, which has a real self-authorship bias problem that an external benchmark would not.

### Test Suite Accuracy
`scope` · Score against several randomly generated databases per question rather than one, which is Spider's official metric.

**Why not v1.** It catches coincidental matches — a wrong query that happens to return the right rows on the one instance that exists — so the number it produces is strictly lower and strictly better. Not v1 because it needs a database generator that perturbs contents while preserving the schema and constraints, which is a project of its own, and because the single-database number is the one that stays comparable to the majority of published work. The consequence is recorded rather than hidden: [EVALUATION.md](../ml/EVALUATION.md) §2 states that numbers here are the more generous of the two, and every BENCHMARKS row names its metric.

### Continuous evaluation
`scope` · Run the eval on every merge, track drift over time.

**Why not v1.** Cost, and it needs a stable baseline first. The smoke split is the v1 compromise.

---

## Post-v1 ordering

If the project continued, roughly in order of value per unit of effort:

1. **Few-shot example retrieval** — highest expected accuracy gain, and Stage 2 already produces the corpus.
2. **Row-level security** — the limitation that most restricts real use.
3. **Query plan feedback** — natural extension of the self-correction loop that already exists.
4. **Cross-encoder reranking** — *if and only if* A5 shows retrieval precision is the bottleneck.
5. **Visualization** — cheap, high demo value.

If instead the goal is to put this in front of more than one person, the order is different and mostly cheaper:

1. **A real connection pool** — the seam exists; without it, concurrency is a correctness bug rather than a performance one.
2. **Per-user admission control** — largest effect per line of code, and it is what makes the service *operable* under load rather than merely fast.
3. **Tenant-aware retrieval** — decide before the catalog schema hardens further, because retrofitting an authorization filter is much more expensive than designing one.
4. **Two-tier execution** — the routing signal is already computed; this is mostly a worker and a job table.

Note that only the last is about making anything faster. The first three are about a slow or hostile query staying one person's problem.
