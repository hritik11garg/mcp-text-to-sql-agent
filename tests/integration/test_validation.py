"""The `EXPLAIN` stage, against a real planner.

The stages before this one are decidable from text and are unit-tested. This
file covers the only claim that needs a live database: that asking the planner
what it *would* do neither runs the query nor leaks how it failed.
"""

from __future__ import annotations

import time

import psycopg
import pytest

from core.settings import ExecutionSettings
from schema.catalog import SchemaCatalog, load_catalog
from validation.validator import SQLValidator, ValidationStage

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def catalog() -> SchemaCatalog:
    return SchemaCatalog(
        {
            "orders": frozenset({"id", "total_amount", "customer_id"}),
            "customers": frozenset({"id", "name", "email", "country"}),
        }
    )


@pytest.fixture
def validator(ro_connection: Conn, catalog: SchemaCatalog, catalog_schema: None) -> SQLValidator:
    return SQLValidator(ro_connection, catalog, ExecutionSettings())


class TestExplain:
    def test_a_valid_query_returns_a_cost_and_a_plan(self, validator: SQLValidator) -> None:
        result = validator.validate("SELECT id, total_amount FROM orders")

        assert result.valid
        assert result.estimated_cost is not None
        assert result.estimated_cost >= 0
        assert result.plan_summary
        assert result.tables == ("orders",)

    def test_explain_does_not_execute_the_query(self, validator: SQLValidator) -> None:
        """The claim the whole validation tier rests on.

        `EXPLAIN ANALYZE` would run this and take five seconds. Planning it
        takes none. If this test ever starts being slow, the tier the agent is
        told it may retry freely has quietly become the expensive one.
        """
        started = time.perf_counter()
        result = validator.validate("SELECT pg_sleep(5)")
        elapsed = time.perf_counter() - started

        assert result.valid
        assert elapsed < 1.0

    def test_a_join_plans_and_reports_both_tables(self, validator: SQLValidator) -> None:
        result = validator.validate(
            "SELECT c.name, o.total_amount FROM orders o JOIN customers c ON c.id = o.customer_id"
        )

        assert result.valid
        assert set(result.tables) == {"orders", "customers"}

    def test_cost_ceiling_rejects_an_expensive_plan(
        self, ro_connection: Conn, catalog: SchemaCatalog, catalog_schema: None
    ) -> None:
        """The bail-out signal that lets the agent avoid spending its execution
        budget on a query the planner already thinks is catastrophic."""
        strict = ExecutionSettings(max_estimated_cost=0.0001)
        validator = SQLValidator(ro_connection, catalog, strict)

        result = validator.validate("SELECT c.name FROM orders o, customers c")

        assert not result.valid
        assert result.error_type == "cost_exceeded"
        assert result.estimated_cost is not None


class TestDatabaseErrors:
    def test_unknown_table_is_classified(self, ro_connection: Conn, catalog_schema: None) -> None:
        """With an empty catalog the identifier stage stands aside, so this is
        the planner's rejection being translated rather than pre-empted."""
        validator = SQLValidator(ro_connection, SchemaCatalog({}), ExecutionSettings())

        result = validator.validate("SELECT id FROM no_such_table")

        assert not result.valid
        assert result.stage_failed is ValidationStage.EXPLAIN
        assert result.error_type == "table_not_found"

    def test_unreadable_table_is_permission_denied(
        self, ro_connection: Conn, catalog_schema: None
    ) -> None:
        """internal_payroll is revoked from the read-only role.

        Validation reports the refusal rather than hiding it, and the error
        type is distinct from "does not exist" -- the agent must not respond by
        retrying with different spelling.
        """
        validator = SQLValidator(ro_connection, SchemaCatalog({}), ExecutionSettings())

        result = validator.validate("SELECT amount FROM internal_payroll")

        assert not result.valid
        assert result.error_type == "permission_denied"

    def test_error_messages_do_not_leak_driver_internals(
        self, ro_connection: Conn, catalog_schema: None
    ) -> None:
        """MCP.md section 6 forbids raw driver output in tool errors."""
        validator = SQLValidator(ro_connection, SchemaCatalog({}), ExecutionSettings())

        result = validator.validate("SELECT id FROM no_such_table")
        message = result.message or ""

        assert "Traceback" not in message
        assert "psycopg" not in message.lower()
        assert "LINE" not in message
        assert "sql_agent" not in message

    def test_a_failed_explain_leaves_the_connection_usable(
        self, ro_connection: Conn, catalog_schema: None
    ) -> None:
        """A failed statement aborts its transaction. If the wrapper did not
        contain that, every later validation would fail with "current
        transaction is aborted" and bury the real cause."""
        validator = SQLValidator(ro_connection, SchemaCatalog({}), ExecutionSettings())

        assert not validator.validate("SELECT id FROM no_such_table").valid
        assert validator.validate("SELECT id FROM orders").valid


class TestCatalogLoading:
    def test_catalog_round_trips_from_the_indexed_elements(
        self, owner_connection: Conn, catalog_schema: None
    ) -> None:
        owner_connection.execute(
            "DELETE FROM agent_meta.schema_elements WHERE dataset = 'test_validation'"
        )
        owner_connection.execute(
            "INSERT INTO agent_meta.schema_elements "
            "(dataset, element_type, table_name, column_name, serialized, model_version) "
            "VALUES ('test_validation', 'column', 'orders', 'total_amount', 'x', 'm'), "
            "       ('test_validation', 'table', 'orders', NULL, 'y', 'm')"
        )

        loaded = load_catalog(owner_connection, dataset="test_validation")

        assert loaded.has_table("orders")
        assert loaded.has_column("orders", "total_amount")
        assert not loaded.has_column("orders", "nope")

    def test_an_unindexed_dataset_loads_empty(self, owner_connection: Conn) -> None:
        assert load_catalog(owner_connection, dataset="never_indexed").is_empty
