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

from api.errors import REQUEST_ID_HEADER

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


__all__ = ["MAX_REQUEST_ID_CHARS", "RequestIdMiddleware", "assign_request_id"]
