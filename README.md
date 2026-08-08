# Text-to-SQL Analytics Agent (MCP-native)

> **Status: Stage 1 core loop complete and served with a UI, the Stage 3 MCP layer running, and Stage 2's full-split benchmark finished.** The four servers run and are callable from any MCP host over stdio. Spider's dev split is converted to PostgreSQL, *verified* against its own gold results, indexed, and answered against — **all 921 scoreable questions across all 20 databases, at 79.9% execution accuracy on a single model.** A browser asks a question and watches it being answered: `POST /v1/query` streams `stage`, `sql`, `rows` and `done` events, and the demo UI draws each phase against a real time axis as it arrives. Next is the agent loop. See [BENCHMARKS](docs/ml/BENCHMARKS.md) for what has been measured and what bounds it, [ROADMAP](docs/project/ROADMAP.md) for stage status, [TASKS](docs/project/TASKS.md) for the working checklist.
>
> | Landed | Next |
> |---|---|
> | **The full split, finished — 921 of 921 questions, 20 of 20 databases, 79.9%, zero infrastructure errors** | **The agent loop that drives the discovered tools** — planning, session memory, budget enforcement |
> | **Spider loaded — 20 dev databases converted to Postgres, 19 verified against every gold result** | Authentication, and a *per-client* in-flight cap that needs it |
> | **Four MCP servers over stdio, with runtime `tools/list` discovery** | An eval that measures the **MCP path** — today's accuracy is the direct path only |
> | Postgres 16 + pgvector, Alembic migrations | A `with-validation` run, so the validation tier is measured rather than asserted |
> | **`SELECT`-only role, proven by 30 negative tests — and now proven to be *the role the app connects as*** | Authentication; the API refuses to bind beyond loopback until it exists |
> | Schema catalog — introspection, serialization, embedding | |
> | Retrieval — pgvector ANN, join-path expansion, clamped limits | |
> | Validation — sqlglot AST + `EXPLAIN`, refused at both layers | |
> | Execution — row limits, timeouts, audit trail | |
> | Generation — provider-agnostic, with a model fallback chain | |
> | Profiling — column stats under a documented disclosure budget | |
> | Eval harness — comparison, Recall@k, resumable runs | |
> | **`POST /v1/query`, streaming and non-streaming — a question over HTTP, answered from a real schema** | |
> | **A demo UI over the stream** — generated SQL, rows, and every phase timed against a real axis | |

An agent that answers analytical questions in plain English against a real PostgreSQL database. Capabilities are exposed as **four MCP servers** rather than hardcoded functions, so any MCP host can point at them and query its own database — including the client this project ships.

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

> **Everything required to run, evaluate and demo this project is free and open source.** No paid tier, no proprietary application and no vendor account is a requirement at any point — the LLM runs against a free tier or a local Ollama, the MCP servers are driven by the client this repo ships, and the demo UI is served by the project's own API.

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

> Those two paragraphs describe the finished system. **Today**: the four servers and runtime discovery are built and tested, and the HTTP layer serves `/health`, `/ready`, a sanitized error envelope and **`POST /v1/query`** — a question in, generated SQL and rows out. SSE streaming is served too. Decomposition, session memory, self-correction and the fine-tune are not built. The status block at the top of this file is the authority on what exists.

Why validation and execution are separate capabilities, how blast radius is bounded on the read-only role, and why the linker was fine-tuned rather than over-retrieved are all recorded in [DECISIONS.md](docs/architecture/DECISIONS.md).

## Demo

> **There is a page now.** `npm run build` in `web/`, point `API_STATIC_DIR` at `web/dist`, and `http://127.0.0.1:8000/` asks a question and draws each phase against a real time axis as the events arrive. Exact commands and expected output: [DEMO_SCRIPT.md](docs/project/DEMO_SCRIPT.md).
>
> The `curl` below is the same thing without a browser, and it is still the fastest way to confirm the service works.

![Asking a question and watching it answered](docs/assets/demo.gif)

*Recorded against Spider's `concert_singer` schema. The question is English; everything below it is the system's own output.*

