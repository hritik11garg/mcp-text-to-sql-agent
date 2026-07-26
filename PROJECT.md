Text-to-SQL Analytics Agent (MCP-native) (AI + SDE; ML with the fine-tuned schema linker) — 5–6 weeks

Stack: Python, MCP SDK (servers + client), FastAPI, PostgreSQL with a read-only sandbox role, sqlglot for AST validation, pgvector for schema retrieval, sentence-transformers (fine-tuned schema linker), SSE streaming, OpenTelemetry, Spider/BIRD benchmark, pytest

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