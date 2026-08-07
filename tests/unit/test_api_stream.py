"""``POST /v1/query`` with ``stream: true`` -- what the stream guarantees.

Four properties, and each of them is a way the streaming path can fail that the
non-streaming path cannot.

**A slot is taken before the response begins.** Once a stream has sent ``200``
and its headers, a ``429`` is no longer expressible. Admission that happens
inside the generator is admission that happens too late.

**A slot comes back however the stream ends** -- including when the client
hangs up. A slot released only on success is a slot a client can consume
permanently by disconnecting, which is a denial of service that needs no
traffic to speak of.

**Exactly one terminal event.** A stream that stops without ``done`` or
``error`` leaves the client on a socket that will never say anything again.

**A failure says no more than the non-streaming path says.** The stream cannot
use the exception handlers -- its response has already started -- so it renders
errors itself, and that is exactly where the sanitisation could quietly differ.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from answering import Candidate
from api import errors, query
from api.query import VALIDATION_FAILED_MESSAGE, QueryService
from api.schemas import QueryRequest
from core.exceptions import LLMUnavailableError, SQLValidationError
from core.ports.llm import Usage
from execution.executor import QueryResult

pytestmark = pytest.mark.unit


def candidate(sql: str = "SELECT id\nFROM orders") -> Candidate:
    """Multi-line by default -- that is the realistic shape of generated SQL."""
    return Candidate(
        sql=sql,
        retrieved=(("orders", "id"),),
        usage=Usage(input_tokens=11, output_tokens=7),
        model="fake",
        context=object(),  # type: ignore[arg-type]
    )


class FakeAnswerer:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises

    async def candidate(self, question: str, *, on_stage: Any = None, **_: Any) -> Candidate:
        if self.raises is not None:
            raise self.raises
        if on_stage is not None:
            on_stage("retrieve")
            on_stage("generate")
        return candidate()


class FakeExecutor:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises

    def execute(self, sql: str, **_: Any) -> QueryResult:
        if self.raises is not None:
            raise self.raises
        return QueryResult(
            columns=("id",),
            rows=((1,), (2,)),
            row_count=2,
            truncated=False,
            duration_ms=1.0,
            executed_sql=sql,
            row_limit=500,
        )

    def explain(self, sql: str, **_: Any) -> None:
        if self.raises is not None:
            raise self.raises


def build_client(
    answerer: Any = None, executor: Any = None, *, max_concurrent: int = 4
) -> TestClient:
    service = QueryService(
        answerer or FakeAnswerer(),
        executor or FakeExecutor(),
        max_concurrent=max_concurrent,
        keepalive_seconds=0.05,
    )
    app = FastAPI()
    errors.install(app)
    app.include_router(query.build_router(lambda _request: service))
    return TestClient(app, raise_server_exceptions=False)


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse a wire body into (name, payload) pairs, ignoring comments."""
    parsed = []
    for frame in body.split("\n\n"):
        if not frame.strip() or frame.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in frame.strip().split("\n"))
        parsed.append((lines["event"], json.loads(lines["data"])))
    return parsed


def stream(client: TestClient, **body: Any) -> str:
    response = client.post("/v1/query", json={"question": "how many?", "stream": True, **body})
    assert response.status_code == 200
    return response.text


class TestTheStreamReportsTheWork:
    def test_the_content_type_is_an_event_stream(self) -> None:
        response = build_client().post("/v1/query", json={"question": "?", "stream": True})

        assert response.headers["content-type"].startswith("text/event-stream")

    def test_proxy_buffering_is_disabled(self) -> None:
        """A buffering proxy holds every event until the end, which turns a
        stream back into a slow non-streaming reply."""
        response = build_client().post("/v1/query", json={"question": "?", "stream": True})

        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["cache-control"] == "no-cache"

    def test_the_stages_are_reported_as_they_complete(self) -> None:
        names = [name for name, _ in events(stream(build_client()))]

        assert names[: names.index("sql")] == ["stage", "stage"]
        assert "sql" in names
        assert "rows" in names

    def test_the_sql_arrives_before_the_rows(self) -> None:
        """The ordering is the feature. Seeing the SQL while it still runs is
        what a 29-second answer needs in order not to look like a hang."""
        names = [name for name, _ in events(stream(build_client()))]

        assert names.index("sql") < names.index("rows") < names.index("done")

    def test_the_rows_arrive(self) -> None:
        payload = dict(events(stream(build_client())))["rows"]

        assert payload["columns"] == ["id"]
        assert payload["rows"] == [[1], [2]]
        assert payload["truncated"] is False

    def test_done_carries_the_usage_and_the_timings(self) -> None:
        payload = dict(events(stream(build_client())))["done"]

        assert payload["row_count"] == 2
        assert payload["executed"] is True
        assert payload["usage"] == {"input_tokens": 11, "output_tokens": 7}
        assert [step["stage"] for step in payload["steps"]] == ["answer", "execute"]

    def test_explain_only_streams_without_executing(self) -> None:
        body = stream(build_client(), options={"explain_only": True})
        payload = dict(events(body))

        assert "rows" not in payload
        assert payload["done"]["executed"] is False

    def test_multiline_sql_survives_the_round_trip(self) -> None:
        """The framing test in test_api_sse.py, proved end to end."""
        payload = dict(events(stream(build_client())))["sql"]

        assert payload["sql"] == "SELECT id\nFROM orders"


