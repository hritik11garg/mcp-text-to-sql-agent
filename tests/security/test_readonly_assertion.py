"""`assert_read_only` against a real PostgreSQL, in both directions.

The assertion is only worth having if it *fails* for a writable role, so the
owner connection is tested here alongside the read-only one. A check that has
only ever been run against a passing case is a check nobody has seen work.

Postgres, not a mock: the whole point is that PostgreSQL's own privilege
functions answer the question, and a fake would be this file asserting what it
already believes.
"""

from __future__ import annotations

import psycopg
import pytest

from composition.resources import assert_read_only
from core.exceptions import ConfigurationError

pytestmark = [pytest.mark.integration, pytest.mark.security]

type Conn = psycopg.Connection[tuple[object, ...]]


class TestTheBoundaryHolds:
    def test_the_read_only_role_passes(self, ro_connection: Conn, catalog_schema: None) -> None:
        """With tables present, so the check has something to find and finds nothing."""
        assert_read_only(ro_connection)

    def test_it_is_not_passing_because_the_schema_is_empty(
        self, ro_connection: Conn, catalog_schema: None
    ) -> None:
        """A vacuous pass is the failure mode of a per-table check."""
        with ro_connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
            row = cur.fetchone()
        assert row is not None
        assert int(row[0]) > 0


class TestTheAssertionCanFail:
    def test_the_owner_connection_is_refused(self, owner_connection: Conn) -> None:
        """The exact misconfiguration this exists to catch: both URLs, one role.

        `DatabaseSettings` only compares the two DSN strings, which two
        spellings of the same host trivially pass. This is the check that looks
        at the role.
        """
        with pytest.raises(ConfigurationError) as caught:
            assert_read_only(owner_connection)

        message = str(caught.value)
        assert "DATABASE_RO_URL" in message
        # Names what to fix, not just that something is wrong.
        assert "SUPERUSER" in message or "public." in message

    def test_a_granted_write_is_detected(
        self, postgres_url: str, owner_connection: Conn, catalog_schema: None
    ) -> None:
        """Grant one INSERT, and the role that passed a moment ago must not."""
        from tests.conftest import _ro_libpq

        owner_connection.execute("GRANT INSERT ON public.customers TO sql_agent_ro")
        try:
            with (
                psycopg.connect(_ro_libpq(postgres_url), autocommit=True) as conn,
                pytest.raises(ConfigurationError, match=r"can write"),
            ):
                assert_read_only(conn)
        finally:
            owner_connection.execute("REVOKE INSERT ON public.customers FROM sql_agent_ro")

    def test_create_on_a_schema_is_detected(
        self, postgres_url: str, owner_connection: Conn, catalog_schema: None
    ) -> None:
        """CREATE is a write: a role that can add a table can add a trigger."""
        from tests.conftest import _ro_libpq

        owner_connection.execute("GRANT CREATE ON SCHEMA public TO sql_agent_ro")
        try:
            with (
                psycopg.connect(_ro_libpq(postgres_url), autocommit=True) as conn,
                pytest.raises(ConfigurationError, match=r"can create objects"),
            ):
                assert_read_only(conn)
        finally:
            owner_connection.execute("REVOKE CREATE ON SCHEMA public FROM sql_agent_ro")


class TestSystemSchemasAreExcluded:
    def test_pg_catalog_does_not_trip_the_check(self, ro_connection: Conn) -> None:
        """PostgreSQL grants the world read on these; a false positive here
        would make the assertion unpassable on every deployment."""
        assert_read_only(ro_connection)

    def test_a_temp_schema_does_not_trip_the_check(self, ro_connection: Conn) -> None:
        """A session's own pg_temp_N is writable by definition, and is not a
        breach of the boundary -- it is invisible to every other session and
        gone when this one ends."""
        ro_connection.execute("SET default_transaction_read_only = off")
        try:
            ro_connection.execute("CREATE TEMP TABLE scratch (id int)")
        except psycopg.Error:
            pytest.skip("this role cannot create temp tables, so there is nothing to exclude")
        finally:
            ro_connection.execute("SET default_transaction_read_only = on")

        assert_read_only(ro_connection)


class TestTheSecondBarrierIsReported:
    def test_the_role_sets_default_transaction_read_only(
        self, ro_connection: Conn, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Migration 002 claims two independent barriers. This is the other one."""
        with caplog.at_level("INFO", logger="composition.resources"):
            assert_read_only(ro_connection)
        assert "default_transaction_read_only=on" in caplog.text
