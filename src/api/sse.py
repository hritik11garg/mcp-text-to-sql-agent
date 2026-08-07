"""Server-sent event framing.

Small enough to inline into the route, and deliberately not, because the one
thing it does is a security control.

**SSE is a newline-delimited text protocol carrying attacker-influenced
data.** A field ends at ``\\n`` and an *event* ends at ``\\n\\n``. So any raw
newline reaching a ``data:`` line does not corrupt the frame -- it **ends** it,
and everything after becomes a new event the client parses and believes. That
is the same defect as CWE-117 log injection one layer out, with a worse
consequence: a log reader sees a confusing line, an SSE client sees a
well-formed ``done`` or ``rows`` event that the server never sent.

It is not theoretical here. The payload most likely to contain a newline is
the one this endpoint exists to send: **generated SQL is routinely multi-line.**
The first question ever streamed would have carried one.

The defence is that a payload is always a mapping, always serialised by
:func:`json.dumps`, and never a caller-supplied string placed on a line. JSON
escapes ``\\n`` as ``\\\\n``, so a newline in generated SQL becomes two safe
characters. :meth:`ServerSentEvent.encode` then asserts the result is
single-line rather than trusting that reasoning to survive the next edit --
the check costs a scan of a string that is about to be written to a socket
anyway, and it fails on the developer's machine rather than at a client.

Event *names* are validated for the same reason and are not merely internal
constants: a name is written to the ``event:`` line, and the code below is the
only thing standing between that line and whatever a future caller passes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

HEARTBEAT: Final = ": keepalive\n\n"
"""A comment frame. Ignored by every client, and it keeps the socket warm.

A generation takes tens of seconds against a free-tier provider (PERFORMANCE.md
records 29 s), and an intermediary that sees no bytes for that long is entitled
to conclude the connection is dead and close it. A comment is the protocol's
own answer to that: it is not an event, so a client cannot mistake it for one,
and it cannot be confused with the terminal event because it carries no name.
"""

_EVENT_NAME: Final = re.compile(r"\A[a-z][a-z_]{0,30}\Z")
"""What may appear on an ``event:`` line.

Restrictive on purpose -- lower-case and underscores is every name API.md
defines, and anything outside it is a mistake rather than a new feature.
Notably excludes whitespace, which is the character class that matters.
"""


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    """One event, framed.

    Args:
        event: The event name. Must match :data:`_EVENT_NAME`.
        data: The payload. A mapping, never a string -- see the module
            docstring. Serialised with :func:`json.dumps`, which is what makes
            a newline inside a value harmless.

    Raises:
        ValueError: the event name is not a legal name, or the encoded payload
            somehow still spans lines.
    """

    event: str
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not _EVENT_NAME.match(self.event):
            raise ValueError(f"illegal SSE event name: {self.event!r}")

    def encode(self) -> str:
        """The wire form: an ``event:`` line, a ``data:`` line, a blank line.

        ``default=str`` because a result set holds whatever the database
        returned -- ``Decimal``, ``date``, ``UUID`` -- and a
        :class:`TypeError` raised while serialising a *success* would abort a
        stream that had already sent ``200``, leaving the client with no
        terminal event at all. Coercing is the lesser failure and it is
        lossless for every type this actually sees.
        """
        payload = json.dumps(self.data, separators=(",", ":"), default=str)

        # Belt and braces over `json.dumps`, which already escapes control
        # characters. This asserts the property the framing *depends* on rather
        # than the implementation that currently provides it -- if a payload
        # ever reaches here pre-serialised, this is what catches it.
        if "\n" in payload or "\r" in payload:
            raise ValueError(f"SSE payload spans lines for event {self.event!r}")

        return f"event: {self.event}\ndata: {payload}\n\n"


__all__ = ["HEARTBEAT", "ServerSentEvent"]
