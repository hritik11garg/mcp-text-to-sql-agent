"""One error shape, and nothing in it the caller was not meant to see.

The contract is docs/architecture/API.md, "Error model". This module is what
makes it true for *every* response rather than for the ones that were written
by hand -- including the two nobody writes: a framework validation failure, and
an exception nobody predicted.

Both of those are where the leaks are, and this project has already found the
same class of bug one layer down. ``mcp_servers.common`` catches every
exception before the SDK can return ``str(exc)`` to a model, because a psycopg
error quotes the connection string back with the password in it. The reasoning
transfers exactly, and the audience is worse: there, the string went to a model
running on the operator's own machine. Here it goes to whoever sent the
request.

So the rules are:

**A domain error's message is publishable; anything else's is not.** Every
message in ``core.exceptions`` was written by this project to be read by a
caller trying to fix their query. Nothing else was -- a driver error, a
``KeyError``, a stack trace from a dependency -- and those become
:data:`GENERIC_FAILURE` with the detail logged where an operator can correlate
it by ``request_id``.

**The status code and the ``code`` string come from one mapping**, so a caller
branching on either gets the same answer. Two tables would eventually disagree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.exceptions import (
    ConfigurationError,
    LLMError,
    LLMQuotaExceededError,
    LLMUnavailableError,
    PermissionDeniedError,
    RetrievalError,
    SQLValidationError,
    StatementTimeoutError,
    TextToSQLError,
)

logger = logging.getLogger(__name__)

GENERIC_FAILURE: Final = "the server could not complete this request"
"""What an unexpected exception says to the caller.

Deliberately uninformative, and deliberately identical for every cause: a
message that varied with the failure would let a caller distinguish "no such
table" from "connection refused" by reading it, which is an oracle. The
operator gets the real exception and traceback in the log, keyed by the same
``request_id`` the caller was handed.
"""

REQUEST_ID_HEADER: Final = "X-Request-Id"


@dataclass(frozen=True, slots=True)
class ErrorCode:
    """An HTTP status and the ``code`` string that always accompanies it."""

    status: int
    code: str


# The published table from API.md section "Error model". Written out rather
# than derived, for the same reason the MCP tool schemas are: it is a contract
# a client may have branched on, so it should change when somebody means to
# change it.
INVALID_REQUEST: Final = ErrorCode(400, "invalid_request")
SESSION_NOT_FOUND: Final = ErrorCode(404, "session_not_found")
QUERY_TIMEOUT: Final = ErrorCode(408, "query_timeout")
PAYLOAD_TOO_LARGE: Final = ErrorCode(413, "payload_too_large")
SQL_VALIDATION_FAILED: Final = ErrorCode(422, "sql_validation_failed")
AMBIGUOUS_QUESTION: Final = ErrorCode(422, "ambiguous_question")
RATE_LIMITED: Final = ErrorCode(429, "rate_limited")
INTERNAL_ERROR: Final = ErrorCode(500, "internal_error")
LLM_UNAVAILABLE: Final = ErrorCode(502, "llm_unavailable")
DATABASE_UNAVAILABLE: Final = ErrorCode(503, "database_unavailable")
NOT_READY: Final = ErrorCode(503, "not_ready")

_NOT_FOUND: Final = ErrorCode(404, "not_found")
_METHOD_NOT_ALLOWED: Final = ErrorCode(405, "method_not_allowed")

_BY_STATUS: Final = {
    400: INVALID_REQUEST,
    404: _NOT_FOUND,
    405: _METHOD_NOT_ALLOWED,
    408: QUERY_TIMEOUT,
    413: PAYLOAD_TOO_LARGE,
    429: RATE_LIMITED,
    503: DATABASE_UNAVAILABLE,
}
"""Codes for statuses raised as a bare ``HTTPException``, so a route that
raises one still produces the envelope rather than Starlette's ``{"detail":
...}``."""


class ApiError(Exception):
    """A failure a route raises deliberately, with its published code attached.

    Carries an :class:`ErrorCode` rather than a status and a string, so a route
    cannot invent a ``code`` that is not in the table above -- which is how a
    documented contract acquires undocumented members.
    """

    def __init__(
        self, error: ErrorCode, message: str, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.error = error
        self.details = details


def envelope(
    error: ErrorCode,
    message: str,
    *,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build the one response shape, with the request id in body and header.

    In both places on purpose. A caller reading JSON finds it in the body; a
    proxy, a browser devtools pane, or a client that only logs headers finds it
    on the response. The whole value of the id is that a user can quote it in a
    bug report, and that only works if it is hard to miss.
    """
    body: dict[str, Any] = {"code": error.code, "message": message, "request_id": request_id}
    if details:
        body["details"] = details
    return JSONResponse(
        status_code=error.status,
        content={"error": body},
        headers={REQUEST_ID_HEADER: request_id},
    )


