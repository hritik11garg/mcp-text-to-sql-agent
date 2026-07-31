# Text-to-SQL Analytics Agent (MCP-native)

> **Status: Stage 1 — core loop, in progress.** Database, read-only role, the schema catalog, and retrieval are in and tested; generation, validation, and the API are not yet. Nothing below claims a benchmark number until the eval harness exists. See [ROADMAP](docs/project/ROADMAP.md) for stage status and [TASKS](docs/project/TASKS.md) for the working checklist.
>
> | Landed | Next |
> |---|---|
> | Postgres 16 + pgvector, Alembic migrations | SQL generation behind the `LLMClient` port |
> | `SELECT`-only role, proven by 30 negative tests | sqlglot AST validation + `EXPLAIN` |
> | Schema catalog — introspection, serialization, embedding | Sandboxed execution under row and time limits |
> | Retrieval — pgvector ANN, join-path expansion, clamped limits | FastAPI + SSE |
> | Provider-agnostic `LLMClient` and `Embedder` ports | MCP servers wrap all of the above in Stage 3 |

An agent that answers analytical questions in plain English against a real PostgreSQL database. Capabilities are exposed as **four MCP servers** rather than hardcoded functions, so any MCP host — Claude Desktop, or your own client — can point at them and query its own database.

---

## Table of contents

