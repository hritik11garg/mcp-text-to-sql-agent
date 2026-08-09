"""The in-flight cap, with more than one caller in flight.

The concurrency controls in this project were all designed for a situation
that had never happened. `_Admission` exists because a `429` is not
expressible after a `200`; the pool exists because `statement_timeout` is
transaction-scoped and two requests sharing a connection would bound each
other's queries. Both are correct arguments. Neither had ever been checked
with two requests actually running at once.

The existing admission tests (`tests/unit/test_api_stream.py`) all have the
shape *one caller holds the only slot, a second is refused*. That proves the
counter increments. It does not prove that four callers at a cap of four are
all admitted, that a fifth is **refused rather than queued**, or that a burst
of twenty leaves the cap fully available afterwards.

**"Refused rather than queued" is the assertion that needs concurrency to
mean anything.** A queue and a refusal are indistinguishable when requests
arrive one at a time — both eventually answer everybody. They differ only
when the slots are full and someone else arrives, and the difference is the
whole design: a queue turns an overload into latency that every caller pays,
and a caller that has not received a response cannot back off from it.

**Driven through `QueryService` directly, not over HTTP.** Admission lives
there, the route's translation to `429` is already covered by the API suites,
and an in-process ASGI client would mean depending on the *transitive* `httpx`
rather than the `httpx2` this project pins deliberately. `asyncio.gather` is
real concurrency and adds nothing to the dependency set.

**Every await that could block is bounded by `asyncio.wait_for`.** That is not
defensive style, it is what makes these tests useful when they fail: the
property under test is *"the caller is refused rather than made to wait"*, so
the failure mode of a broken implementation is an await that never returns.
An unbounded version of these tests hangs the suite instead of reporting which
assertion broke -- verified by mutating the cap to never refuse, which hung
until the bound was added.

**This is the test `locust` was pinned for**, in `requirements-dev.txt`, with
the comment "concurrency + timeout behaviour under load" — and nothing ever
imported it. The pin is removed in the same commit as this file. Load
*generation* against a running server measures throughput and soak, which is
Stage 6 work with no pipeline to run it in; the behaviour §28 actually named
first is a correctness property, and correctness properties belong in a test
that runs on every commit rather than in a tool somebody remembers to launch.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

from answering import Candidate
from api.errors import RATE_LIMITED, ApiError
from api.query import QueryService
from api.schemas import QueryRequest
from core.exceptions import ExecutionError
from core.ports.llm import Usage
from core.settings import ExecutionSettings
from execution.executor import QueryResult, SQLExecutor
from validation.validator import ValidationResult

pytestmark = pytest.mark.unit

CAP = 4


def candidate() -> Candidate:
    return Candidate(
        sql="SELECT id FROM orders",
        retrieved=(("orders", "id"),),
        usage=Usage(input_tokens=1, output_tokens=1),
        model="fake",
        context=object(),  # type: ignore[arg-type]
    )


class Gate:
    """Holds every caller until released, and records the high-water mark.

    The peak is the measurement the suite exists for. Counting *completed*
    requests proves nothing about a cap -- a service with no cap at all
    completes them too. Only the number in flight **at the same moment**
    distinguishes admission control from a queue.

    Incrementing without a lock is correct here and is a property of asyncio
    rather than of this class: there is no suspension point between the read
    and the write, so no other coroutine can observe the value in between.
    Same reasoning as `_Admission` itself, which is why it is stated rather
    than assumed.
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        self.in_flight = 0
        self.peak = 0
        self.error = error
        self._released = asyncio.Event()
        self._full: asyncio.Event | None = None
        self._target = 0

    def expect(self, n: int) -> asyncio.Event:
        """An event set once `n` callers are simultaneously inside."""
        self._target = n
        self._full = asyncio.Event()
        return self._full

    def release(self) -> None:
        self._released.set()

    def rearm(self) -> None:
        """Close the gate again, for a test that runs several rounds."""
        self._released = asyncio.Event()

    async def candidate(self, question: str, *, on_stage: Any = None, **_: Any) -> Candidate:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        if self._full is not None and self.in_flight >= self._target:
            self._full.set()
        try:
            await self._released.wait()
            if self.error is not None:
                raise self.error
            return candidate()
        finally:
            self.in_flight -= 1


