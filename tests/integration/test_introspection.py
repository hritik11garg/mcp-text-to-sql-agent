"""Introspection against a real PostgreSQL catalog.

Reading pg_catalog is exactly the kind of code that looks right and returns
subtly wrong data -- a dropped column that still appears, a composite foreign
key whose columns get paired in the wrong order, a table the agent cannot
actually read. None of that is visible without a real database.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from schema.introspection import PostgresIntrospector
from schema.models import SchemaSnapshot

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def snapshot(ro_connection: Conn, catalog_schema: None) -> SchemaSnapshot:
    return PostgresIntrospector(ro_connection, schema="public").snapshot()


def table_named(snapshot: SchemaSnapshot, name: str) -> Any:
    return next(table for table in snapshot.tables if table.name == name)


class TestTablesAndColumns:
    def test_finds_the_target_tables(self, snapshot: SchemaSnapshot) -> None:
        assert {"customers", "orders"} <= {table.name for table in snapshot.tables}

    def test_reads_table_comments(self, snapshot: SchemaSnapshot) -> None:
        assert table_named(snapshot, "customers").comment == "One row per customer account"

    def test_reads_column_comments(self, snapshot: SchemaSnapshot) -> None:
        column = next(
            c for c in table_named(snapshot, "orders").columns if c.name == "total_amount"
        )
        assert column.comment == "Order total including tax, in USD"

    def test_reports_real_postgres_types(self, snapshot: SchemaSnapshot) -> None:
        # format_type, not information_schema.data_type -- the latter reports
        # "numeric" and drops the precision the model benefits from seeing.
        column = next(
            c for c in table_named(snapshot, "orders").columns if c.name == "total_amount"
        )
        assert column.data_type == "numeric(12,2)"

    def test_reports_nullability(self, snapshot: SchemaSnapshot) -> None:
        columns = {c.name: c for c in table_named(snapshot, "customers").columns}
        assert columns["name"].is_nullable is False
        assert columns["country"].is_nullable is True

    def test_excludes_system_columns(self, snapshot: SchemaSnapshot) -> None:
        # ctid, xmin and friends have attnum <= 0 and are not answerable.
        names = {c.name for table in snapshot.tables for c in table.columns}
        assert not names & {"ctid", "xmin", "tableoid", "cmin"}


class TestPrivilegeFiltering:
    """Only index what the read-only role can actually SELECT.

    Cataloguing an unreadable table produces retrieval hits that generate SQL
    the database then refuses -- a confident answer followed by a permission
    error, which is worse than not knowing the table exists.
    """

    def test_unreadable_table_is_not_catalogued(self, snapshot: SchemaSnapshot) -> None:
        assert "internal_payroll" not in {table.name for table in snapshot.tables}

    def test_owner_sees_what_the_readonly_role_cannot(
        self, owner_connection: Conn, catalog_schema: None
    ) -> None:
        # Proves the previous assertion is about privileges rather than about
        # the table simply not existing.
        owner_view = PostgresIntrospector(owner_connection, schema="public").snapshot()
        assert "internal_payroll" in {table.name for table in owner_view.tables}


class TestForeignKeys:
    def test_finds_the_join_path(self, snapshot: SchemaSnapshot) -> None:
        edges = {
            (fk.from_table, fk.from_column, fk.to_table, fk.to_column)
            for fk in snapshot.foreign_keys
        }
        assert ("orders", "customer_id", "customers", "id") in edges

    def test_composite_keys_pair_columns_in_order(
        self, owner_connection: Conn, catalog_schema: None
    ) -> None:
        """The failure this guards against is silent and produces wrong SQL.

        Flattening conkey and confkey without ordinality pairs (a, b) with
        (y, x) whenever the declaration order differs, and the resulting join
        still parses and runs -- it just returns the wrong rows.
        """
        owner_connection.execute("""
            CREATE TABLE IF NOT EXISTS public.composite_parent (
                a int, b int, PRIMARY KEY (a, b)
            )
        """)
        owner_connection.execute("""
            CREATE TABLE IF NOT EXISTS public.composite_child (
                y int, x int,
                CONSTRAINT composite_fk FOREIGN KEY (x, y)
                    REFERENCES public.composite_parent (a, b)
            )
        """)
        try:
            snapshot = PostgresIntrospector(owner_connection, schema="public").snapshot()
            pairs = [
                (fk.from_column, fk.to_column)
                for fk in snapshot.foreign_keys
                if fk.constraint_name == "composite_fk"
            ]
            assert pairs == [("x", "a"), ("y", "b")]
        finally:
            owner_connection.execute("DROP TABLE IF EXISTS public.composite_child")
            owner_connection.execute("DROP TABLE IF EXISTS public.composite_parent")
