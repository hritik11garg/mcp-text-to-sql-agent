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

**Both response shapes share one admission decision and one error rendering.**
Streaming does not get its own cap and does not get its own view of which
messages are publishable -- see :class:`_Admission` and
:func:`api.errors.published`. A second copy of either would be a second policy,
and the one that drifts is always the stricter one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from answering import Candidate, QuestionAnswerer
from api.errors import (
    GENERIC_FAILURE,
    INTERNAL_ERROR,
    RATE_LIMITED,
    REQUEST_ID_HEADER,
    SQL_VALIDATION_FAILED,
    ApiError,
    published,
)
from api.schemas import QueryRequest, QueryResponse, QueryStep
from api.sse import HEARTBEAT, ServerSentEvent
from core.exceptions import SQLValidationError, TextToSQLError
from execution.executor import QueryResult, SQLExecutor

logger = logging.getLogger(__name__)

STREAM_MEDIA_TYPE = "text/event-stream"

STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # nginx and several other reverse proxies buffer a response by default,
    # which for an event stream means holding every event until the response
    # ends -- turning a stream into a slow non-streaming reply and defeating
    # the entire point. Harmless where no proxy is present.
    "X-Accel-Buffering": "no",
}

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


class _Admission:
    """The in-flight cap, for both response shapes.

    A plain counter rather than :class:`asyncio.Semaphore`, and the reason is
    the streaming path. A slot has to be taken **before** the response begins:
    once a stream has sent its ``200`` and its headers, a ``429`` is no longer
    expressible, and the only remaining way to refuse is an ``error`` event on
    a response that already claimed success.

    ``Semaphore`` cannot do that. Its acquire is a coroutine, so taking a slot
    means awaiting, and awaiting means the route has already returned. Checking
    ``locked()`` first and acquiring later leaves a gap with an ``await`` in it,
    which is exactly the window two concurrent requests both pass through.

    :meth:`acquire` is synchronous and therefore atomic against the event loop:
    there is no suspension point between the test and the increment, so no
    second request can observe the state in between. That property is the whole
    design, and it is a property of *asyncio being single-threaded* rather than
    of this class -- which is why this is not thread-safe and does not pretend
    to be. Nothing here is called from a worker thread.
    """

    __slots__ = ("_held", "_limit")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._held = 0

    def acquire(self) -> None:
        """Take a slot, or refuse.

        Raises:
            ApiError: the cap is reached. Refused rather than queued -- a queue
                turns an overload into latency every caller pays, and a caller
                that has not received a response cannot back off from it.
        """
        if self._held >= self._limit:
            raise ApiError(
                RATE_LIMITED,
                f"too many questions in flight; the limit is {self._limit}",
            )
        self._held += 1

    def release(self) -> None:
        self._held -= 1

    @property
    def held(self) -> int:
        return self._held


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
        keepalive_seconds: float = 15.0,
    ) -> None:
        self._answerer = answerer
        self._executor = executor
        self._admission = _Admission(max_concurrent)
        self._keepalive = keepalive_seconds

    async def answer(self, request: QueryRequest, *, request_id: str) -> Answer:
        """Retrieve, generate, and unless ``explain_only`` execute.

        Raises:
            ApiError: the concurrency cap was already reached.
            TextToSQLError: any component failure; the envelope maps it.
        """
        self._admission.acquire()
        try:
            return await self._answer(request, request_id=request_id)
        finally:
            self._admission.release()

    def stream(
        self,
        request: QueryRequest,
        *,
        request_id: str,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[str]:
        """The same work, reported as it happens.

        **Deliberately not ``async``.** A slot must be taken while a ``429`` is
        still expressible, and an ``async def`` here would not run its own body
        until the route had already returned a ``200`` -- an ``async``
        generator's body does not start until the first ``__anext__``. So this
        is a plain function that admits the request *now* and returns the
        generator that does the work, which is what puts the refusal in the
        status line where a client can act on it.

        Args:
            is_disconnected: Polled between events. A client that has gone away
                should stop costing an LLM call and, more importantly, stop
                holding a slot that a present caller could use.

        Raises:
            ApiError: the concurrency cap was already reached. Raised
                synchronously, before any part of the response exists.
        """
        self._admission.acquire()
        # Nothing between the acquire and the generator's `try` may raise: the
        # release lives in that generator's `finally`. Constructing an async
        # generator cannot fail, which is what makes this safe rather than
        # merely short.
        return self._stream(request, request_id=request_id, is_disconnected=is_disconnected)

    async def _answer(
        self,
        request: QueryRequest,
        *,
        request_id: str,
        on_event: Callable[[ServerSentEvent], None] | None = None,
        steps: list[QueryStep] | None = None,
    ) -> Answer:
        """The work, with an optional observer. One body, two response shapes.

        The streaming and non-streaming paths differ in how they *report*, not
        in what they do. Splitting them into two implementations would put the
        row limit, the timeout and the ``explain_only`` branch in two places,
        and the second copy is the one that misses the next change.
        """
        steps = [] if steps is None else steps

        def emit(event: str, **data: Any) -> None:
            if on_event is not None:
                on_event(ServerSentEvent(event, data))

        candidate = await _timed(
            steps,
            "answer",
            self._answerer.candidate(
                request.question,
                # Reports the composition; does not perform it. See
                # `QuestionAnswerer.candidate` -- a caller that sequences the
                # phases itself is a caller that can sequence them wrongly.
                on_stage=(lambda stage: emit("stage", stage=stage, status="ok"))
                if on_event is not None
                else None,
            ),
        )
        emit("sql", sql=candidate.sql, attempt=1)

        if request.options.explain_only:
            # Validation still runs -- it is what `explain_only` is for -- but
            # inside the executor, which is the only component that re-validates
            # rather than trusting a caller. Reaching it without executing means
            # asking it to plan and stop.
            await _timed_call(
                steps,
                "validate",
                self._executor.explain,
                candidate.sql,
                timeout_ms=request.options.timeout_ms,
            )
            emit("stage", stage="validate", status="ok")
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
        # Emitted before the rows, and the ordering is the point: a client
        # should learn that a phase finished from a `stage` event, not by
        # noticing that a data event turned up. Without this the `explain_only`
        # branch above announces `validate` while the ordinary path announces
        # nothing for the phase that actually touches the database -- so every
        # client has to infer execution from `rows`, and an inference is what
        # this event exists to make unnecessary.
        emit("stage", stage="execute", status="ok")
        emit(
            "rows",
            columns=list(result.columns),
            rows=[list(row) for row in result.rows],
            truncated=result.truncated,
        )
        return Answer(candidate=candidate, result=result, steps=tuple(steps))

    async def _stream(
        self,
        request: QueryRequest,
        *,
        request_id: str,
        is_disconnected: Callable[[], Awaitable[bool]] | None,
    ) -> AsyncIterator[str]:
        """Drain a queue the work fills, emitting heartbeats while it is empty.

        The work runs as a task rather than inline because this generator has a
        second job -- noticing that nothing has happened for a while. A single
        coroutine that awaited the work directly could not also emit a
        heartbeat, and 29 seconds of silence (PERFORMANCE.md) is long enough
        for an intermediary to close the connection.
        """
        queue: asyncio.Queue[ServerSentEvent | None] = asyncio.Queue()
        worker = asyncio.create_task(self._produce(request, request_id=request_id, queue=queue))
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=self._keepalive)
                except TimeoutError:
                    if is_disconnected is not None and await is_disconnected():
                        logger.info("client disconnected mid-stream [request_id=%s]", request_id)
                        return
                    yield HEARTBEAT
                    continue

                if event is None:
                    return
                yield event.encode()
        finally:
            # Runs on the ordinary end, on an exception, and on the client
            # hanging up -- Starlette closes the generator, which raises
            # `GeneratorExit` at the `yield`. The slot must come back in all
            # three, and a slot that is only returned on success is a slot a
            # client can consume permanently by disconnecting.
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            self._admission.release()

    async def _produce(
        self,
        request: QueryRequest,
        *,
        request_id: str,
        queue: asyncio.Queue[ServerSentEvent | None],
    ) -> None:
        """Do the work, push events, and always terminate the stream.

        Every exit path puts ``None`` on the queue. A stream that stops without
        a terminal event leaves a client waiting on a socket that will never
        produce anything, which is the failure mode a timeout eventually
        resolves and nothing else does.
        """
        steps: list[QueryStep] = []
        try:
            answer = await self._answer(
                request,
                request_id=request_id,
                on_event=queue.put_nowait,
                steps=steps,
            )
        except asyncio.CancelledError:
            # The client went away. Nothing to report to them, and no terminal
            # event either -- there is no longer anyone on the other end. Not
            # swallowed: re-raised so the task ends cancelled as it was asked to.
            raise
        except SQLValidationError as exc:
            logger.warning("validation rejected generated SQL [request_id=%s]: %s", request_id, exc)
            queue.put_nowait(
                _error_event(SQL_VALIDATION_FAILED.code, VALIDATION_FAILED_MESSAGE, request_id)
            )
        except ApiError as exc:
            queue.put_nowait(_error_event(exc.error.code, str(exc), request_id))
        except TextToSQLError as exc:
            # `published` and not a local judgement: the same rule the HTTP
            # handler applies, so the two cannot disagree about which of this
            # project's messages a caller may read.
            error, message = published(exc, context="POST /v1/query (stream)")
            queue.put_nowait(_error_event(error.code, message, request_id))
        except Exception:
            logger.exception("unhandled error streaming a question [request_id=%s]", request_id)
            queue.put_nowait(_error_event(INTERNAL_ERROR.code, GENERIC_FAILURE, request_id))
        else:
            usage = answer.candidate.usage
            queue.put_nowait(
                ServerSentEvent(
                    "done",
                    {
                        "row_count": 0 if answer.result is None else answer.result.row_count,
                        "executed": answer.result is not None,
                        "steps": [step.model_dump() for step in answer.steps],
                        "usage": {
                            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                        },
                    },
                )
            )
        finally:
            queue.put_nowait(None)


def _error_event(code: str, message: str, request_id: str) -> ServerSentEvent:
    """The terminal failure event, in the envelope shape the HTTP errors use.

    Same keys as :func:`api.errors.envelope` produces, because a client should
    not have to parse two error shapes depending on which response it asked
    for.
    """
    return ServerSentEvent(
        "error", {"error": {"code": code, "message": message, "request_id": request_id}}
    )


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

    @router.post("/query", response_model=None)
    async def query(body: QueryRequest, request: Request) -> QueryResponse | StreamingResponse:
        service: QueryService = service_of(request)
        request_id: str = getattr(request.state, "request_id", "")

        if body.stream:
            # `service.stream` admits the request synchronously, so a `429`
            # raised here is still an ordinary error response. Once
            # `StreamingResponse` is returned, the status is spent.
            return StreamingResponse(
                service.stream(
                    body,
                    request_id=request_id,
                    is_disconnected=request.is_disconnected,
                ),
                media_type=STREAM_MEDIA_TYPE,
                headers={**STREAM_HEADERS, REQUEST_ID_HEADER: request_id},
            )

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
