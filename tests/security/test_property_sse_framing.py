"""One event in, one event out -- over any payload at all.

The framing half of the fourth property in
docs/project/ENGINEERING_MATRIX.md section 38.

SSE is a newline-delimited text protocol carrying attacker-influenced data. A
field ends at ``\\n`` and an event ends at ``\\n\\n``, so a raw newline reaching
a ``data:`` line does not corrupt the frame -- it **ends** it, and everything
after becomes a second event the client parses and believes. The client cannot
tell it apart from one the server sent.

``src/api/sse.py`` defends this by serialising every payload through
``json.dumps``. That is a claim about a whole library's escaping behaviour over
every input, which is exactly the shape of claim an example-based test cannot
make and a property test can: **the assertion below is that no payload
whatsoever produces two events.**

The value flows from a query result, so the strategies deliberately include
what a database returns -- ``Decimal``, ``date``, ``UUID`` -- alongside text
containing the separators themselves.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from api.sse import HEARTBEAT, ServerSentEvent

pytestmark = pytest.mark.property

LEGAL_NAMES = st.sampled_from(
    ["stage", "rows", "done", "error", "sql", "token", "heartbeat", "a", "a_b_c"]
)

_JSON_LEAVES = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text()
)
"""Leaves that survive a JSON round trip unchanged.

``NaN`` and the infinities are excluded from *this* strategy and generated
separately below. ``json.dumps`` emits them as bare ``NaN``/``Infinity``, which
is not JSON -- Python reads it back, a browser's ``JSON.parse`` does not. That
is worth a named test rather than a silent exclusion, so the round-trip
property does not have to pretend the case does not exist.
"""

JSON_PAYLOADS = st.dictionaries(
    st.text(min_size=1, max_size=12),
    st.recursive(
        _JSON_LEAVES,
        lambda children: (
            st.lists(children, max_size=4)
            | st.dictionaries(st.text(max_size=8), children, max_size=4)
        ),
        max_leaves=10,
    ),
    max_size=6,
)

DATABASE_VALUES = st.dictionaries(
    st.text(min_size=1, max_size=12),
    _JSON_LEAVES
    | st.decimals(allow_nan=False, allow_infinity=False)
    | st.dates()
    | st.datetimes()
    | st.uuids()
    | st.binary(max_size=16),
    max_size=6,
)
"""What actually reaches this code: a row from PostgreSQL.

``encode`` passes ``default=str`` for these, and the reason is in its
docstring -- a ``TypeError`` raised while serialising a *success* would abort a
stream that had already sent its 200, leaving the client with no terminal
event. So the property here is not that they round-trip; it is that they still
produce exactly one well-formed event.
"""

HOSTILE_TEXT = st.sampled_from(
    [
        "\n",
        "\r\n",
        "\n\n",
        "\r",
        "\n\nevent: done\ndata: {}\n\n",
        '\ndata: {"rows": []}\n\n',
        "\u2028",  # LINE SEPARATOR: a line break to a JS parser, not to JSON
        "\u2029",  # PARAGRAPH SEPARATOR: likewise
        "\x00",
        "\x1b[31m",
        "</script><script>alert(1)</script>",
        "\\n\\n",
    ]
)
"""Payloads chosen to forge a second event, mixed in with the generated ones.