**What to look at, in the order it appears.** The **generated SQL** arrives before any row does — a `JOIN` with a `GROUP BY` and a `HAVING`, which the model wrote and which validation and the read-only role both had to pass. Then the rows. Then, down the left, **how long each phase actually took**, drawn to scale.

That rail is the point. It is a time axis, not a progress bar: `retrieve` 344 ms, `generate` 930 ms, `execute` 27 ms — and the segments are that different in length. A stepper with four checkmarks would look identical whether execution took 27 milliseconds or twenty seconds, which is precisely how a model checkpoint load once hid inside an aggregate here and got written up as a slow provider ([ADR-040](docs/architecture/DECISIONS.md#adr-040--startup-opens-the-model-because-naming-it-is-not-loading-it)).

The footer carries a **second** set of timings — the server's own, measured inside the process. The rail is what the browser observed, network included. They are different quantities, so both are shown and neither is quietly substituted for the other ([ADR-044](docs/architecture/DECISIONS.md#adr-044--two-clocks-both-reported-neither-substituted-for-the-other)).

```console
$ curl -s -X POST http://127.0.0.1:8000/v1/query -H 'Content-Type: application/json'     -d '{"question": "How many singers are there?"}'
{
  "sql": "SELECT COUNT(*) FROM singer;",
  "columns": ["count"], "rows": [[6]], "row_count": 1,
  "truncated": false, "executed": true,
  "steps": [{"stage": "answer",  "duration_ms": 1542.0, "status": "ok"},
            {"stage": "execute", "duration_ms": 10.6,   "status": "ok"}],
  "usage": {"input_tokens": 501, "output_tokens": 43}
}
```

Or streamed, which is the same work reported as it happens — the generated SQL arrives before any row does:

```console
$ curl -sN -X POST http://127.0.0.1:8000/v1/query -H 'Content-Type: application/json'       -d '{"question": "How many singers are there?", "stream": true}'
event: stage
data: {"stage":"retrieve","status":"ok"}

event: stage
data: {"stage":"generate","status":"ok"}

event: sql
data: {"sql":"SELECT COUNT(*) FROM singer;","attempt":1}

event: stage
data: {"stage":"execute","status":"ok"}

event: rows
data: {"columns":["count"],"rows":[[6]],"truncated":false}

event: done
data: {"row_count":1,"executed":true,"steps":[...],"usage":{...}}
```

Run against Spider's `concert_singer` schema, converted and loaded by this repo's own benchmark loader. Ask for `max_rows: 3` on a larger result and the limit is injected into the AST, with `truncated: true` on the way back — a client is never handed a clipped result that claims to be complete.

**Those timings replaced a recording of 29,081 ms**, which was narrated as a rate-limited provider and was actually a model checkpoint loading inside the first request. The per-stage events added for streaming are what separated retrieval from generation and showed it ([PERFORMANCE.md](docs/operations/PERFORMANCE.md) §1).

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
   /health /ready                                     ^
   error envelope                                     |
   request correlation            startup proves this role cannot write
```

Everything in that sketch exists except the thing it is drawn around: the **agent loop**. The MCP servers, the database layer, the FastAPI process and `POST /v1/query` are built — a request is answered end to end. What is missing is the path that reaches the tools *through MCP* rather than calling the components directly, plus the decomposition and self-correction that path is for.

## Features

> These describe the finished system. The status block at the top of this file is the authority on what exists today.

- **Runtime tool discovery** — the agent lists tools from each MCP server on connect; adding a capability does not require an agent code change.
- **Side-effect-free validation tier** — sqlglot AST parse plus `EXPLAIN`, so invalid SQL is caught and retried without ever touching the executor.
- **Error-feedback self-correction** — database errors are fed back as structured tool results, not swallowed.
- **Multi-step decomposition** — compound questions ("compare Q3 vs Q4 growth by region and flag anomalies") become several queries plus a synthesis step.
- **Session memory** — prior results are addressable in follow-up questions.
- **Fine-tuned schema linker** — contrastive sentence-transformer over question→column pairs, with a committed before/after ablation.
- **Bounded blast radius** — read-only role, statement timeouts, row limits, cost caps. The role is verified at startup, not assumed: a control tested only against the fixture that satisfies it has been tested against itself.
- **SSE streaming + OpenTelemetry** — progress visible per agent step; traces span agent → MCP → database.
- **A UI that shows the work, not just the answer** — the generated SQL, whether the row limit clipped the result, and each phase drawn against a real time axis. Built because an aggregate hid a 20-second model load once, and a split timing found it.

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
pytest                    # ~1,310 tests; integration, security and contract need Docker
pytest -m security        # the containment suite, on its own — 255 tests
ruff check . ; mypy
```

The Postgres-backed tests skip cleanly without a running Docker daemon. In CI that is not good enough: *skipped* and *passed* look alike, so the pipeline must fail if the security suite did not actually run.

**There is a third state, and it is worse than either.** A test carrying no marker is not skipped — it is silently *deselected* by any `-m` run and reported by nothing. Thirteen files had drifted that way, including the archive-extraction and DSN-redaction suites, so the security gate was running 156 tests while believing it covered the layer. Layer markers are now derived from the directory a test lives in (`tests/conftest.py`), because `tests/security/` **is** the security layer and a hand-written marker can disagree with the path while a derived one cannot. A test outside the known layers fails collection rather than disappearing.

**A container runtime is needed for the tests, not to run the project.** `testcontainers` talks to any Docker-compatible socket, so **Podman**, **Rancher Desktop**, **colima** or plain **Docker Engine** all work — worth knowing because Docker Desktop is free for personal use but its licence requires a paid subscription for larger commercial organisations, and it is the one dependency here that is not free for everybody. Running the project itself needs a PostgreSQL 16 with pgvector from anywhere: a system install, a container, or a hosted free tier.

## Usage

**As MCP servers — available now.** Each is a module launched over stdio:

```powershell
python -m mcp_servers.schema_search
python -m mcp_servers.validate_sql
python -m mcp_servers.execute_sql
python -m mcp_servers.profile_table
```

Three ways to drive them, **none of which costs anything or needs an account** ([MCP.md](docs/architecture/MCP.md) §9):

| | |
|---|---|
| **The project's own client** | `ToolRegistry` — what the contract suite drives, so it is exercised on every test run |
| **MCP Inspector** | `npx @modelcontextprotocol/inspector` — open-source browser UI, installs nothing permanently |
| **Any desktop MCP host** | Claude Desktop and others work; one option, not a requirement |

They need both database URLs and an indexed catalog. They do **not** need an LLM key, since they are called by a model rather than calling one.

**As an HTTP service — the foundation is running.**

```powershell
python -m api                                          # or, while developing:
uvicorn api.app:create_app --factory --reload
```

```
GET /health   →  200 {"status": "ok"}
GET /ready    →  200 {"status": "ready", "dependencies": {...}}   or 503
```

`POST /v1/query` answers in both shapes — one JSON body, or `text/event-stream` with `stream: true`. The table in [API.md](docs/architecture/API.md) says which routes and which *events* answer today and which are still design intent.

**It has no authentication, and it refuses to start on any address but loopback until it does.** Not a warning — a `ConfigurationError` before the socket is bound. An endpoint that runs model-generated SQL against your database and spends your token budget should not become reachable because somebody was debugging. To deploy it, publish the port from a container runtime or front it with a proxy that authenticates, so exposing it is a decision somebody made. [SECURITY.md §13](docs/operations/SECURITY.md) reviews the boundary in full.

Startup also **proves the read-only role cannot write** rather than trusting `DATABASE_RO_URL` to name one. Thirty negative tests already asserted what `sql_agent_ro` may do; none of them asserted that the application *connects as that role*, and the only check that existed compared the two connection strings for inequality — which two spellings of the same superuser pass. `assert_read_only` now asks PostgreSQL's privilege functions directly, and it asks rather than attempting a write, because the deployment this catches is exactly the one where a test `INSERT` would succeed.

**With the demo UI.** Build it once, then point the API at the output:

```powershell
cd web; npm ci; npm run build       # produces web/dist
$env:API_STATIC_DIR = "$PWD\..\web\dist"
python -m api                       # http://127.0.0.1:8000/ now serves the page
```

While developing the UI, run Vite instead and leave `API_STATIC_DIR` empty — `npm run dev` proxies `/v1` to the API, so the browser still talks to one origin:

```powershell
cd web; npm run dev                 # http://localhost:5173
```

**Both arrangements keep `API_CORS_ORIGINS` empty**, and that is the reason for both. The page and the API share an origin, so no browser origin has to be trusted by an endpoint that has no authentication. A separately hosted UI would need an entry in that list; this one never does.

`API_STATIC_DIR` is empty by default because `web/dist` is a build artifact and is absent from a fresh checkout. Set it to a path that is missing, is not a directory, or holds no `index.html`, and **the process refuses to start** rather than serving 404s while reporting itself healthy.

Still planned:

- **CLI** — eval harness and training runs via `typer` entrypoints.

## Folder structure

Directories marked *(stub)* exist with a docstring stating which stage fills them, so the intended shape is visible without pretending the code is there. Ones marked *(planned)* do **not** exist yet — they are in this tree to show where something will go, and the distinction is drawn because a listing that implies a directory is present when it is not is the same defect as a doc claiming work that was not done.

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
│   │   ├── llm/                # OpenAI-compatible + fake + factory
│   │   └── embedding/          # sentence-transformer + hashing + factory
│   ├── schema/                 # introspection, serialization, sensitivity, indexer, retrieval
│   ├── generation/             # sql_gen prompt + generator (pure prompt building)
│   ├── validation/             # sqlglot AST stages + EXPLAIN, no I/O below stage 5
│   ├── execution/              # row limits, timeouts, audit trail
│   ├── answering/              # retrieve → generate, shared by the eval and the API
│                               # so a benchmark number describes the served system
│   ├── profiling/              # column statistics under a disclosure budget
│   ├── mcp_servers/            # four servers + the shared error contract
│   ├── evals/                  # comparison, Recall@k, artifacts, resumable runs
│   ├── benchmark/              # acquire, SQLite→Postgres, verify the conversion, splits
│   ├── agent/                  # tools/list discovery; planner and memory — Stage 4
│   ├── composition/            # the dependency graph, built once per process
│                               # shared by the MCP servers and the API, which are peers
│                               # one connection for stdio, a pool for HTTP
│   └── api/                    # FastAPI — /health, /ready, POST /v1/query,
│                               # the error envelope, the body cap,
│                               # sse.py — event framing, which is a security control
│                               # static.py — serving the UI, its CSP, and why
│                               #   there is no catch-all mount
├── web/                        # React + TypeScript demo UI over the SSE stream
│   ├── src/api/                # the SSE parser and the typed event contract
│   ├── src/state/              # the reducer — all of the logic, none of the DOM
│   ├── src/sql/                # a tokenizer, so highlighting needs no innerHTML
│   └── src/components/         # TimeRail draws each phase against a real axis
├── .github/
│   └── pull_request_template.md
├── tests/
│   ├── unit/                   # no I/O, fakes throughout
│   ├── integration/            # real Postgres via testcontainers
│   ├── contract/               # servers as subprocesses over real stdio
│   └── security/               # negative tests — the role MUST be denied
├── data/                       # benchmark data, gitignored — except artifacts.lock.json
│                               # and splits/, which are the reproducibility record
├── results/                    # eval artifacts, gitignored — real rows, see SECURITY §14.2.8
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

> **The full-split run is complete: all 921 scoreable questions, all 20 databases, one model, zero infrastructure errors.** It took three days and three free-tier daily budgets, resuming into the same results directory each time. Each figure is single-database execution accuracy, *not* Spider's stricter Test Suite Accuracy, and 113 of the split's 1034 questions are excluded with reasons. Every measurement is recorded in [BENCHMARKS.md](docs/ml/BENCHMARKS.md) with the commit and command it came from; nothing appears here that is not traceable to a row there.

| Metric | Baseline | Current | Stage |
|---|---|---|---|
| **Conversion fidelity** (Spider dev, 1034 questions) | — | **99.3%** — 915 / 921, 19 of 20 databases fully verified | 2 |
| **Execution accuracy** (Spider dev, **all 20 DBs**, `k=30`) | 42.7% @ `k=10` | **79.9%** — 736 / 921, single model, `retrieval-only` | 2 |
| Schema-linking Recall@5 | — | **0.9435** (R@1 0.7445, R@10 0.9828, R@20 0.9973) | 5 |
| Schema-linking Recall@10 | — | **0.9828** — the fine-tune's target is R@1, not coverage | 5 |
| Invalid-query rate | 20.7% | **1.4%** — pre-correction; the retry loop is Stage 4 | 4 |
| Cost per question | — | **$0.00** free tier · **0.018 cents** at list price † | 2 |
| Multi-step task success | TBD | TBD | 4 |

**79.9% is an average over databases that range from 100% to 54.8%, and the average is the least useful number here.** `poker_player` is a clean 40/40; `car_1` is 46/84. Three databases sit below 63%. A 45-point spread across schemas in the same corpus, measured by the same code on the same day, means **which schema a user brings decides more than the headline does** — the per-database table is in [BENCHMARKS §1.1](docs/ml/BENCHMARKS.md).

**The number went down when the run finished, which is why partial numbers are not quoted here.** It read 81.4% at 744 questions. The remaining 177 were not a random sample — they were whatever the walk had not reached — and they brought it to 79.9%. A partial run is a biased sample of its own corpus and the direction of the bias is unknowable until it completes.

**Recall@20 = 0.9973 bounds what Stage 5 can buy on Spider.** It read exactly 1.000 over 150 questions and again over 379, then fell at every widening. The headroom at rank 20 is **0.27 points**; at R@5 it is 5.65. A retriever that already finds nearly every needed element by rank 20 can mostly only be improved into finding them *sooner*: **R@1 at 0.7445 is where the ~24 points of headroom actually are.** That is the argument for BIRD, whose schemas are large enough for retrieval to fail properly, and the shape of the null result [R-01](docs/project/RISKS.md) predicted.

† **All prices here are standard on-demand rates recorded on 2026-08-08** — not batch, not cached-input, not priority. Batch tiers are commonly **half** the standard rate and are not modelled.

**Reproducing the whole benchmark costs 17 cents, or nothing.** The default configuration uses a free tier, which is why the row above says `$0.00`. At the standard on-demand rate of the model that actually produced these numbers — `openai/gpt-oss-120b` on Groq, $0.15 in / $0.60 out per 1M — one full 921-question run bills **$0.17**, and $0.04 to $6.49 across the models a reader might substitute. Nothing else in this repository makes an LLM call — the API, the MCP servers, the UI and the entire test suite are free to run — so **the cost of the project is the cost of evaluating it**.

Two things worth knowing beyond the headline. **Total spend is ~2.3× a single reproduction**, because the corpus has been answered eight times across smoke runs, defect fixes and baseline changes, and a configuration change means starting from scratch. And **the free tier's real cost was three days and a resumption mechanism**, not money — several days of engineering exist to avoid a seventeen-cent bill, which is defensible only because it buys reproducibility by strangers rather than economy. [BENCHMARKS §6](docs/ml/BENCHMARKS.md) prices all of it, and `python -m evals.cost` regenerates the table rather than trusting a number somebody typed.

**Two things this run does not measure.** It runs `retrieval-only`, so **no validator executes** — the 13 invalid queries reached PostgreSQL and were refused there. And it exercises the **direct** answering path, the same one the HTTP API uses; **nothing here goes through the MCP servers.** They are proven to work by the contract suite and proven to answer as well by nothing.

**Nothing here measures self-correction either, and no baseline can.** The `with-validation` configuration is a *gate*: it validates once and drops a query that fails, with no retry and no feedback to the model. Error-feedback self-correction is Stage 4 and does not exist in any code path, so `validation_attempts` is 0 or 1 and never more.

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

Ideas deliberately out of scope for v1 are tracked in [FUTURE.md](docs/project/FUTURE.md) — GraphRAG over the schema, hybrid retrieval, a fine-tuned generator, result caching.

It also carries the **scale and concurrency path**, which is worth reading before deploying this for more than one person: everything in v1 is bounded for a *single caller*, deliberately, and that section records what changes when there is more than one — a real connection pool, two-tier interactive/batch execution, per-user admission control, and why tenant isolation has to reach the retrieval layer rather than stopping at the database role.

## License

MIT. See [LICENSE](LICENSE).
