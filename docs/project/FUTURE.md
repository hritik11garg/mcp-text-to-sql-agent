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
