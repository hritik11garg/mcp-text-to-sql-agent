# Code Style

> **Status: decided now, enforced from Stage 1.** Tooling config lands in `pyproject.toml` with the first code.

Enforced by `ruff` and `mypy` where possible. The rules below are the ones a linter cannot check.

---

## 1. Tooling

| Tool | Role |
|---|---|
| `ruff format` | Formatting — replaces black |
| `ruff check` | Linting + import sorting — replaces flake8 + isort |
| `mypy --strict` | Type checking on `src/` |
| `pytest` | Tests |

```powershell
ruff format . ; ruff check . --fix ; mypy src ; pytest
```

CI runs the same commands. **Style is not reviewed by hand** — if it is not enforced by a tool, it is not a rule.

## 2. Naming

| Thing | Convention | Example |
|---|---|---|
| Module | `snake_case`, singular | `retriever.py` not `retrievers.py` |
| Class | `PascalCase` | `SchemaRetriever` |
| Function | `snake_case`, verb-first | `retrieve_elements`, not `elements` |
| Constant | `UPPER_SNAKE` | `MAX_ROWS_CEILING` |
| Private | leading underscore | `_build_prompt` |
| Async function | no `async_` prefix | the `await` at the call site says it |
| Test | `test_<unit>_<condition>_<expectation>` | `test_validate_sql_rejects_multiple_statements` |

Domain vocabulary matches [../GLOSSARY.md](../GLOSSARY.md) exactly. Something called a "schema element" in the docs is `SchemaElement` in code — not `Column`, not `Field`. Two names for one concept is how a codebase becomes hard to read.

## 3. Folder organization

The tree as built — `README.md` carries the annotated version. Principles it follows:

- **Organize by capability, not by layer.** `retrieval/` containing its model, queries, and service beats `models/` + `services/` + `repositories/` each holding one file per capability. Related code changes together.
- **Each MCP server is its own package** with its own entrypoint — they are separate processes.
- **Shared code goes in a `core/` package** and depends on nothing above it.
- **The demo UI lives in `web/`, outside `src/`.** A different language and toolchain, built to static files that FastAPI serves — keeping it out of the Python package tree means `pip install` and `pytest` never touch it.
- **A top-level package name must not shadow an installed one.** This is a `src` layout, so every package under `src/` is importable as a top-level name for the whole process. Naming one `datasets` would shadow the HuggingFace library for `transformers`, and the failure surfaces as an unrelated library breaking at a distance. The benchmark loader is `benchmark/` for this reason.
- **Offline tools live in `src/` like everything else, and compose only the settings they use.** `benchmark.load` builds `BenchmarkSettings` + `DatabaseSettings` rather than `Settings.load()`, because a tool that validates configuration for a subsystem it never calls gets worked around with a fake value — and a fake value in the environment outlives the command that needed it.
- **No circular imports.** If two modules need each other, the shared piece belongs in `core/`.

## 4. Dependency injection

Dependencies are passed in, not imported at the call site.

```python
# No — untestable without a live database and a real API key
class Agent:
    def __init__(self) -> None:
        self.db = psycopg.connect(os.environ["DATABASE_URL"])
        self.llm = OpenAI(api_key=os.environ["LLM_API_KEY"])


# Yes
class Agent:
    def __init__(self, db: DatabasePool, llm: LLMClient, settings: Settings) -> None:
        self.db = db
        self.llm = llm
        self.settings = settings
```

Rules:
- **Settings are injected**, never read from `os.environ` mid-module. One `Settings` object, constructed at startup, passed down.
- **Depend on protocols, not concrete classes**, at boundaries that get faked in tests (`LLMClient`, `Retriever`).
- FastAPI's `Depends` wires the HTTP layer; constructors wire everything else.

Point being: the agent has to be testable without a live LLM. If constructing it requires an API key, every test costs money and is non-deterministic.

## 5. Type hints

- **Every function annotated**, parameters and return. `mypy --strict` on `src/`.
- **No bare `Any` at module boundaries.** Internally, occasionally justified with a comment saying why.
- **Pydantic models for anything crossing a boundary** — HTTP bodies, MCP tool inputs/outputs, settings. Validation and typing from one definition.
- **`TypedDict` / `Protocol`** for structural typing over inheritance.
- `list[str]`, `dict[str, int]`, `X | None` — modern syntax, no `typing.List`.

## 6. Async rules

The HTTP path is async. **The database layer is not** — this project holds sync `psycopg` connections, and that is a real constraint rather than an oversight, so rule 1 has teeth here and a standing exception is not available.

1. **No blocking calls inside `async def`.** No `requests`, no `time.sleep`, no sync `psycopg`. One blocking call stalls the entire event loop, not just that request.
2. **CPU-bound work goes to a thread.** Embedding inference is CPU-bound: `await asyncio.to_thread(model.encode, texts)`. This is the most likely place to get it wrong, because `model.encode` looks harmless.
3. **Every await has a timeout.** No unbounded `await`. Timeouts come from config, not literals.
4. **Concurrent tool calls use `asyncio.gather`** with `return_exceptions=True` — one failing tool must not cancel the others silently.
5. **Async context managers for resources.** Connections, MCP sessions, HTTP clients. No manual close.
6. **No fire-and-forget tasks.** A bare `asyncio.create_task` without a reference can be garbage-collected mid-flight; keep the reference and await it.

