"""``POST /v1/query`` -- the first route that does work on a caller's behalf.

Three things here are not obvious from the route body, and each is a control
rather than a convenience.

**Every blocking call runs on a worker thread.** Retrieval and execution are
synchronous ``psycopg``, and this is an ``async`` route. Running them inline
does not make one request slow -- it stalls *the event loop*, so every other
in-flight request, every readiness probe and every health check stops until the
query returns. CODE_STYLE section 6 has forbidden this since Stage 0, and
``/ready`` violated it anyway; the rule that catches it is to judge a blocking
call by its behaviour in the failure it exists for, not by its cost in the
healthy case. A two-second aggregate is the *ordinary* case here.

**Concurrency is capped, and over-limit requests are refused rather than
queued.** A queue turns an overload into latency every caller pays; a refusal
is a fact the caller can act on. The cap also bounds the connection pool and
the provider's rate limit at the same point, which is the only place all three
are visible together.

**A validation failure tells the caller less than it tells the operator.**
``SQLValidationError`` carries the identifier at fault and the catalog's
nearest match -- ``no such column 'custmer_id'. Nearest match: customer_id`` --
which is written for someone fixing their own query against their own
database. Returned over HTTP with no authentication, it is a schema
enumeration oracle: submit a question, read which names come back. So the
message a caller receives names *what kind* of thing was wrong and the log
keeps the detail, correlated by ``request_id``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request

from answering import Candidate, QuestionAnswerer
from api.errors import RATE_LIMITED, SQL_VALIDATION_FAILED, ApiError
from api.schemas import QueryRequest, QueryResponse, QueryStep
from core.exceptions import SQLValidationError
from execution.executor import QueryResult, SQLExecutor

logger = logging.getLogger(__name__)

VALIDATION_FAILED_MESSAGE = "the generated query did not pass validation"
"""What a validation failure says to a caller.

Fixed text, and it does not name the identifier. See the module docstring: the
detailed form is a schema oracle when the endpoint is unauthenticated, and it
is only *useful* to somebody who already knows the schema.
"""


@dataclass(frozen=True, slots=True)
class Answer:
    """What answering produced, before it is shaped into a response body."""

    candidate: Candidate
    result: QueryResult | None
    steps: tuple[QueryStep, ...]


class QueryService:
    """Answers one question, under this request's bounds.

    Constructed once per process and shared. It holds no per-request state --
    everything a request needs is an argument -- which is what makes sharing it
    across concurrent callers safe. The semaphore is the one piece of shared
    mutable state and is the point.

    Args:
        answerer: The retrieve-then-generate path shared with the eval harness.
        executor: Runs the generated SQL under limits and audits it.
        max_concurrent: In-flight questions allowed at once.
    """

    def __init__(
        self,
        answerer: QuestionAnswerer,
        executor: SQLExecutor,
        *,
        max_concurrent: int,
    ) -> None:
        self._answerer = answerer
        self._executor = executor
        self._slots = asyncio.Semaphore(max_concurrent)
        self._limit = max_concurrent

    async def answer(self, request: QueryRequest, *, request_id: str) -> Answer:
        """Retrieve, generate, and unless ``explain_only`` execute.

        Raises:
            ApiError: the concurrency cap was already reached.
            TextToSQLError: any component failure; the envelope maps it.
        """
        if self._slots.locked():
            # Checked rather than awaited. `async with self._slots` would queue
            # the request behind the others and answer it late, which reads as
            # a slow service rather than a busy one -- and a caller cannot back
            # off from a response they have not received yet.
            raise ApiError(
                RATE_LIMITED,
                f"too many questions in flight; the limit is {self._limit}",
            )

        async with self._slots:
            steps: list[QueryStep] = []
            candidate = await _timed(steps, "answer", self._answerer.candidate(request.question))

            if request.options.explain_only:
                # Validation still runs -- it is what `explain_only` is for --
                # but inside the executor, which is the only component that
                # re-validates rather than trusting a caller. Reaching it
                # without executing means asking it to plan and stop.
                await _timed_call(
                    steps,
                    "validate",
                    self._executor.explain,
                    candidate.sql,
                    timeout_ms=request.options.timeout_ms,
                )
                return Answer(candidate=candidate, result=None, steps=tuple(steps))

            result = await _timed_call(
                steps,
                "execute",
                self._executor.execute,
                candidate.sql,
                max_rows=request.options.max_rows,
                timeout_ms=request.options.timeout_ms,
                question=request.question,
                request_id=request_id,
            )
            return Answer(candidate=candidate, result=result, steps=tuple(steps))


async def _timed(steps: list[QueryStep], stage: str, awaitable: Any) -> Any:
    started = time.perf_counter()
    try:
        value = await awaitable
    except Exception:
        steps.append(QueryStep(stage=stage, duration_ms=_since(started), status="error"))
        raise
    steps.append(QueryStep(stage=stage, duration_ms=_since(started), status="ok"))
    return value


async def _timed_call(steps: list[QueryStep], stage: str, fn: Any, *args: Any, **kw: Any) -> Any:
    """Run a **blocking** callable on a worker thread, timed.

    ``asyncio.to_thread`` rather than calling it: see the module docstring.
    Everything routed through here is synchronous ``psycopg``.
    """
    return await _timed(steps, stage, asyncio.to_thread(lambda: fn(*args, **kw)))


def _since(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def build_router(service_of: Any) -> APIRouter:
    """The route table.

    Args:
        service_of: Called with the request to get the :class:`QueryService`.
            A callable rather than the service itself because the service is
            built in the lifespan, after the router is registered -- the same
            reason :class:`api.health.Readiness` is configured rather than
            constructed late. A router added after startup works only because
            Starlette happens to consult a mutable list.
    """
    router = APIRouter(prefix="/v1", tags=["query"])

    @router.post("/query", response_model=QueryResponse)
    async def query(body: QueryRequest, request: Request) -> QueryResponse:
        service: QueryService = service_of(request)
        request_id: str = getattr(request.state, "request_id", "")
        try:
            answer = await service.answer(body, request_id=request_id)
        except SQLValidationError as exc:
            # Caught here rather than in the envelope's generic handler, which
            # would publish `str(exc)` -- and this project's own contract says
            # a domain exception's message is publishable. It is, to the
            # audience it was written for. This one was written for an operator
            # holding the schema, and the endpoint has no authentication.
            logger.warning("validation rejected generated SQL [request_id=%s]: %s", request_id, exc)
            raise ApiError(SQL_VALIDATION_FAILED, VALIDATION_FAILED_MESSAGE) from exc

        return _body(answer)

    return router


def _body(answer: Answer) -> QueryResponse:
    result = answer.result
    usage = answer.candidate.usage
    return QueryResponse(
        sql=answer.candidate.sql,
        columns=() if result is None else result.columns,
        rows=() if result is None else result.rows,
        row_count=0 if result is None else result.row_count,
        truncated=False if result is None else result.truncated,
        executed=result is not None,
        steps=answer.steps,
        usage={
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        },
    )


__all__ = ["VALIDATION_FAILED_MESSAGE", "Answer", "QueryService", "build_router"]