Hypothesis generates ``\\n`` on its own readily enough, but it will not
assemble ``\\n\\nevent: done\\ndata: {}\\n\\n`` in any reasonable number of
examples. A property test is not an excuse to stop thinking of the adversarial
case; these are the cases, and the generated payloads are what covers the ones
nobody listed.
"""


def _frames(stream: str) -> list[str]:
    """Split a stream the way a client does: on a blank line."""
    return [frame for frame in stream.split("\n\n") if frame]


class TestOnePayloadIsAlwaysOneEvent:
    """The security property. Everything else in this module supports it."""

    @given(LEGAL_NAMES, JSON_PAYLOADS)
    def test_the_wire_form_holds_exactly_three_newlines(
        self, name: str, payload: dict[str, Any]
    ) -> None:
        """``event:``, ``data:``, and the blank line that ends the frame.

        Counting is the whole assertion. A fourth newline anywhere is a second
        field or a second event, and there is no payload for which either is
        correct.
        """
        wire = ServerSentEvent(name, payload).encode()

        assert wire.count("\n") == 3
        assert wire.endswith("\n\n")
        assert "\r" not in wire

    @given(LEGAL_NAMES, JSON_PAYLOADS)
    def test_a_client_sees_one_frame_with_the_name_the_server_chose(
        self, name: str, payload: dict[str, Any]
    ) -> None:
        wire = ServerSentEvent(name, payload).encode()

        frames = _frames(wire)

        assert len(frames) == 1
        event_line, data_line = frames[0].split("\n")
        assert event_line == f"event: {name}"
        assert data_line.startswith("data: ")

    @given(LEGAL_NAMES, st.dictionaries(st.text(min_size=1, max_size=8), HOSTILE_TEXT, max_size=4))
    def test_a_payload_that_spells_out_an_event_is_still_one_event(
        self, name: str, payload: dict[str, str]
    ) -> None:
        """The forgery attempt, stated directly.

        If any of these escaped, a client would act on a ``done`` or ``rows``
        event the server never sent -- which for this UI means rendering
        fabricated results as though they came from the database.

        The assertion is on **lines beginning** ``event:``, not on occurrences
        of the substring. The first version counted substrings and failed
        against ``{"0": "\\n\\nevent: done\\ndata: {}\\n\\n"}``, whose wire form
        genuinely does contain the text ``event: done`` -- escaped, inside the
        data line, on the same line as everything else. That is the defence
        working, and an assertion that calls it a failure is an assertion that
        would eventually be "fixed" by weakening the escaping.
        """
        wire = ServerSentEvent(name, payload).encode()

        assert len(_frames(wire)) == 1
        assert sum(line.startswith("event:") for line in wire.split("\n")) == 1
        assert sum(line.startswith("data:") for line in wire.split("\n")) == 1

    @given(LEGAL_NAMES, DATABASE_VALUES)
    def test_database_values_produce_one_event_even_when_coerced(
        self, name: str, payload: dict[str, Any]
    ) -> None:
        """``default=str`` is a coercion, and a coercion is a place to leak a
        newline: ``str()`` of a value is not escaped by anything downstream."""
        wire = ServerSentEvent(name, payload).encode()

        assert wire.count("\n") == 3
        assert len(_frames(wire)) == 1

    @given(LEGAL_NAMES, JSON_PAYLOADS, st.integers(min_value=0, max_value=3))
    def test_heartbeats_never_merge_with_an_event(
        self, name: str, payload: dict[str, Any], beats: int
    ) -> None:
        """A comment frame keeps the socket warm and must not become an event.

        Concatenation matters because that is what the route does: heartbeats
        are interleaved with real events on one stream, and a frame that ends
        one character short would swallow the next.
        """
        stream = HEARTBEAT * beats + ServerSentEvent(name, payload).encode()

        events = [frame for frame in _frames(stream) if not frame.startswith(":")]

        assert len(events) == 1


class TestRoundTrip:
    @given(LEGAL_NAMES, JSON_PAYLOADS)
    def test_a_json_payload_survives_intact(self, name: str, payload: dict[str, Any]) -> None:
        """Framing that is safe but lossy would be a different bug, not a fix."""
        wire = ServerSentEvent(name, payload).encode()

        data_line = wire.split("\n")[1]

        assert json.loads(data_line.removeprefix("data: ")) == payload

    @given(LEGAL_NAMES, JSON_PAYLOADS)
    def test_the_data_line_is_ascii(self, name: str, payload: dict[str, Any]) -> None:
        """``json.dumps`` escapes non-ASCII by default, which is what makes
        ``\\u2028`` -- a line terminator to a JavaScript parser but not to a
        JSON one -- harmless here rather than a second framing question."""
        wire = ServerSentEvent(name, payload).encode()

        wire.split("\n")[1].encode("ascii")


class TestNonJsonFloats:
    """``NaN`` and the infinities, excluded above and named here.

    ``json.dumps`` writes them bare. Python reads them back; a browser's
    ``JSON.parse`` raises. So the *framing* property holds -- one event, no
    newline -- and the *round-trip* property does not, and the honest thing is
    to assert exactly that rather than quietly leave the case ungenerated.

    Not a defect worth fixing today: nothing in a result set produces them, as
    PostgreSQL's ``float8`` NaN arrives as a ``Decimal`` or a float that
    ``psycopg`` has already handled. Recorded so that if one ever does, the
    client-side failure is already written down.
    """

    @given(LEGAL_NAMES, st.sampled_from([float("nan"), float("inf"), float("-inf")]))
    def test_framing_still_holds(self, name: str, value: float) -> None:
        wire = ServerSentEvent(name, {"value": value}).encode()

        assert wire.count("\n") == 3
        assert len(_frames(wire)) == 1

    def test_but_the_result_is_not_strict_json(self) -> None:
        wire = ServerSentEvent("done", {"value": float("nan")}).encode()

        assert "NaN" in wire


class TestEventNames:
    """The name is written to a line of its own, so it is framing too."""

    @given(st.text(max_size=40).filter(lambda name: not re.fullmatch(r"[a-z][a-z_]{0,30}", name)))
    def test_an_illegal_name_is_refused_at_construction(self, name: str) -> None:
        """Refused where it is cheap to refuse, rather than at a client.

        A name containing a newline would put arbitrary text on its own line
        in a position a client reads as protocol -- the same forgery as a
        payload newline, one field earlier, and not covered by ``json.dumps``
        because the name is never serialised.
        """
        with pytest.raises(ValueError, match="illegal SSE event name"):
            ServerSentEvent(name, {})

    @given(LEGAL_NAMES)
    def test_every_documented_name_is_accepted(self, name: str) -> None:
        assert ServerSentEvent(name, {}).encode().startswith(f"event: {name}\n")


class TestNothingRaisesOnASuccessPath:
    """``encode`` is called after the response has begun.

    Once a 200 and the first event are on the wire, an exception cannot become
    an error status -- the client is left with a stream that stops. So the
    property is that encoding a *plausible* payload never raises, which is
    weaker than "never raises" and is the claim the route actually depends on.
    """

    @given(LEGAL_NAMES, DATABASE_VALUES)
    def test_encoding_a_database_row_never_raises(self, name: str, payload: dict[str, Any]) -> None:
        ServerSentEvent(name, payload).encode()

    @given(
        LEGAL_NAMES,
        st.dictionaries(
            st.text(min_size=1, max_size=8),
            st.one_of(
                st.just(Decimal("1.5")),
                st.just(dt.date(2026, 8, 9)),
                st.just(uuid.uuid4()),
                st.just(b"\x00\xff"),
            ),
            max_size=4,
        ),
    )
    def test_the_types_named_in_the_docstring_are_the_types_handled(
        self, name: str, payload: dict[str, Any]
    ) -> None:
        """``encode``'s docstring names ``Decimal``, ``date`` and ``UUID``.

        A docstring that names types nothing tests is a comment. This is the
        cheapest possible way to make it a claim.
        """
        assert ServerSentEvent(name, payload).encode().count("\n") == 3
