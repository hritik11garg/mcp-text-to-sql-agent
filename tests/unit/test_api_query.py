"""``POST /v1/query`` -- the request contract and the concurrency cap.

No database and no model. The route is driven through ``TestClient`` with a
fake answerer and a fake executor, because what is under test here is the
*contract*: which requests are accepted, what a refusal looks like, and what
the response says about a query that did not run.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from answering import Candidate
from api import errors, query
from api.query import VALIDATION_FAILED_MESSAGE, QueryService
from api.schemas import QueryRequest
from core.exceptions import SQLValidationError
from core.ports.llm import Usage
from execution.executor import QueryResult

pytestmark = pytest.mark.unit


def candidate(sql: str = "SELECT id FROM orders") -> Candidate:
    return Candidate(
        sql=sql,
        retrieved=(("orders", "id"),),
        usage=Usage(input_tokens=11, output_tokens=7),
        model="fake",
        context=object(),  # type: ignore[arg-type]
    )


def result(rows: tuple[tuple[Any, ...], ...] = ((1,), (2,))) -> QueryResult:
    return QueryResult(
        columns=("id",),
        rows=rows,
        row_count=len(rows),
        truncated=False,
        duration_ms=1.0,
        executed_sql="SELECT id FROM orders LIMIT 500",
        row_limit=500,
    )


class FakeAnswerer:
    def __init__(self, sql: str = "SELECT id FROM orders") -> None:
        self.sql = sql
        self.questions: list[str] = []

    async def candidate(self, question: str, **_: Any) -> Candidate:
        self.questions.append(question)
        return candidate(self.sql)


class FakeExecutor:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.executed: list[str] = []
        self.explained: list[str] = []
        self.thread_names: list[str] = []

    def execute(self, sql: str, **_: Any) -> QueryResult:
        self.thread_names.append(threading.current_thread().name)
        if self.raises is not None:
            raise self.raises
        self.executed.append(sql)
        return result()

    def explain(self, sql: str, **_: Any) -> None:
        self.thread_names.append(threading.current_thread().name)
        if self.raises is not None:
            raise self.raises
        self.explained.append(sql)


def build_client(
    answerer: Any = None, executor: Any = None, *, max_concurrent: int = 4
) -> TestClient:
    service = QueryService(
        answerer or FakeAnswerer(),
        executor or FakeExecutor(),
        max_concurrent=max_concurrent,
    )
    app = FastAPI()
    errors.install(app)
    app.include_router(query.build_router(lambda _request: service))
    return TestClient(app, raise_server_exceptions=False)


class TestTheRequestAcceptsOnlyWhatItHonours:
    """Fields that parse and do nothing are the config-file defect at the API.

    A caller who sets one gets no error and concludes it took effect. For
    ``session_id`` that belief is worse than a failure: the answer comes back
    *plausible* rather than obviously missing the previous turn's context.
    """

    @pytest.mark.parametrize("field", ["session_id"])
    def test_an_unimplemented_field_is_refused_by_name(self, field: str) -> None:
        response = build_client().post("/v1/query", json={"question": "how many?", field: "x"})

        assert response.status_code == 400
        assert field in response.text, "a refusal that does not name the field is a mystery"

    def test_stream_is_accepted_now_that_it_does_something(self) -> None:
        """The rule running forwards.

        ``stream`` was refused by name for exactly as long as there was no
        stream behind it. A field appears when its behaviour does, which is
        what keeps the request shape and the served behaviour from disagreeing.
        """
        response = build_client().post("/v1/query", json={"question": "how many?", "stream": False})

        assert response.status_code == 200

    def test_an_unknown_option_is_refused(self) -> None:
        response = build_client().post(
            "/v1/query", json={"question": "how many?", "options": {"nonsense": 1}}
        )

        assert response.status_code == 400

    def test_an_empty_question_is_refused(self) -> None:
        assert build_client().post("/v1/query", json={"question": ""}).status_code == 400

    def test_an_overlong_question_is_refused(self) -> None:
        response = build_client().post("/v1/query", json={"question": "x" * 2001})

        assert response.status_code == 400

    def test_the_longest_allowed_question_is_accepted(self) -> None:
        """The boundary in the accepting direction. A cap tested only from
        outside is a cap that could be off by one and look correct."""
        response = build_client().post("/v1/query", json={"question": "x" * 2000})

        assert response.status_code == 200

    def test_a_validation_failure_answers_in_the_envelope(self) -> None:
        response = build_client().post("/v1/query", json={"question": ""})

        assert response.json()["error"]["code"] == "invalid_request"


class TestTheAnswer:
    def test_a_question_is_answered(self) -> None:
        answerer = FakeAnswerer()
        response = build_client(answerer).post("/v1/query", json={"question": "how many orders?"})

        assert response.status_code == 200
        assert answerer.questions == ["how many orders?"]
        body = response.json()
        assert body["sql"] == "SELECT id FROM orders"
        assert body["rows"] == [[1], [2]]
        assert body["executed"] is True

    def test_usage_is_reported(self) -> None:
        body = build_client().post("/v1/query", json={"question": "?"}).json()

        assert body["usage"] == {"input_tokens": 11, "output_tokens": 7}

    def test_the_steps_name_what_ran(self) -> None:
        body = build_client().post("/v1/query", json={"question": "?"}).json()

        assert [s["stage"] for s in body["steps"]] == ["answer", "execute"]
        assert all(s["status"] == "ok" for s in body["steps"])

    def test_a_component_failure_becomes_its_published_status(self) -> None:
        """400 is a malformed *request*; 422 is a request this system could not
        answer. Collapsing them would tell a caller to fix their JSON when the
        JSON was fine."""
        executor = FakeExecutor(raises=SQLValidationError("unknown_identifier", "no", "custmer"))
        response = build_client(executor=executor).post("/v1/query", json={"question": "?"})

        assert response.status_code == 422


class TestExplainOnlyDoesNotRun:
    def test_it_validates_instead_of_executing(self) -> None:
        executor = FakeExecutor()
        response = build_client(executor=executor).post(
            "/v1/query", json={"question": "?", "options": {"explain_only": True}}
        )

        assert response.status_code == 200
        assert executor.explained == ["SELECT id FROM orders"]
        assert executor.executed == []

    def test_the_response_says_it_did_not_run(self) -> None:
        """`executed: false` rather than letting a caller infer it from empty
        rows. A query that returns nothing and a query that never ran are
        different facts and identical in shape."""
        body = (
            build_client()
            .post("/v1/query", json={"question": "?", "options": {"explain_only": True}})
            .json()
        )

        assert body["executed"] is False
        assert body["rows"] == []
        assert body["row_count"] == 0


class TestAValidationFailureIsNotASchemaOracle:
    """The message names a *kind* of failure, never an identifier.

    `SQLValidationError` carries the offending name and the catalog's nearest
    match -- written for an operator holding the schema. Returned over an
    unauthenticated endpoint it is an enumeration primitive: submit questions,
    read which column names come back.
    """

    def test_the_identifier_is_not_published(self) -> None:
        executor = FakeExecutor(
            raises=SQLValidationError("unknown_identifier", "no such column", "salary_usd")
        )
        response = build_client(executor=executor).post("/v1/query", json={"question": "?"})

        assert response.status_code == 422
        assert "salary_usd" not in response.text

    def test_the_suggestion_is_not_published(self) -> None:
        executor = FakeExecutor(
            raises=SQLValidationError(
                "unknown_identifier", "no such column 'x'. Nearest match: customer_id", "x"
            )
        )
        response = build_client(executor=executor).post("/v1/query", json={"question": "?"})

        assert "customer_id" not in response.text

    def test_the_caller_still_learns_the_kind_of_failure(self) -> None:
        """Withholding the detail must not withhold the category -- a caller
        needs to know a retry will not help."""
        executor = FakeExecutor(raises=SQLValidationError("unknown_identifier", "no", "x"))
        body = build_client(executor=executor).post("/v1/query", json={"question": "?"}).json()

        assert body["error"]["code"] == "sql_validation_failed"
        assert body["error"]["message"] == VALIDATION_FAILED_MESSAGE


class TestBlockingWorkLeavesTheEventLoop:
    def test_execution_runs_on_a_worker_thread(self) -> None:
        """Synchronous psycopg inside an async route stalls every other
        request in the process, including the readiness probe. CODE_STYLE
        section 6 forbids it; `/ready` did it anyway."""
        executor = FakeExecutor()
        build_client(executor=executor).post("/v1/query", json={"question": "?"})

        assert executor.thread_names, "the executor was never called"
        assert not any(name.startswith("MainThread") for name in executor.thread_names), (
            f"execution ran on {executor.thread_names}"
        )

    def test_explain_runs_on_a_worker_thread(self) -> None:
        executor = FakeExecutor()
        build_client(executor=executor).post(
            "/v1/query", json={"question": "?", "options": {"explain_only": True}}
        )

        assert not any(name.startswith("MainThread") for name in executor.thread_names)


class TestTheConcurrencyCap:
    def test_an_over_limit_request_is_refused_immediately(self) -> None:
        """Refused, not queued. A queue turns an overload into latency every
        caller waits out; a 429 is a fact a client can back off from."""

        async def exercise() -> tuple[Any, Any]:
            started = asyncio.Event()
            release = asyncio.Event()

            class Blocking:
                async def candidate(self, question: str, **_: Any) -> Candidate:
                    started.set()
                    await release.wait()
                    return candidate()

            service = QueryService(Blocking(), FakeExecutor(), max_concurrent=1)
            request = QueryRequest(question="?")

            first = asyncio.create_task(service.answer(request, request_id="a"))
            await started.wait()
            second = await asyncio.gather(
                service.answer(request, request_id="b"), return_exceptions=True
            )
            release.set()
            await first
            return second[0], None

        refused, _ = asyncio.run(exercise())

        assert isinstance(refused, errors.ApiError)
        assert refused.error is errors.RATE_LIMITED

    def test_a_slot_is_released_when_a_request_fails(self) -> None:
        """A semaphore leaked on the error path is a service that refuses
        everything after N failures and recovers only on restart."""
        executor = FakeExecutor(raises=SQLValidationError("x", "y", "z"))
        client = build_client(executor=executor, max_concurrent=1)

        for _ in range(3):
            assert client.post("/v1/query", json={"question": "?"}).status_code == 422

        healthy = build_client(max_concurrent=1)
        assert healthy.post("/v1/query", json={"question": "?"}).status_code == 200

    def test_sequential_requests_are_not_limited(self) -> None:
        """The cap is on *concurrency*, not a rate. One caller asking twice in
        a row must not be refused."""
        client = build_client(max_concurrent=1)

        for _ in range(5):
            assert client.post("/v1/query", json={"question": "?"}).status_code == 200