class TestExactlyOneTerminalEvent:
    """A stream that ends without one leaves a client waiting forever."""

    def test_a_success_ends_with_done(self) -> None:
        names = [name for name, _ in events(stream(build_client()))]

        assert names[-1] == "done"
        assert names.count("done") == 1
        assert "error" not in names

    def test_a_failure_ends_with_error(self) -> None:
        client = build_client(answerer=FakeAnswerer(raises=LLMUnavailableError("upstream is down")))
        names = [name for name, _ in events(stream(client))]

        assert names[-1] == "error"
        assert names.count("error") == 1
        assert "done" not in names

    def test_a_failure_after_the_sql_still_terminates(self) -> None:
        """The hard case: events already sent, then the work fails."""
        client = build_client(executor=FakeExecutor(raises=LLMUnavailableError("gone")))
        names = [name for name, _ in events(stream(client))]

        assert "sql" in names
        assert names[-1] == "error"


class TestAStreamedFailureIsNotASchemaOracle:
    """The stream renders its own errors, so this is where the two paths could
    quietly disagree about what a caller may read."""

    def test_the_identifier_is_not_published(self) -> None:
        client = build_client(
            executor=FakeExecutor(raises=SQLValidationError("custmer_id", "column", "customer_id"))
        )
        body = stream(client)

        assert "custmer_id" not in body
        # The nearest match completes a guess rather than merely confirming it,
        # which is what makes it the more expensive half of the leak.
        assert "customer_id" not in body

    def test_the_caller_still_learns_the_kind_of_failure(self) -> None:
        client = build_client(
            executor=FakeExecutor(raises=SQLValidationError("custmer_id", "column", "customer_id"))
        )
        payload = dict(events(stream(client)))["error"]["error"]

        assert payload["code"] == "sql_validation_failed"
        assert payload["message"] == VALIDATION_FAILED_MESSAGE

    def test_an_unpublishable_message_stays_unpublished(self) -> None:
        """Anything mapping to internal_error is ours, not the caller's.

        Shared with the non-streaming path through `errors.published` -- two
        renderers, one rule, so neither can drift into being the lenient copy.
        """
        from core.exceptions import ConfigurationError

        client = build_client(answerer=FakeAnswerer(raises=ConfigurationError("DSN=secret")))
        body = stream(client)

        assert "secret" not in body
        assert dict(events(body))["error"]["error"]["code"] == "internal_error"

    def test_the_error_event_carries_the_request_id(self) -> None:
        client = build_client(answerer=FakeAnswerer(raises=LLMUnavailableError("down")))
        payload = dict(events(stream(client)))["error"]["error"]

        assert "request_id" in payload