class InstantExecutor:
    def execute(self, sql: str, **_: Any) -> QueryResult:
        return QueryResult(
            columns=("id",),
            rows=((1,),),
            row_count=1,
            truncated=False,
            duration_ms=0.1,
            executed_sql=sql,
            row_limit=500,
        )

    def explain(self, sql: str, **_: Any) -> None:
        return None


def build(gate: Gate, *, cap: int = CAP) -> QueryService:
    return QueryService(gate, InstantExecutor(), max_concurrent=cap, keepalive_seconds=0.05)  # type: ignore[arg-type]


def ask(service: QueryService, tag: str) -> Any:
    return asyncio.create_task(service.answer(QueryRequest(question="how many?"), request_id=tag))


class TestTheCapUnderRealConcurrency:
    async def test_exactly_the_cap_runs_at_once(self) -> None:
        """Four callers, a cap of four, all four inside simultaneously.

        The cap is a *limit*, not a target, so a service that serialised
        everything would satisfy every existing admission test. Only the peak
        tells them apart.
        """
        gate = Gate()
        service = build(gate)
        full = gate.expect(CAP)

        tasks = [ask(service, f"r{i}") for i in range(CAP)]
        await asyncio.wait_for(full.wait(), timeout=5)
        gate.release()
        await asyncio.gather(*tasks)

        assert gate.peak == CAP

    async def test_the_overflow_is_refused_while_the_others_are_still_running(self) -> None:
        """**Refused, not queued** -- and the timing is the assertion.

        The refusals are collected *before* the blocked callers are released,
        so a queueing implementation could not produce this result: it would
        still be holding those six, and the test would time out rather than
        fail. That is deliberate, and it is why the release comes after the
        assertion rather than before it.
        """
        gate = Gate()
        service = build(gate)
        full = gate.expect(CAP)

        held = [ask(service, f"held{i}") for i in range(CAP)]
        await asyncio.wait_for(full.wait(), timeout=5)

        refusals = []
        for index in range(6):
            try:
                # Bounded on purpose. A queueing implementation would *block*
                # here rather than raise, and an unbounded await would hang the
                # whole suite instead of reporting which property broke.
                await asyncio.wait_for(
                    service.answer(QueryRequest(question="?"), request_id=f"over{index}"),
                    timeout=2,
                )
            except ApiError as exc:
                refusals.append(exc)

        assert len(refusals) == 6
        assert all(exc.error is RATE_LIMITED for exc in refusals)
        assert gate.in_flight == CAP, "an overflow request reached the answerer"

        gate.release()
        await asyncio.gather(*held)

    async def test_every_slot_returns_after_a_burst(self) -> None:
        """Twenty at once against a cap of four, then the cap again.

        A slot leaked once is invisible; leaked on the path a burst takes, it
        is a service that degrades under exactly the load the cap exists for
        and recovers only on restart.
        """
        gate = Gate()
        service = build(gate)
        full = gate.expect(CAP)

        burst = [ask(service, f"b{i}") for i in range(20)]
        await asyncio.wait_for(full.wait(), timeout=5)
        gate.release()
        settled = await asyncio.gather(*burst, return_exceptions=True)

        refused = [r for r in settled if isinstance(r, ApiError)]
        assert len(refused) == 20 - CAP

        # **The same service.** The first version of this test built a fresh
        # one for the second batch, which proves a new counter starts at zero
        # and says nothing about whether the old one came back down. Mutating
        # `_Admission.release` into a no-op left it passing.
        gate.rearm()
        full_again = gate.expect(CAP)
        second = [ask(service, f"s{i}") for i in range(CAP)]
        await asyncio.wait_for(full_again.wait(), timeout=5)
        gate.release()
        await asyncio.gather(*second)

        assert gate.peak == CAP

    async def test_a_burst_of_failures_returns_every_slot_too(self) -> None:
        """The error path is where a leaked slot actually happens.

        The success path releases in the ordinary flow and gets exercised by
        every other test in the suite. `finally` is the only thing covering
        this one, and `finally` is what gets lost in a refactor.
        """
        gate = Gate(error=ExecutionError("provider is down"))
        service = build(gate)

        for _ in range(5):
            full = gate.expect(CAP)
            tasks = [ask(service, f"f{i}") for i in range(CAP)]
            await asyncio.wait_for(full.wait(), timeout=5)
            gate.release()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(o, ExecutionError) for o in outcomes)
            gate.rearm()

        assert gate.in_flight == 0

    async def test_streams_and_plain_requests_draw_on_one_cap(self) -> None:
        """A second counter would be a second policy, and the effective limit
        would be whichever one happened to be checked first."""
        gate = Gate()
        service = build(gate)
        full = gate.expect(CAP - 1)

        held = [ask(service, f"a{i}") for i in range(CAP - 1)]
        await asyncio.wait_for(full.wait(), timeout=5)

        service.stream(QueryRequest(question="?", stream=True), request_id="stream")

        # Bounded, like the overflow loop above and for the same reason: an
        # implementation that queued instead of refusing would block here
        # forever, and a hung test reports nothing. `wait_for` turns that into
        # a `TimeoutError` naming this test.
        with pytest.raises(ApiError) as refused:
            await asyncio.wait_for(
                service.answer(QueryRequest(question="?"), request_id="over"), timeout=2
            )

        assert refused.value.error is RATE_LIMITED

        gate.release()
        await asyncio.gather(*held)


