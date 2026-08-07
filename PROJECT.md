Text-to-SQL Analytics Agent (MCP-native) (AI + SDE; ML with the fine-tuned schema linker) — 5–6 weeks

> **This is the original PRD, kept as written.** It is the spec the build is measured against, not a status page — [ROADMAP.md](docs/project/ROADMAP.md) and the README status block are the authority on what exists. Where the build has deliberately diverged, the reason is recorded rather than the text edited:
>
> - **"point Claude Desktop or any MCP host"** — desktop hosts are now listed *third* in [MCP.md](docs/architecture/MCP.md) §9, behind the project's own client and the open-source Inspector. Making a core capability contingent on one vendor's product decisions conflicts with the constraint in the next paragraph.
> - **Scope grew twice.** A React demo UI was added to Stage 1 after noticing the project had no surface a reader could see; the eval harness gained resumability, which the PRD does not mention and which the free-tier token cap made mandatory.
> - **The API arrived in two slices, not one.** The PRD lists "FastAPI + SSE" as a single item. The non-streaming endpoint landed first with the six containment controls that had to accompany the project's first request-accepting surface; SSE follows. Splitting it was the difference between shipping an endpoint and shipping an endpoint with an unbounded request body.
> - **Stages have not been built in order.** Stage 3 (MCP servers) and much of Stage 2 landed before Stage 1 closed. The cost of that ordering is recorded per stage in ROADMAP.md.

**Constraint: everything required to run, evaluate and demo this project is free and open source.** No paid tier, no proprietary application, no vendor account is a *requirement* at any point. Free-tier hosted models and proprietary local runtimes may be *supported* — they must never be the only path. This is why the LLM sits behind a port with Ollama as a first-class option (ADR-014), why the eval harness is resumable around free-tier token caps, and why the MCP servers are driven by the project's own client rather than by anyone's desktop app.

Stack: Python, MCP SDK (servers + client), FastAPI, PostgreSQL with a read-only sandbox role, sqlglot for AST validation, pgvector for schema retrieval, sentence-transformers (fine-tuned schema linker), SSE streaming, React + TypeScript (demo UI), OpenTelemetry, Spider/BIRD benchmark, pytest

An agent that answers analytical questions in plain English against a real database. Capabilities are exposed as four MCP servers — schema_search (retrieve relevant tables/columns from a large schema), validate_sql (sqlglot AST parse + EXPLAIN, side-effect-free), execute_sql (sandboxed run with row limits and statement timeouts), and profile_table (column stats and sample rows for disambiguation). The agent is an MCP client that discovers tools at runtime rather than calling hardcoded functions. It decomposes multi-step questions ("compare Q3 vs Q4 growth by region and flag anomalies") into several queries plus synthesis, keeps session memory of prior results for follow-ups, self-corrects when the database returns an error, and streams progress over SSE. A fine-tuned schema-linking retriever (contrastive training on question→table/column pairs, measured as Recall@k lift over the off-the-shelf embedding) sits inside the retrieval step. Evaluated on a held-out set for both single-query execution accuracy and multi-step task success.

Helps because it's the only project on this list where SDE, AI, and ML genuinely coexist in one repo: sandboxing, validation, concurrency, and tool-boundary design on the engineering side; agent loop, tool discovery, decomposition, and self-correction on the AI side; and a real training loop with measured retrieval gains on the ML side. MCP makes it runnable by anyone — point Claude Desktop or any MCP host at your servers and query your own database, which is rare enough in a portfolio that people actually try it. It's also the most reliably finishable project in the top tier: every stage produces something demoable, so a bad week doesn't sink the whole build.

Where the interview depth lives — worth knowing before you start, since it shapes design decisions: why validate and execute are separate capabilities (validation must be side-effect-free and freely retryable; execution isn't), what the agent sees when a query times out versus when it's syntactically invalid, how you bound blast radius on a read-only role, and why you fine-tuned the schema linker rather than just retrieving more candidates. If MCP ends up as "I wrapped three functions in a protocol," an interviewer spots it in two minutes — the substance is in the tool contracts, not the wrapper.

Build order (each stage is independently demoable):

Stage	Weeks	Output
Core loop — schema retrieval, generation, validation, sandboxed execution	1.5–2	Working single-query text-to-SQL
Eval harness — Spider/BIRD subset, execution accuracy	0.5	Baseline numbers to improve against
MCP servers + client refactor	0.5–1	Runnable from any MCP host
Agent layer — decomposition, session memory, self-correction	1	Multi-step task success metric
Fine-tuned schema linker	1	Recall@k before/after ablation
Hardening — timeouts, row limits, cost caps, tracing, tests	0.5	Production-style repo

Resume bullets it should earn (fill in your measured numbers):

Built an MCP-native text-to-SQL agent exposing schema search, validation, execution, and profiling as four MCP servers with runtime tool discovery — achieving X% execution accuracy on a held-out Spider/BIRD subset.
Raised schema-linking Recall@5 from X% to Y% by fine-tuning a sentence-transformer with contrastive learning on question→column pairs, measured on a committed eval harness.
Reduced invalid-query rate X%→Y% via a side-effect-free validation tier (sqlglot AST + EXPLAIN) and an error-feedback self-correction loop, with execution confined to a read-only role under row limits and statement timeouts.
Implemented multi-step decomposition with session memory, scoring X% task success on compound analytical questions; SSE streaming and OpenTelemetry tracing across agent steps.

**What is measurable so far, with the bound that stops it being the final number** — see [BENCHMARKS.md](docs/ml/BENCHMARKS.md) for the full rows:

| Bullet | Measured | Why it is not the number yet |
|---|---|---|
| Execution accuracy | **81.4%** (`retrieval-only`, k=30, single model) | 744 of 921 scoreable questions — **15 of 20 databases**, up from 9. The full-split run *pauses on the daily token cap and resumes into the same directory*, so this figure moves rather than being replaced; one more day finishes it. Per-database it spans **54.8% to 100%**, which is a better description of the system than the average. Still not comparable to published Spider numbers |
| Recall@5 baseline | **0.943** (R@1 0.747, R@10 0.984, R@20 **0.998**) | This is the *before*. There is no *after* — Stage 5 has not started. **R@20 stopped being 1.000** at 744 questions: about one question in 600 has no correct table in the top 20. So there is a little headroom rather than none, and the bound on what a fine-tune can buy on Spider is still tight enough to be the argument for BIRD |
| Invalid-query rate | **3.3%** | Pre-correction only. The self-correction loop is Stage 4, so the "X%→Y%" the bullet promises is a single number today |
| Multi-step task success | — | Stage 4. Not started |
| End-to-end latency | **0.6–1.8 s** warm (`execute` 15–21 ms) | Against a 4-table schema. The `< 8 s` budget is met; an earlier 29 s figure turned out to be a cold model load inside the first request, not a provider cost ([ADR-040](docs/architecture/DECISIONS.md#adr-040--startup-opens-the-model-because-naming-it-is-not-loading-it)) |