class TestAdmissionHappensBeforeTheResponse:
    """A 429 is only expressible before the first byte of a 200."""

    def test_an_over_limit_stream_is_refused_with_a_status(self) -> None:
        async def exercise() -> Any:
            started = asyncio.Event()
            release = asyncio.Event()

            class Blocking:
                async def candidate(self, question: str, **_: Any) -> Candidate:
                    started.set()
                    await release.wait()
                    return candidate()

            service = QueryService(
                Blocking(), FakeExecutor(), max_concurrent=1, keepalive_seconds=0.05
            )
            request = QueryRequest(question="?", stream=True)

            first = asyncio.create_task(_drain(service.stream(request, request_id="a")))
            await started.wait()
            try:
                service.stream(request, request_id="b")
            except errors.ApiError as exc:
                refused = exc
            else:
                refused = None  # type: ignore[assignment]
            release.set()
            await first
            return refused

        refused = asyncio.run(exercise())

        assert refused is not None, "the second stream was admitted"
        assert refused.error is errors.RATE_LIMITED

    def test_the_refusal_is_raised_synchronously(self) -> None:
        """Not from inside the generator. `stream()` is deliberately not
        `async def`, because an async generator's body does not run until the
        first `__anext__` -- by which time the route has returned a 200."""
        service = QueryService(
            FakeAnswerer(), FakeExecutor(), max_concurrent=1, keepalive_seconds=0.05
        )
        request = QueryRequest(question="?", stream=True)

        service.stream(request, request_id="a")  # holds the only slot, never iterated

        with pytest.raises(errors.ApiError):
            service.stream(request, request_id="b")

    def test_one_cap_covers_both_response_shapes(self) -> None:
        """Streaming does not get its own allowance. A second counter would be
        a second policy, and the limit would be whichever one was checked."""

        async def exercise() -> Any:
            service = QueryService(
                FakeAnswerer(), FakeExecutor(), max_concurrent=1, keepalive_seconds=0.05
            )
            request = QueryRequest(question="?")

            service.stream(request, request_id="a")  # takes the slot
            try:
                await service.answer(request, request_id="b")
            except errors.ApiError as exc:
                return exc
            return None

        refused = asyncio.run(exercise())

        assert refused is not None, "a stream and a plain request shared no cap"
        assert refused.error is errors.RATE_LIMITED


class TestTheSlotComesBack:
    def test_a_completed_stream_releases_its_slot(self) -> None:
        client = build_client(max_concurrent=1)

        for _ in range(4):
            assert dict(events(stream(client)))["done"]["row_count"] == 2

    def test_a_failed_stream_releases_its_slot(self) -> None:
        """A slot leaked on the error path is a service that refuses everything
        after N failures and recovers only on restart."""
        client = build_client(
            answerer=FakeAnswerer(raises=LLMUnavailableError("down")), max_concurrent=1
        )

        for _ in range(4):
            names = [name for name, _ in events(stream(client))]
            assert names[-1] == "error"

    def test_an_abandoned_stream_releases_its_slot(self) -> None:
        """The one that matters. A client that opens streams and hangs up must
        not be able to consume the whole cap -- that is a denial of service
        costing the attacker one connection each."""

        async def exercise() -> int:
            started = asyncio.Event()
            release = asyncio.Event()

            class Blocking:
                async def candidate(self, question: str, **_: Any) -> Candidate:
                    started.set()
                    await release.wait()
                    return candidate()

            service = QueryService(
                Blocking(), FakeExecutor(), max_concurrent=1, keepalive_seconds=0.05
            )
            request = QueryRequest(question="?", stream=True)

            generator = service.stream(request, request_id="a")
            consume = asyncio.create_task(_drain(generator))
            await started.wait()

            # The client hangs up: Starlette closes the generator.
            consume.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consume
            await generator.aclose()
            release.set()

            # If the slot came back, this succeeds. If it did not, it raises.
            service.stream(request, request_id="b")
            return 1

        assert asyncio.run(exercise()) == 1


class TestTheHeartbeatKeepsASilentStreamOpen:
    def test_a_slow_answer_emits_keepalives(self) -> None:
        """Without these, the slowest requests are the ones most likely to be
        killed by something in the middle -- the opposite of the point."""

        class Slow:
            async def candidate(self, question: str, **_: Any) -> Candidate:
                await asyncio.sleep(0.3)
                return candidate()

        client = build_client(answerer=Slow())
        body = stream(client)

        assert ": keepalive" in body
        assert [name for name, _ in events(body)][-1] == "done"

    def test_a_keepalive_is_not_parsed_as_an_event(self) -> None:
        class Slow:
            async def candidate(self, question: str, **_: Any) -> Candidate:
                await asyncio.sleep(0.3)
                return candidate()

        names = [name for name, _ in events(stream(build_client(answerer=Slow())))]

        assert names.count("done") == 1
        assert all(name in {"stage", "sql", "rows", "done"} for name in names)


async def _drain(generator: Any) -> list[str]:
    return [chunk async for chunk in generator]