- [Overview](#overview)
- [Demo](#demo)
- [Architecture](#architecture)
- [Features](#features)
- [Installation](#installation)
- [Usage](#usage)
- [Folder structure](#folder-structure)
- [Benchmarks](#benchmarks)
- [Documentation](#documentation)
- [Future improvements](#future-improvements)
- [License](#license)

---

## Overview

Natural-language analytics tools usually fail in one of two ways: they hallucinate SQL against schemas they only half-retrieved, or they run that SQL with enough privilege to do damage. This project separates those concerns explicitly.

Four MCP servers expose the agent's capabilities:

| Server | Responsibility | Side effects |
|---|---|---|
| `schema_search` | Retrieve relevant tables/columns from a large schema | None |
| `validate_sql` | sqlglot AST parse + `EXPLAIN` | **None — freely retryable** |
| `execute_sql` | Sandboxed run under row limits and statement timeouts | Reads only, read-only role |
| `profile_table` | Column stats and sample rows for disambiguation | None |

The agent is an **MCP client** that discovers these tools at runtime. It decomposes multi-step questions, keeps session memory for follow-ups, self-corrects when the database returns an error, and streams progress over SSE.

A fine-tuned schema-linking retriever (contrastive training on question→table/column pairs) sits inside the retrieval step, with Recall@k measured against the off-the-shelf embedding baseline.

Why validation and execution are separate capabilities, how blast radius is bounded on the read-only role, and why the linker was fine-tuned rather than over-retrieved are all recorded in [DECISIONS.md](docs/architecture/DECISIONS.md).

## Demo

> **TBD — Stage 1.** Demo GIF, screenshots, and the Claude Desktop walkthrough land once the core loop is demoable. Exact commands and expected output: [DEMO_SCRIPT.md](docs/project/DEMO_SCRIPT.md).

## Architecture

> **TBD — Stage 1.** Diagram to be committed at `docs/assets/architecture.png` and embedded here. Component breakdown, data flow, and sequence diagrams: [SYSTEM_ARCHITECTURE.md](docs/architecture/SYSTEM_ARCHITECTURE.md).

```
  NL question
      |
      v
  +-----------+     tool discovery / calls (MCP)     +------------------+
  |   Agent   | <----------------------------------> |  schema_search   |
  |  (client) |                                      |  validate_sql    |
  |           |                                      |  execute_sql     |
  |           |                                      |  profile_table   |
  +-----------+                                      +------------------+
      |                                                       |
      | SSE progress                                          v
      v                                              PostgreSQL (read-only role,
   FastAPI                                            pgvector, row + time limits)
```

## Features

> These describe the finished system. The status block at the top of this file is the authority on what exists today.

- **Runtime tool discovery** — the agent lists tools from each MCP server on connect; adding a capability does not require an agent code change.
- **Side-effect-free validation tier** — sqlglot AST parse plus `EXPLAIN`, so invalid SQL is caught and retried without ever touching the executor.
- **Error-feedback self-correction** — database errors are fed back as structured tool results, not swallowed.
- **Multi-step decomposition** — compound questions ("compare Q3 vs Q4 growth by region and flag anomalies") become several queries plus a synthesis step.
- **Session memory** — prior results are addressable in follow-up questions.
- **Fine-tuned schema linker** — contrastive sentence-transformer over question→column pairs, with a committed before/after ablation.
- **Bounded blast radius** — read-only role, statement timeouts, row limits, cost caps.
- **SSE streaming + OpenTelemetry** — progress visible per agent step; traces span agent → MCP → database.

## Installation

**Requires Python 3.12** (3.13 also works; 3.14 is not recommended — see [DECISIONS.md](docs/architecture/DECISIONS.md)).

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt
```

PostgreSQL 16+ with the `pgvector` extension is required. Docker Compose setup, environment variables, and the read-only role bootstrap: [DEPLOYMENT.md](docs/operations/DEPLOYMENT.md) and [DATABASE.md](docs/architecture/DATABASE.md).

Copy `.env.example` to `.env` and fill in the values described in [CONFIG.md](docs/operations/CONFIG.md).

Bring the database up and apply the migrations:

```powershell
docker compose up -d postgres
python -m alembic upgrade head
```

That creates the `agent_meta` schema, the pgvector extension and the HNSW index, and the `SELECT`-only role the agent runs as.

Verify the install — the security suite is the one that matters, and it passes by being **refused**:

```powershell
pytest                    # 181 tests; integration and security need Docker
pytest -m security        # the read-only containment suite, on its own
ruff check . ; mypy
```

The Postgres-backed tests skip cleanly without a running Docker daemon. In CI that is not good enough: *skipped* and *passed* look alike, so the pipeline must fail if the security suite did not actually run.

## Usage

> **TBD — Stage 1/3.** End-to-end query commands land with the core loop; MCP host config lands with the refactor.

Planned entrypoints:

- **HTTP API** — `POST /query`, streaming progress over SSE. See [API.md](docs/architecture/API.md).
- **MCP host** — point Claude Desktop (or any MCP host) at the four servers. Contracts and config: [MCP.md](docs/architecture/MCP.md).
- **CLI** — eval harness and training runs via `typer` entrypoints.

## Folder structure

Directories marked *(stub)* exist with a docstring stating which stage fills them, so the intended shape is visible without pretending the code is there.

```
.
├── PROJECT.md                  # PRD — objective, scope, timeline, success metrics
├── README.md                   # this file
├── CHANGELOG.md · CONTRIBUTING.md · LICENSE
├── pyproject.toml              # ruff, mypy, pytest, coverage — one config file
├── requirements.txt · requirements-dev.txt
├── docker-compose.yml          # Postgres 16 + pgvector, with a healthcheck
├── alembic.ini
├── migrations/
│   └── versions/               # 001 extensions + agent_meta · 002 read-only role
│                               # 003 schema_elements uniqueness fix
├── src/
│   ├── core/
│   │   ├── settings.py         # typed config, validated at startup
│   │   ├── exceptions.py       # domain error hierarchy
│   │   └── ports/              # LLMClient, Embedder — protocols the app depends on
│   ├── adapters/
│   │   ├── llm/                # fake + factory; OpenAI-compatible adapter next
│   │   └── embedding/          # sentence-transformer + hashing + factory
│   ├── schema/                 # introspection, serialization, sensitivity, indexer, retrieval
│   ├── validation/             # (stub) sqlglot AST validation
│   ├── execution/              # (stub) sandboxed execution under limits
│   ├── profiling/              # (stub) column statistics
│   ├── mcp_servers/            # (stub) four servers — Stage 3
│   ├── agent/                  # (stub) planner, executor, memory — Stage 4
│   └── api/                    # (stub) FastAPI + SSE
├── tests/
│   ├── unit/                   # no I/O, fakes throughout
│   ├── integration/            # real Postgres via testcontainers
│   └── security/               # negative tests — the role MUST be denied
└── docs/
    ├── GLOSSARY.md
    ├── architecture/           # SYSTEM_ARCHITECTURE, API, MCP, DATABASE, DECISIONS
    ├── ml/                     # TRAINING, EVALUATION, BENCHMARKS, DATASETS, PROMPTS
    ├── operations/             # SECURITY, DEPLOYMENT, OBSERVABILITY, PERFORMANCE,
    │                           # CONFIG, TROUBLESHOOTING
    ├── development/            # CODE_STYLE, TESTING
    └── project/                # ROADMAP, TASKS, RISKS, FUTURE, DEMO_SCRIPT
```

## Benchmarks

> **TBD — Stage 2 onward.** No numbers are claimed until the eval harness is committed and reproducible. Every measurement is recorded in [BENCHMARKS.md](docs/ml/BENCHMARKS.md) with the commit it came from.

| Metric | Baseline | Current | Stage |
|---|---|---|---|
| Execution accuracy (held-out Spider/BIRD subset) | TBD | TBD | 2 |
| Schema-linking Recall@5 | TBD | TBD | 5 |
| Schema-linking Recall@10 | TBD | TBD | 5 |
| Invalid-query rate | TBD | TBD | 4 |
| Multi-step task success | TBD | TBD | 4 |

## Documentation

The two documents most worth reading first are [SYSTEM_ARCHITECTURE.md](docs/architecture/SYSTEM_ARCHITECTURE.md) (what the pieces are) and [DECISIONS.md](docs/architecture/DECISIONS.md) (why they are that way).

Each document is **filled in as its stage lands** — one that would otherwise contain invented numbers carries an explicit `TBD — Stage N` marker instead. Stage numbers refer to [ROADMAP.md](docs/project/ROADMAP.md).

| Document | Contents | Filled in |
|---|---|---|
| [GLOSSARY](docs/GLOSSARY.md) | Every term used across these docs | Done |
| **Architecture** | | |
| [SYSTEM_ARCHITECTURE](docs/architecture/SYSTEM_ARCHITECTURE.md) | Diagram, components, data flow, tradeoffs, scalability | Stage 1 |
| [API](docs/architecture/API.md) | Every endpoint: request, response, errors, examples | Stage 1 |
| [MCP](docs/architecture/MCP.md) | Server contracts, tool definitions, discovery, versioning | Stage 3 |
| [DATABASE](docs/architecture/DATABASE.md) | ER diagram, tables, indexes, read-only role, migrations | Stage 1 |
| [DECISIONS](docs/architecture/DECISIONS.md) | Decision log with alternatives and tradeoffs | Continuous |
| **Machine learning** | | |
| [TRAINING](docs/ml/TRAINING.md) | Dataset, pipeline, hyperparameters, loss, ablations, export | Stage 5 |
| [EVALUATION](docs/ml/EVALUATION.md) | Metric definitions, harness design, failure taxonomy | Stage 2 |
| [BENCHMARKS](docs/ml/BENCHMARKS.md) | Append-only log of every measured run | Stage 2+ |
| [DATASETS](docs/ml/DATASETS.md) | Spider, BIRD, splits, licenses | Stage 2 |
| [PROMPTS](docs/ml/PROMPTS.md) | System, planner, SQL, retry, summarizer prompts | Stage 1 |
| **Operations** | | |
| [SECURITY](docs/operations/SECURITY.md) | Threat model, SQL/prompt injection, secrets, audit | Stage 1 / 6 |
| [DEPLOYMENT](docs/operations/DEPLOYMENT.md) | Docker, Compose, production setup, scaling, health checks | Stage 6 |
| [OBSERVABILITY](docs/operations/OBSERVABILITY.md) | Logging, tracing, metrics, alerts, dashboards | Stage 6 |
| [PERFORMANCE](docs/operations/PERFORMANCE.md) | Latency targets and measured results | Stage 6 |
| [CONFIG](docs/operations/CONFIG.md) | Every env var, feature flag, limit, and default | Stage 1 |
| [TROUBLESHOOTING](docs/operations/TROUBLESHOOTING.md) | Docker, Postgres, pgvector, MCP, LLM, training failures | Continuous |
| **Development** | | |
| [CODE_STYLE](docs/development/CODE_STYLE.md) | Naming, DI, logging, exceptions, type hints, async rules | Stage 1 |
| [TESTING](docs/development/TESTING.md) | Philosophy, test layers, coverage policy | Stage 1 / 6 |
| **Project** | | |
| [ROADMAP](docs/project/ROADMAP.md) | Stages with completion percentages | Continuous |
| [TASKS](docs/project/TASKS.md) | The working checklist | Continuous |
| [RISKS](docs/project/RISKS.md) | What could sink the build, with mitigations | Continuous |
| [FUTURE](docs/project/FUTURE.md) | Deliberately out of scope for v1 | Continuous |
| [DEMO_SCRIPT](docs/project/DEMO_SCRIPT.md) | Exact commands, questions, expected output | Stage 1+ |

**Conventions.** No number appears anywhere that is not traceable to a [BENCHMARKS](docs/ml/BENCHMARKS.md) row. Decisions are logged when made, not reconstructed afterwards. Benchmarks are append-only — new rows, never edits.

## Future improvements

Ideas deliberately out of scope for v1 are tracked in [FUTURE.md](docs/project/FUTURE.md) — GraphRAG over the schema, hybrid retrieval, a fine-tuned generator, distributed execution, result caching.

## License

MIT. See [LICENSE](LICENSE).
