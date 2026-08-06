"""The composition root's own behaviour, with every connection faked.

The privilege queries themselves are asserted against a real PostgreSQL in
tests/security/test_readonly_assertion.py -- they are questions only a database
can answer. What is here is the wiring around them: that the assertion runs at
all, and that refusing does not leak a connection.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.conftest import build_settings

from composition import resources as resources_module
from core.exceptions import ConfigurationError


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.statements.append((sql, params))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[FakeConnection]:
    """Replace the connect call, recording every connection handed out."""
    connections: list[FakeConnection] = []

    def fake_connect(*_args: Any, **_kwargs: Any) -> FakeConnection:
        conn = FakeConnection()
        connections.append(conn)
        return conn

    monkeypatch.setattr(resources_module, "_connect", fake_connect)
    return connections


def make_resources() -> resources_module.Resources:
    """A Resources whose settings are never read past `database`."""
    return resources_module.Resources(build_settings())


class TestTheAssertionRunsOnOpen:
    def test_opening_the_read_only_connection_asserts_it(
        self, monkeypatch: pytest.MonkeyPatch, opened: list[FakeConnection]
    ) -> None:
        """Five entrypoints is five places to forget. It happens here instead."""
        checked: list[object] = []
        monkeypatch.setattr(resources_module, "assert_read_only", checked.append)

        conn = make_resources().readonly

        assert checked == [conn]

    def test_the_owner_connection_is_not_asserted(
        self, monkeypatch: pytest.MonkeyPatch, opened: list[FakeConnection]
    ) -> None:
        """The owner is *supposed* to write. Asserting it would refuse every start."""
        checked: list[object] = []
        monkeypatch.setattr(resources_module, "assert_read_only", checked.append)

        _ = make_resources().owner

        assert checked == []

    def test_the_connection_is_cached_and_asserted_once(
        self, monkeypatch: pytest.MonkeyPatch, opened: list[FakeConnection]
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(resources_module, "assert_read_only", calls.append)

        resources = make_resources()
        first, second = resources.readonly, resources.readonly

        assert first is second
        assert len(calls) == 1


class TestRefusalDoesNotLeak:
    def test_a_refused_connection_is_closed(
        self, monkeypatch: pytest.MonkeyPatch, opened: list[FakeConnection]
    ) -> None:
        """A crash loop against a bad configuration would otherwise consume a
        server-side connection slot per restart, turning one misconfigured
        deployment into an outage for everything else on that database."""

        def refuse(_conn: object) -> None:
            raise ConfigurationError("DATABASE_RO_URL can write")

        monkeypatch.setattr(resources_module, "assert_read_only", refuse)

        with pytest.raises(ConfigurationError):
            _ = make_resources().readonly

        assert opened
        assert opened[0].closed

    def test_a_refused_connection_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch, opened: list[FakeConnection]
    ) -> None:
        """Caching a closed connection would turn one startup failure into
        every subsequent access failing for a different, misleading reason."""
        monkeypatch.setattr(
            resources_module,
            "assert_read_only",
            lambda _conn: (_ for _ in ()).throw(ConfigurationError("nope")),
        )
        resources = make_resources()

        with pytest.raises(ConfigurationError):
            _ = resources.readonly

        assert resources._readonly is None


class TestTheSchemaIsScopedWithTheAssertion:
    """Generated SQL names bare tables; `EXPLAIN` resolves them through the
    session's `search_path`. A catalog built from one schema and a session
    pointed at another is the failure that returns a plausible answer from the
    wrong tables rather than an error.
    """

    def test_the_search_path_is_set_on_the_read_only_session(
        self, monkeypatch: pytest.MonkeyPatch, opened: list[FakeConnection]
    ) -> None:
        monkeypatch.setattr(resources_module, "assert_read_only", lambda _conn: None)
        resources = make_resources()

        conn = resources.readonly

        assert conn.statements, "the session was never scoped"  # type: ignore[attr-defined]
        sql, params = conn.statements[0]  # type: ignore[attr-defined]
        assert "search_path" in sql
        assert params == ("public",)

    def test_the_schema_is_bound_not_interpolated(
        self, monkeypatch: pytest.MonkeyPatch, opened: list[FakeConnection]
    ) -> None:
        """Configuration, but configuration that reaches SQL. This project does
        not compose that category into a statement."""
        monkeypatch.setattr(resources_module, "assert_read_only", lambda _conn: None)
        resources = make_resources()

        sql, _ = resources.readonly.statements[0]  # type: ignore[attr-defined]

        assert "public" not in sql

    def test_a_session_is_not_scoped_before_it_is_proved(
        self, monkeypatch: pytest.MonkeyPatch, opened: list[FakeConnection]
    ) -> None:
        """Ordering, and it is the ordering that matters: a connection that
        fails the read-only assertion must be closed without having been
        configured to look like a working one."""
        monkeypatch.setattr(
            resources_module,
            "assert_read_only",
            lambda _conn: (_ for _ in ()).throw(ConfigurationError("nope")),
        )
        resources = make_resources()

        with pytest.raises(ConfigurationError):
            _ = resources.readonly

        assert opened[0].statements == []