def code_for(exc: TextToSQLError) -> ErrorCode:
    """Map a domain exception to its published code.

    Ordered most specific first because the hierarchy is nested --
    ``LLMQuotaExceededError`` is an ``LLMUnavailableError`` is an ``LLMError``,
    and matching the base first would report a spent quota as a generic
    upstream failure. A dict keyed by type would get this wrong for the same
    reason; ``mcp_servers.common._error_type`` says so too, and both are
    deliberate.
    """
    if isinstance(exc, StatementTimeoutError):
        return QUERY_TIMEOUT
    if isinstance(exc, PermissionDeniedError):
        # The read-only role refused the statement. Reported as a validation
        # failure, not a 403: nothing about the *caller's* authorization is
        # wrong, and a 403 would invite them to go looking for credentials that
        # would make it work. The containment boundary held; that is a fact
        # about the query.
        return SQL_VALIDATION_FAILED
    if isinstance(exc, SQLValidationError):
        return SQL_VALIDATION_FAILED
    if isinstance(exc, LLMQuotaExceededError):
        return RATE_LIMITED
    if isinstance(exc, LLMUnavailableError | LLMError):
        return LLM_UNAVAILABLE
    if isinstance(exc, RetrievalError):
        return AMBIGUOUS_QUESTION
    if isinstance(exc, ConfigurationError):
        # Reached at request time rather than startup, which means something
        # the process depends on changed under it. Not the caller's fault and
        # not the caller's business.
        return INTERNAL_ERROR
    return INTERNAL_ERROR


def install(app: FastAPI) -> None:
    """Register the handlers. Order does not matter; coverage does.

    Four handlers, because there are four ways a response can be produced
    without a route author writing it: a deliberate :class:`ApiError`, a domain
    exception raised deep in a component, a framework validation failure, and
    an unhandled exception. Miss any one and that path answers in a different
    shape -- which is exactly the hole a client's error handling falls into.
    """

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return envelope(exc.error, str(exc), request_id=request_id_of(request), details=exc.details)

    @app.exception_handler(TextToSQLError)
    async def _domain_error(request: Request, exc: TextToSQLError) -> JSONResponse:
        error = code_for(exc)
        if error is INTERNAL_ERROR:
            # Mapped to internal_error means "this project wrote the message
            # but did not intend it for a caller". Log it, publish nothing.
            # `exc_info=exc` rather than `logger.exception`: a FastAPI handler
            # is called with the exception as an argument, not from inside an
            # `except` block, so there is no ambient exception for the implicit
            # form to pick up -- it would log the message and drop the
            # traceback, which is the only part an operator needs.
            logger.error("unpublishable domain error on %s", request.url.path, exc_info=exc)
            return envelope(INTERNAL_ERROR, GENERIC_FAILURE, request_id=request_id_of(request))
        logger.info("%s: %s", error.code, type(exc).__name__)
        return envelope(error, str(exc), request_id=request_id_of(request))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return envelope(
            INVALID_REQUEST,
            "the request body or parameters did not match the expected shape",
            request_id=request_id_of(request),
            details={"fields": _fields(exc)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = _BY_STATUS.get(exc.status_code, ErrorCode(exc.status_code, "request_failed"))
        return envelope(error, _http_message(exc), request_id=request_id_of(request))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The catch-all, and the reason this module exists. Without it Starlette
        # re-raises, and whether the traceback reaches the client depends on the
        # server's debug flag -- which is a security control living in a
        # deployment setting somebody else sets.
        logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return envelope(INTERNAL_ERROR, GENERIC_FAILURE, request_id=request_id_of(request))


def request_id_of(request: Request) -> str:
    """The id the middleware assigned, or a placeholder if it did not run.

    The fallback matters: exception handlers run for requests that failed
    before middleware completed, and an error handler that raised ``KeyError``
    while reporting an error is a 500 with no envelope at all.
    """
    return str(getattr(request.state, "request_id", "unassigned"))


def _fields(exc: RequestValidationError) -> list[dict[str, str]]:
    """Which fields were wrong, and why -- without echoing what was sent.

    pydantic's own error list carries ``input``, the offending value verbatim.
    Reflecting that back means a request body appears in the response, and from
    there in any log or error tracker that records responses. The location and
    the rule are what a client needs to fix the call; the value is something
    they already have.
    """
    return [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())),
            "reason": error.get("msg", "invalid"),
        }
        for error in exc.errors()
    ]


def _http_message(exc: StarletteHTTPException) -> str:
    """A fixed message per status, ignoring any detail the framework attached.

    Starlette's 404 detail is "Not Found", which is harmless; a route that
    raised ``HTTPException(404, detail=f"no such session {sid}")`` would not
    be, because it reflects input. Fixed strings make that impossible rather
    than reviewable -- a route with something to say uses :class:`ApiError`,
    where the message is written knowing it will be published.
    """
    return {
        404: "no route matches this path",
        405: "that method is not allowed on this path",
    }.get(exc.status_code, "the request could not be completed")


__all__ = [
    "AMBIGUOUS_QUESTION",
    "DATABASE_UNAVAILABLE",
    "GENERIC_FAILURE",
    "INTERNAL_ERROR",
    "INVALID_REQUEST",
    "LLM_UNAVAILABLE",
    "NOT_READY",
    "PAYLOAD_TOO_LARGE",
    "QUERY_TIMEOUT",
    "RATE_LIMITED",
    "REQUEST_ID_HEADER",
    "SESSION_NOT_FOUND",
    "SQL_VALIDATION_FAILED",
    "ApiError",
    "ErrorCode",
    "code_for",
    "envelope",
    "install",
    "request_id_of",
]