# --- the pool, under the same pressure -------------------------------------


class CountingSource:
    """A connection source that records simultaneous borrows.

    The peak matters because the settings enforce
    ``API_POOL_MAX_SIZE > API_MAX_CONCURRENT_REQUESTS`` at startup -- so the
    cap is what is supposed to keep the pool from ever being the thing that
    blocks. That relationship is validated as *configuration*; this checks it
    holds as *behaviour*, which is a different claim.

    Borrow accounting is guarded by a lock because this one really is touched
    from several threads: the executor is synchronous and reaches it through
    ``asyncio.to_thread``.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self._lock = threading.Lock()
        self.borrowed = 0
        self.returned = 0
        self.live = 0
        self.peak = 0
        self._fail = fail

    @contextmanager
    def connection(self) -> Any:
        with self._lock:
            self.borrowed += 1
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            yield _Connection(fail=self._fail)
        finally:
            with self._lock:
                self.live -= 1
                self.returned += 1


class _Connection:
    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    @contextmanager
    def transaction(self) -> Any:
        yield

    @contextmanager
    def cursor(self) -> Any:
        yield _Cursor(fail=self._fail)


class _Cursor:
    description = ()

    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    def execute(self, statement: str, params: object = None) -> None:
        del params
        if self._fail and not statement.lstrip().upper().startswith("SELECT SET_CONFIG"):
            raise psycopg.OperationalError("connection to server was lost")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class AlwaysValid:
    def validate(self, sql: str) -> ValidationResult:
        return ValidationResult(valid=True, sql=sql)


class TestThePoolIsNeverTheBottleneck:
    async def test_simultaneous_borrows_never_exceed_the_in_flight_cap(self) -> None:
        """What the startup validator promises, observed rather than assumed.

        `API_POOL_MAX_SIZE > API_MAX_CONCURRENT_REQUESTS` is enforced as
        configuration. It only *means* anything if one request never holds two
        connections -- which is true today and is exactly the kind of thing a
        future change (a second query for a follow-up, a profiling call inside
        answering) would break silently.
        """
        source = CountingSource()
        executor = SQLExecutor(source, AlwaysValid(), ExecutionSettings())  # type: ignore[arg-type]

        await asyncio.gather(
            *(asyncio.to_thread(executor.execute, "SELECT id FROM orders") for _ in range(CAP))
        )

        assert source.peak <= CAP
        assert source.borrowed == CAP

    async def test_every_connection_comes_back_even_when_the_query_fails(self) -> None:
        """A connection leaked on the error path exhausts a pool of eight in
        eight failures, and every request after that blocks on a checkout."""
        source = CountingSource(fail=True)
        executor = SQLExecutor(source, AlwaysValid(), ExecutionSettings())  # type: ignore[arg-type]

        outcomes = await asyncio.gather(
            *(asyncio.to_thread(executor.execute, "SELECT id FROM orders") for _ in range(12)),
            return_exceptions=True,
        )

        assert all(isinstance(o, ExecutionError) for o in outcomes)
        assert source.borrowed == 12
        assert source.returned == 12
        assert source.live == 0