### Rule 1 and the sync database layer

Every connection this process holds is sync `psycopg`. Anything reached from a route therefore has to cross a thread boundary, and "it is only a small query" is not an exemption — it is how the first genuinely slow one gets through.

`Readiness.status()` is the worked example, and it was written wrong first. A readiness probe runs `SELECT 1` on a held connection: sub-millisecond, cached five seconds, obviously harmless. It was called directly from the route.

**The reasoning fails on the case the probe exists for.** `SELECT 1` is fast when the database is healthy. When the peer has gone away, a write to that socket blocks until the OS gives up — and the moment a readiness probe blocks is exactly the moment every other request needs the loop. A probe whose whole job is to report an outage would, during one, stall the process it was reporting on.

```python
detail = await asyncio.to_thread(self._probe_all)
```

One thread for the whole refresh rather than one per probe: they are sequential anyway, and each hop off the loop costs more than the statement it protects.

**This scales up, and is on the list before `POST /v1/query`.** A readiness probe is a `SELECT 1`. A user's query is a two-second analytical aggregate, and running one of those on the loop would stall *every* concurrent request for its whole duration. The endpoint needs the same treatment, and it needs a connection pool for the threads to draw from — see [../operations/SECURITY.md](../operations/SECURITY.md) §13.9 and [../project/TASKS.md](../project/TASKS.md).

**The generalisable rule:** judge a blocking call by its behaviour in the failure it was written to detect, not by its cost in the healthy case.

**It has now been broken twice, and the second time is the more instructive.** `/ready` ran a sync `psycopg` call inline; that was caught by re-reading this page against the code. `QuestionAnswerer.candidate()` did the same with a pgvector query — and it was written `async` from the start, in a module built specifically for the HTTP layer, one commit before the HTTP layer existed. Nothing was wrong with it while its only caller was the sequential eval harness, so no test could have failed, and no review of the module in isolation would flag it.

**A blocking call is only wrong relative to who calls it.** That makes this a rule about *composition*, not about a function, and the practical form is: when a module becomes reachable from an event loop for the first time, re-read every synchronous call in it. `git grep -n 'def .*psycopg\|\.execute(' src/answering src/api` is thirty seconds and would have found it.

## 7. Logging

```python
log.info("sql_validated", attempt=2, stage="explain", cost=1240.5, request_id=rid)
```

- **`structlog`, structured, key-value.** Never f-strings — `f"Validated on attempt {n}"` is not queryable.
- **Event name is a stable identifier** (`sql_validated`), not a sentence. Sentences change; identifiers are what dashboards count.
- **`request_id` and `trace_id` on every line.** Bound once via contextvars, not threaded through every signature.
- **Never log:** secrets, connection strings, result values.
- Levels per [../operations/OBSERVABILITY.md](../operations/OBSERVABILITY.md) §2.

## 8. Exception handling

- **Domain exceptions, not generic ones.** `SQLValidationError`, `RetryBudgetExhausted`, `SchemaElementNotFound` — one base class per package.
- **Never `except Exception: pass`.** Never bare `except:`.
- **Catch narrowly, at the layer that can act.** A retry-able validation error is caught by the retry loop, not by the HTTP handler.
- **Chain with `raise ... from e`.** Losing the original traceback makes production debugging guesswork.
- **Errors crossing the API boundary are sanitized** — internal detail goes to logs, the sanitized envelope goes to the client. See [../architecture/API.md](../architecture/API.md).
- **Tool errors are not exceptions.** An MCP tool failure returns `isError: true` with structured content, because the agent needs to *read* the failure to correct it. An exception gives it nothing. This is the most important rule in this section — see [../architecture/MCP.md](../architecture/MCP.md) §6.

## 9. Comments and docstrings

- **Docstrings on public functions**, one-line summary plus args/returns where non-obvious.
- **Comments explain *why*, never *what*.** The code says what.

```python
# No
# Increment the attempt counter
attempts += 1

# Yes
# EXPLAIN can succeed on a query the executor still rejects (planner and
# executor disagree on some casts), so a validated query can still fail.
```

- **No commented-out code.** Git remembers it.
- **`TODO` carries a name and an issue** or it gets deleted.

## 10. SQL in code

- **Application queries against `agent_meta` use parameter binding.** No f-strings, no `.format()`, no concatenation — no exceptions.
- **Generated SQL is never treated as trusted**, even after validation. It is data until the database accepts it.
- **Identifiers that must be interpolated** (a table name from the catalog) are validated against the catalog first, then quoted with the driver's identifier quoting — never string-formatted.

## 11. Tests

Detail in [TESTING.md](TESTING.md). Style rules:

- **Arrange / Act / Assert**, visually separated.
- **One behaviour per test.** A test asserting five things fails uninformatively.
- **Names state the expectation:** `test_execute_sql_clamps_row_limit_to_ceiling`.
- **No sleeps.** Wait on a condition or use a fake clock.
- **Fixtures build data; tests assert on it.** A fixture that asserts is doing the test's job.
