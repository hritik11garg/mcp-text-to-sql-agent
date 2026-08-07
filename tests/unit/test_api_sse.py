"""SSE framing -- the newline is the whole subject.

Server-sent events are newline-delimited: a field ends at ``\\n`` and an event
ends at ``\\n\\n``. So a raw newline inside a payload does not corrupt the
frame, it **ends** it, and whatever follows is parsed as a fresh event. That
makes a payload containing ``\\n\\nevent: done\\ndata: {}`` an injected
terminal event the server never sent.

This is not a hypothetical for this endpoint. The payload most likely to
contain a newline is the one it exists to send: **generated SQL is routinely
multi-line.**
"""

from __future__ import annotations

import json

import pytest

from api.sse import HEARTBEAT, ServerSentEvent

pytestmark = pytest.mark.unit


class TestAPayloadCannotEndItsOwnEvent:
    """The injection case, which is the reason this module is separate."""

    def test_multiline_sql_stays_one_event(self) -> None:
        sql = "SELECT id\nFROM orders\nWHERE total > 10"

        wire = ServerSentEvent("sql", {"sql": sql}).encode()

        assert wire.count("\n\n") == 1, "an event may only terminate once, at its end"
        assert wire.endswith("\n\n")
        assert "\nFROM orders" not in wire, "a raw newline reached the wire"

    def test_an_injected_event_does_not_survive_encoding(self) -> None:
        """The attack in full: a payload trying to forge a terminal event."""
        hostile = 'x\n\nevent: done\ndata: {"row_count": 999}\n\n'

        wire = ServerSentEvent("sql", {"sql": hostile}).encode()

        assert wire.count("\n\n") == 1

        # The forged text is still *present* -- escaping does not delete it,
        # and should not. What matters is that it is no longer on a line of its
        # own, because a field is only a field at the start of a line. Asserting
        # `"event: done" not in wire` would pass for the wrong reason and would
        # fail the day someone asks a question containing that string.
        lines = wire.split("\n")
        assert [line for line in lines if line.startswith("event:")] == ["event: sql"]
        assert len([line for line in lines if line.startswith("data:")]) == 1

        # And it survives as data: nothing is lost, it is escaped. A client
        # reading the payload back gets the original string byte for byte.
        assert json.loads(wire.split("data: ", 1)[1].strip())["sql"] == hostile

    @pytest.mark.parametrize("hostile", ["a\nb", "a\rb", "a\r\nb", "\n", "\u2028", "\u2029"])
    def test_control_characters_do_not_reach_the_wire(self, hostile: str) -> None:
        """``\\u2028`` and ``\\u2029`` are in this list on purpose.

        They are not newlines to Python and they do not end an SSE field, so
        the framing survives them either way. They *are* line terminators to
        a JavaScript parser, which is what every browser client of this
        endpoint will be. ``json.dumps`` escapes them because ``ensure_ascii``
        defaults to true, and this records that the default is load-bearing
        rather than incidental -- turning it off to keep unicode readable
        would reintroduce the problem for exactly one class of client.

        Python's own ``str.splitlines`` splits on both, which is how this
        list was found: it corrupted the script written to patch this test.
        """
        wire = ServerSentEvent("rows", {"value": hostile}).encode()

        body = wire.split("data: ", 1)[1]
        assert body.count("\n") == 2, "only the two framing newlines belong here"

        # The payload alone, without the two newlines that frame the event --
        # otherwise the bare "\n" case matches the framing and passes for a
        # reason that has nothing to do with escaping.
        payload = body.split("\n")[0]
        assert hostile not in payload, "the raw character reached the wire unescaped"

    def test_a_hostile_column_name_is_escaped_too(self) -> None:
        """Column names come from the database, not from a literal in this repo."""
        wire = ServerSentEvent("rows", {"columns": ["ok", "bad\n\nevent: error"]}).encode()

        assert wire.count("\n\n") == 1
        assert [line for line in wire.split("\n") if line.startswith("event:")] == ["event: rows"]


class TestTheEventNameIsValidated:
    """A name is written to its own line, so it is injectable the same way."""

    @pytest.mark.parametrize("name", ["sql", "rows", "done", "error", "stage"])
    def test_the_names_this_project_sends_are_legal(self, name: str) -> None:
        assert ServerSentEvent(name, {}).encode().startswith(f"event: {name}\n")

    @pytest.mark.parametrize(
        "name", ["bad name", "bad\nname", "", "UPPER", "trailing ", "x" * 40, "has-dash", "1st"]
    )
    def test_an_illegal_name_is_refused_at_construction(self, name: str) -> None:
        with pytest.raises(ValueError, match="illegal SSE event name"):
            ServerSentEvent(name, {})


class TestEncodingSurvivesRealDatabaseValues:
    """A result set holds whatever the database returned, not only JSON types."""

    def test_a_non_serialisable_value_does_not_abort_a_started_stream(self) -> None:
        """``default=str`` is a availability decision, not a convenience.

        By the time rows exist the response has already sent ``200``. A
        ``TypeError`` here would end the stream with no terminal event at all,
        leaving the client waiting on a socket that will never say anything.
        """
        from datetime import date
        from decimal import Decimal

        wire = ServerSentEvent(
            "rows", {"rows": [[Decimal("1.50"), date(2026, 8, 7), object()]]}
        ).encode()

        assert wire.endswith("\n\n")
        assert "1.50" in wire
        assert "2026-08-07" in wire


class TestTheHeartbeat:
    def test_it_is_a_comment_and_not_an_event(self) -> None:
        """A client must never mistake a keepalive for the terminal event."""
        assert HEARTBEAT.startswith(":")
        assert "event:" not in HEARTBEAT
        assert HEARTBEAT.endswith("\n\n")
