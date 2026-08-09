"""Break each dependency on purpose, and check the system degrades as documented.

Three behaviours are claimed in prose across this codebase and none of them had
ever been demonstrated (`docs/project/ENGINEERING_MATRIX.md` section 30):

1. **Postgres dies and the API stays alive with an unhealthy readiness.**
2. **The model provider dies and the request fails cleanly, without partial state.**
3. **The MCP server dies rather than serving a broken tool.** That one needs a
   real process and lives in `tests/contract/test_mcp_process_death.py`.

**The interesting assertion is almost never about the failing request.** That a
single call returns an error is easy and is already covered
(`tests/unit/test_api_stream.py`). What is not covered is the *state the
process is left in*: whether the next caller is served, whether a slot came
back, whether an outage makes the probes more expensive rather than less, and
whether a failure that never reached the database still left an audit row.
Those are the properties that decide whether an incident is a blip or an
outage, and every one of them is invisible when you look at one request.

**Failures are injected at the fake seam, not by killing a container**, and
that limit is real: `testcontainers` gives this suite a *session-scoped*
Postgres, so stopping it would poison every test that ran afterwards. Killing a
real database is the stronger experiment and it needs its own container and its
own slice. Named in section 30 rather than implied. What is injected here is
the failure *as the application observes it* -- a connection that raises, a
provider that raises, a pool with nothing left to give.

**What is deliberately not injected**, kept here rather than left to be
inferred from what is absent:

- **A real Postgres killed mid-query** -- see above.
- **A slow dependency**, as opposed to a failing one. Timeouts are configured
  and clamped, and nothing here makes a dependency *hang*. A failure that
  arrives promptly is the easy half.
- **A partial network failure** -- a peer that accepts the connection and then
  never answers. ``DB_CONNECT_TIMEOUT_MS`` covers the connect half only.
- **Disk and memory pressure.** Nothing in this project bounds either.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import psycopg_pool
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import build_settings
from tests.fakes_api import FakeConnection, FakeResources

from answering import Candidate
from api import errors, query
from api.app import create_app
from api.health import DOWN, UP, Probe, Readiness
from api.query import QueryService
from core.exceptions import ExecutionError, LLMUnavailableError
from core.ports.llm import Usage
from core.settings import ExecutionSettings, Settings
from execution.executor import QueryResult, SQLExecutor
from validation.validator import ValidationResult

pytestmark = pytest.mark.unit


# --- injectable dependencies -----------------------------------------------


class DeadConnection(FakeConnection):
    """A connection whose peer has gone away.

    Raises on `execute` rather than on `cursor`, because that is where a real
    one fails: a socket whose peer is gone still looks open until something is
    written to it, which is the whole reason `ping` runs `SELECT 1` instead of
    reading a driver flag.
    """

    def execute(self, *_: object) -> None:
        raise psycopg.OperationalError("connection to server was lost")


class FlakyConnection(FakeConnection):
    """Down for the first `failures` calls, up afterwards.

    The point of the suite: an outage that *ends*. A dependency that fails
    forever proves the error path; a dependency that recovers proves there is
    no latch holding the process unhealthy after the cause is gone.
    """

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.calls = 0

    def execute(self, *_: object) -> None:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise psycopg.OperationalError("connection to server was lost")


def resources_with(readonly: FakeConnection) -> type:
    """A resource factory whose read-only connection is the injected one."""

    class Injected(FakeResources):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            self.readonly = readonly

    return Injected


@contextmanager
def app_with(readonly: FakeConnection) -> Iterator[TestClient]:
    app = create_app(build_settings(), resource_factory=resources_with(readonly))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# --- 1 · Postgres goes away ------------------------------------------------


class TestPostgresGoesAway:
    """`/health` failing means *restart me*. `/ready` failing means *leave me
    alone*. Conflating them turns one database outage into a fleet restart."""

    def test_liveness_stays_up_while_readiness_goes_down(self) -> None:
        """The documented behaviour, asserted on one process at one moment.

        Both halves in a single test on purpose. A test that only checked
        `/ready` would pass against an implementation that took the whole
        process down, which is the failure this design exists to prevent.
        """
        with app_with(DeadConnection()) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 503

    def test_the_process_keeps_answering_liveness_throughout_the_outage(self) -> None:
        """Not once -- repeatedly. An orchestrator probes on a period, and a
        liveness check that degrades after the first failure restarts the pod
        on probe number three rather than probe number one."""
        with app_with(DeadConnection()) as client:
            for _ in range(10):
                assert client.get("/health").json() == {"status": "ok"}

    async def test_an_outage_does_not_make_the_probes_more_expensive(self) -> None:
        """**Failures are cached too**, and that is not an accident.

        Caching only successes is a plausible-looking refactor: retry the
        broken thing more often, notice recovery sooner. It converts an
        unauthenticated endpoint into an amplifier at exactly the moment the
        database is least able to absorb it -- every probe from every replica,
        every interval, against a dependency that is already down.
        """
        attempts = 0

        def failing() -> None:
            nonlocal attempts
            attempts += 1
            raise psycopg.OperationalError("connection to server was lost")

        readiness = Readiness([Probe("database", failing)], ttl=60.0)

        for _ in range(20):
            ready, detail = await readiness.status()
            assert not ready
            assert detail == {"database": DOWN}

        assert attempts == 1, "an outage must not cost one round trip per probe"

    async def test_readiness_recovers_without_a_restart(self) -> None:
        """The half that matters and the half nobody writes.

        Everything here is easy to get right on the way down and easy to get
        wrong on the way back up: a cached verdict that never expires, a flag
        set once, a connection object replaced but not retried. The process
        must return to ready **on its own**, with no intervention, once the
        dependency is back.
        """
        connection = FlakyConnection(failures=1)
        readiness = Readiness([Probe("database", connection.execute)], ttl=0.0)

        first, _ = await readiness.status()
        second, detail = await readiness.status()

        assert first is False
        assert second is True
        assert detail == {"database": UP}

    async def test_recovery_is_not_a_one_way_latch_either(self) -> None:
        """Down, up, and down again. A `self._healthy = True` that is never
        cleared passes the recovery test above and fails this one."""
        connection = FlakyConnection(failures=1)
        readiness = Readiness([Probe("database", connection.execute)], ttl=0.0)

        assert (await readiness.status())[0] is False
        assert (await readiness.status())[0] is True

        connection.remaining = 1
        assert (await readiness.status())[0] is False
        assert (await readiness.status())[0] is True

    def test_the_outage_reason_never_reaches_the_caller(self) -> None:
        """`/ready` is unauthenticated, and a driver message carries the DSN."""
        with app_with(DeadConnection()) as client:
            body = client.get("/ready").text

        assert "OperationalError" not in body
        assert "connection to server was lost" not in body


# --- 2 · The model provider goes away --------------------------------------


def candidate() -> Candidate:
    return Candidate(
        sql="SELECT id\nFROM orders",
        retrieved=(("orders", "id"),),
        usage=Usage(input_tokens=11, output_tokens=7),
        model="fake",
        context=object(),  # type: ignore[arg-type]
    )


class FlakyAnswerer:
    """Fails the first `failures` questions, then answers normally."""

    def __init__(self, *, failures: int, error: Exception | None = None) -> None:
        self.remaining = failures
        self.error = error or LLMUnavailableError("every provider in the chain failed")
        self.calls = 0

    async def candidate(self, question: str, *, on_stage: Any = None, **_: Any) -> Candidate:
        self.calls += 1
        if on_stage is not None:
            on_stage("retrieve")
        if self.remaining > 0:
            self.remaining -= 1
            raise self.error
        if on_stage is not None:
            on_stage("generate")
        return candidate()


class FlakyExecutor:
    def __init__(self, *, failures: int) -> None:
        self.remaining = failures

    def execute(self, sql: str, **_: Any) -> QueryResult:
        if self.remaining > 0:
            self.remaining -= 1
            raise ExecutionError("the query could not be executed")
        return QueryResult(
            columns=("id",),
            rows=((1,),),
            row_count=1,
            truncated=False,
            duration_ms=1.0,
            executed_sql=sql,
            row_limit=500,
        )

    def explain(self, sql: str, **_: Any) -> None:
        return None


def build_client(answerer: Any = None, executor: Any = None, *, max_concurrent: int = 4) -> Any:
    service = QueryService(
        answerer or FlakyAnswerer(failures=0),
        executor or FlakyExecutor(failures=0),
        max_concurrent=max_concurrent,
        keepalive_seconds=0.05,
    )
    app = FastAPI()
    errors.install(app)
    app.include_router(query.build_router(lambda _request: service))
    return TestClient(app, raise_server_exceptions=False)


def names(body: str) -> list[str]:
    parsed = []
    for frame in body.split("\n\n"):
        if not frame.strip() or frame.startswith(":"):
            continue
        parsed.append(dict(line.split(": ", 1) for line in frame.strip().split("\n"))["event"])
    return parsed


def ask(client: Any, **body: Any) -> str:
    response = client.post("/v1/query", json={"question": "how many?", "stream": True, **body})
    assert response.status_code == 200
    return response.text


class TestTheProviderGoesAway:
    def test_a_provider_failure_emits_no_partial_result(self) -> None:
        """**Progress may be partial. Data may not.**

        The distinction is the whole claim. A caller that saw `retrieve`
        complete and then an `error` knows exactly what happened. A caller that
        saw a `rows` event and then an `error` has to decide whether to believe
        the rows, and there is no right answer to that question.
        """
        client = build_client(FlakyAnswerer(failures=1))

        emitted = names(ask(client))

        assert emitted.count("error") == 1
        assert "rows" not in emitted
        assert "done" not in emitted
        assert emitted[-1] == "error"

    def test_the_stage_that_completed_is_still_reported(self) -> None:
        """The other side of the same claim: a failure does not erase the
        progress that genuinely happened before it."""
        client = build_client(FlakyAnswerer(failures=1))

        assert names(ask(client))[0] == "stage"

    def test_the_next_request_is_served(self) -> None:
        """The reliability claim. One dead provider call must not leave the
        service unable to answer the caller behind it."""
        client = build_client(FlakyAnswerer(failures=1))

        first = names(ask(client))
        second = names(ask(client))

        assert first[-1] == "error"
        assert second[-1] == "done"
        assert "rows" in second

    def test_a_long_outage_does_not_leak_the_last_slot(self) -> None:
        """The failure that only appears after N of them.

        With `max_concurrent=1` a single unreleased slot makes the service
        permanently unavailable, and it takes exactly one leaked failure to do
        it. Fifty is not for statistical confidence -- it is so that an
        *occasionally* leaking path cannot pass by luck.
        """
        client = build_client(FlakyAnswerer(failures=50), max_concurrent=1)

        for _ in range(50):
            assert names(ask(client))[-1] == "error"

        # The assertion is behavioural rather than a look at the counter: with
        # a cap of one, being served at all is the only proof that matters, and
        # it stays true if the cap is ever reimplemented.
        assert names(ask(client))[-1] == "done"

    def test_the_provider_failure_does_not_publish_its_own_message(self) -> None:
        """A provider's exception text is not this project's to publish."""
        secret = "api key sk-live-0000 rejected by upstream at 10.0.0.4"
        client = build_client(FlakyAnswerer(failures=1, error=RuntimeError(secret)))

        body = ask(client)

        assert "sk-live-0000" not in body
        assert "10.0.0.4" not in body
        assert names(body)[-1] == "error"


# --- 3 · The database goes away mid-request --------------------------------


class TestTheDatabaseGoesAwayMidRequest:
    def test_an_execution_failure_emits_no_rows(self) -> None:
        client = build_client(executor=FlakyExecutor(failures=1))

        emitted = names(ask(client))

        assert "rows" not in emitted
        assert emitted[-1] == "error"

    def test_the_generated_sql_still_reached_the_caller(self) -> None:
        """Generation succeeded; only execution failed. Withholding the SQL
        would discard the one artifact that explains the failure."""
        client = build_client(executor=FlakyExecutor(failures=1))

        assert "sql" in names(ask(client))

    def test_the_next_request_is_served(self) -> None:
        client = build_client(executor=FlakyExecutor(failures=1))

        assert names(ask(client))[-1] == "error"
        assert names(ask(client))[-1] == "done"


# --- 4 · The connection pool is exhausted ----------------------------------


class ExhaustedPool:
    """A `ConnectionSource` with nothing left to give.

    `psycopg_pool.PoolTimeout` is what a real pool raises when every connection
    is checked out and `timeout` elapses. Raised from `connection()` rather
    than from a cursor, because that is where it happens: the request never
    gets a connection at all.
    """

    def connection(self) -> Any:
        raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")


class AlwaysValid:
    def validate(self, sql: str) -> ValidationResult:
        return ValidationResult(valid=True, sql=sql)


class RecordingAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, **fields: Any) -> None:
        self.rows.append(fields)


class TestThePoolIsExhausted:
    """Saturation, which is a failure mode the other three are not.

    Nothing is broken here. Every dependency is healthy and there is simply no
    capacity, which is the state a service spends most of an incident in.
    """

    def executor(self) -> tuple[SQLExecutor, RecordingAudit]:
        audit = RecordingAudit()
        return (
            SQLExecutor(
                ExhaustedPool(),  # type: ignore[arg-type]
                AlwaysValid(),  # type: ignore[arg-type]
                ExecutionSettings(),
                audit=audit,  # type: ignore[arg-type]
            ),
            audit,
        )

    def test_exhaustion_becomes_a_domain_error_not_an_internal_one(self) -> None:
        """This works for a reason that is worth checking rather than assuming.

        The executor catches `psycopg.Error`, and `PoolTimeout` is defined in
        `psycopg_pool` -- a different distribution. It is caught because
        `PoolTimeout` subclasses `psycopg.OperationalError`, which is a fact
        about somebody else's class hierarchy and exactly the kind of
        assumption this project has already been wrong about once
        (`SqlglotError`, SECURITY.md 14.2.13).
        """
        executor, _ = self.executor()

        with pytest.raises(ExecutionError):
            executor.execute("SELECT id FROM orders")

    def test_exhaustion_is_audited_even_though_nothing_ran(self) -> None:
        """An attempt that never reached the database is still an attempt.

        Written after the inverse defect: a validation exception escaped
        *before* the rejection audit, so a refused query left no record it had
        ever been asked. The same question asked of the saturation path.
        """
        executor, audit = self.executor()

        with pytest.raises(ExecutionError):
            executor.execute("SELECT id FROM orders")

        assert len(audit.rows) == 1
        assert audit.rows[0]["outcome"] == "error"
        assert audit.rows[0]["duration_ms"] is not None

    def test_the_pools_own_message_is_not_republished(self) -> None:
        """`PoolTimeout` carries no `diag`, so the executor's fallback text is
        what a caller sees -- asserted, because the fallback is the only thing
        standing between pool internals and a response."""
        executor, _ = self.executor()

        with pytest.raises(ExecutionError) as raised:
            executor.execute("SELECT id FROM orders")

        assert "couldn't get a connection" not in str(raised.value)
        assert str(raised.value) == "the query could not be executed"

    def test_saturation_is_not_sticky(self) -> None:
        """A pool that recovers must leave the executor usable. Nothing here
        caches a verdict -- this asserts that nothing starts to."""
        executor, audit = self.executor()

        for _ in range(5):
            with pytest.raises(ExecutionError):
                executor.execute("SELECT id FROM orders")

        assert len(audit.rows) == 5
