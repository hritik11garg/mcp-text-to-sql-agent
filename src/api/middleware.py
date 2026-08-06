"""Request correlation, from a header this service does not trust.

``X-Request-Id`` is honoured because a caller behind a gateway wants one id
across every hop, and losing it at this boundary makes a distributed trace stop
exactly where the interesting part starts.

But it arrives from the network, and it goes on to two places that make an
unchecked string dangerous:

**Into the log.** A value containing a newline writes a second log line, and an
attacker who chooses that line chooses what the operator reads -- a forged
``ERROR authentication bypassed for admin``, or a fabricated entry that buries
the real one. That is CWE-117, log injection, and it is the reason the charset
below excludes control characters rather than merely trimming whitespace.

**Back out in the response**, in a header and in the error envelope. A header
value with ``\\r\\n`` in it is response splitting; a body value is only inert
because the response is JSON, which is a property of today's content type
rather than a control.

The answer is not to reject a bad id -- a 400 for a malformed correlation
header would fail requests that were otherwise fine, and would hand a prober a
way to tell this service apart from any other. It is to **replace** it: a
caller who sends something usable keeps it, and a caller who does not gets a
generated one and never learns which happened.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.errors import PAYLOAD_TOO_LARGE, REQUEST_ID_HEADER, envelope

MAX_REQUEST_ID_CHARS: Final = 128
"""Longer than any sane correlation id, short enough that a million of them in
flight is not a memory strategy. A gateway-generated trace id is ~32 hex."""

_SAFE_REQUEST_ID: Final = re.compile(rf"\A[A-Za-z0-9._:-]{{1,{MAX_REQUEST_ID_CHARS}}}\Z")
"""An allowlist, not a denylist of the characters known to be dangerous.

Anchored with ``\\A``/``\\Z`` rather than ``^``/``$`` deliberately: in Python
``$`` also matches immediately before a trailing newline, so ``^[\\w]+$``
accepts ``"abc\\n"`` -- which is precisely the input this pattern exists to
reject, and a mistake common enough to be worth naming here.
"""


def assign_request_id(supplied: str | None) -> str:
    """Return the caller's id if it is safe to repeat, otherwise a fresh one."""
    if supplied is not None and _SAFE_REQUEST_ID.match(supplied):
        return supplied
    return f"req_{uuid.uuid4().hex}"


class BodySizeLimitMiddleware:
    """Refuse an oversized body without buffering it.

    Pure ASGI rather than :class:`BaseHTTPMiddleware`, because this has to act
    on the request *stream*. ``BaseHTTPMiddleware`` hands a route the body only
    after Starlette has read it, so a limit enforced there is checked after the
    process has already allocated whatever the caller chose to send -- which is
    the cost the limit exists to avoid.

    **``Content-Length`` is checked first and is not trusted.** It is the cheap
    rejection: a caller who declares 90 MB is refused before a byte of it
    arrives. But it is a claim, and two kinds of caller do not make it
    truthfully -- a hostile one that understates it, and an ordinary one using
    ``Transfer-Encoding: chunked``, which has no ``Content-Length`` at all. So
    the received bytes are counted as they arrive and the limit is enforced
    again on the real total. Checking only the header is a limit that any
    attacker can opt out of by omitting it.

    On refusal the request body is not drained. The response goes out and the
    connection is closed by the server; continuing to read a body already known
    to be too large would be doing the work the limit exists to prevent.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self._max_bytes:
            await self._refuse(scope, send)
            return

        received = 0
        too_large = False

        async def counted() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    too_large = True
                    # Presented to the app as an empty final chunk. The app
                    # then fails to parse an empty body and returns 400, which
                    # would be a *misleading* answer -- so the flag above is
                    # what the outer send() consults, and the app's response is
                    # discarded. Raising here instead would surface as a 500.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded(message: Message) -> None:
            if too_large:
                if message["type"] == "http.response.start":
                    await self._refuse(scope, send)
                return
            await send(message)

        await self._app(scope, counted, guarded)

    async def _refuse(self, scope: Scope, send: Send) -> None:
        request_id = _request_id_from(scope)
        response = envelope(
            PAYLOAD_TOO_LARGE,
            f"request body exceeds {self._max_bytes} bytes",
            request_id=request_id,
        )
        await response(scope, _no_body, send)


async def _no_body() -> Message:  # pragma: no cover - a refusal reads nothing
    return {"type": "http.disconnect"}


def _declared_length(scope: Scope) -> int | None:
    for key, value in scope.get("headers", ()):
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                # An unparseable Content-Length is not this middleware's
                # decision to make -- the server or the app will reject it.
                # Guessing a number here would be inventing the fact the check
                # depends on.
                return None
    return None


def _request_id_from(scope: Scope) -> str:
    """The id assigned upstream, or a fresh one.

    This middleware runs *outside* :class:`RequestIdMiddleware` so that it can
    refuse before any body is read, which means the usual assignment has not
    happened yet on the refusal path. Generating one here keeps the guarantee
    that every response carries an id -- an operator investigating a flood of
    413s needs the same correlation key as for any other traffic.
    """
    state = scope.get("state") or {}
    existing = state.get("request_id")
    if isinstance(existing, str):
        return existing
    for key, value in scope.get("headers", ()):
        if key == REQUEST_ID_HEADER.lower().encode():
            return assign_request_id(value.decode("latin-1"))
    return assign_request_id(None)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a safe request id to every request and every response.

    Runs for *all* requests including ones that will 404, so an error handler
    always has an id to put in the envelope, and an operator investigating a
    scan has the same correlation key as for real traffic.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = assign_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


__all__ = [
    "MAX_REQUEST_ID_CHARS",
    "BodySizeLimitMiddleware",
    "RequestIdMiddleware",
    "assign_request_id",
]
