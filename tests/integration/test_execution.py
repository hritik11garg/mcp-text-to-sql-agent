"""Execution against a real database, under real limits.

Row limiting and the audit trail are the two things a fake connection cannot
prove: one depends on how many rows the database actually has, and the other on
a second connection with privileges the first one does not hold.
"""

from __future__ import annotations

import psycopg
import pytest

from core.exceptions import PermissionDeniedError, SQLValidationError, StatementTimeoutError
from core.settings import ExecutionSettings
from execution.executor import AuditLog, SingleConnectionSource, SQLExecutor
from schema.catalog import SchemaCatalog
from validation.validator import SQLValidator

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[tuple[object, ...]]

CATALOG = SchemaCatalog(
    {
        "orders": frozenset({"id", "total_amount", "customer_id"}),
        "customers": frozenset({"id", "name", "email", "country"}),
        "many_rows": frozenset({"n"}),
    }
)


@pytest.fixture(scope="module")
def bulk_table(owner_connection: Conn) -> None:
    """Enough rows that a row limit has something to cut."""
    owner_connection.execute(
        "CREATE TABLE IF NOT EXISTS public.many_rows AS SELECT g AS n FROM generate_series(1, 50) g"
    )
    owner_connection.execute("GRANT SELECT ON public.many_rows TO sql_agent_ro")


@pytest.fixture
def audit(owner_connection: Conn) -> AuditLog:
    owner_connection.execute("DELETE FROM agent_meta.query_audit")
    return AuditLog(owner_connection, db_role="sql_agent_ro")


@pytest.fixture
def executor(
    ro_connection: Conn, catalog_schema: None, bulk_table: None, audit: AuditLog
) -> SQLExecutor:
    settings = ExecutionSettings(max_rows_default=10, max_rows_ceiling=20)
    validator = SQLValidator(ro_connection, CATALOG, settings)
    return SQLExecutor(SingleConnectionSource(ro_connection), validator, settings, audit=audit)


class TestExecution:
    def test_returns_rows_and_column_names(self, executor: SQLExecutor) -> None:
        result = executor.execute("SELECT n FROM many_rows ORDER BY n")

        assert result.columns == ("n",)
        assert result.rows[0] == (1,)

    def test_the_default_row_limit_applies(self, executor: SQLExecutor) -> None:
        result = executor.execute("SELECT n FROM many_rows ORDER BY n")

        assert result.row_count == 10
        assert result.truncated

    def test_a_caller_may_ask_for_fewer(self, executor: SQLExecutor) -> None:
        result = executor.execute("SELECT n FROM many_rows ORDER BY n", max_rows=3)

        assert result.row_count == 3
        assert result.truncated

    def test_a_caller_may_not_ask_for_more(self, executor: SQLExecutor) -> None:
        """The ceiling is 20. Asking for a thousand gets 20."""
        result = executor.execute("SELECT n FROM many_rows ORDER BY n", max_rows=1_000)

        assert result.row_count == 20
        assert result.truncated

    def test_a_complete_result_is_not_marked_truncated(self, executor: SQLExecutor) -> None:
        """The distinction the extra fetched row exists to make."""
        result = executor.execute("SELECT n FROM many_rows WHERE n <= 4 ORDER BY n")

        assert result.row_count == 4
        assert not result.truncated

    def test_a_caller_limit_reached_exactly_is_not_truncation(self, executor: SQLExecutor) -> None:
        """The query asked for five rows and got five. Nothing was lost, and
        telling the agent otherwise would send it looking for missing data."""
        result = executor.execute("SELECT n FROM many_rows ORDER BY n LIMIT 5")

        assert result.row_count == 5
        assert not result.truncated

    def test_the_executed_sql_is_reported(self, executor: SQLExecutor) -> None:
        """The query the agent wrote and the query the database ran differ."""
        result = executor.execute("SELECT n FROM many_rows ORDER BY n")

        assert "LIMIT" in result.executed_sql
        assert result.row_limit == 10


class TestItDoesNotTrustTheCaller:
    def test_it_revalidates_rather_than_assuming(self, executor: SQLExecutor) -> None:
        """Another MCP host can call execute_sql directly. A tool that is only
        safe when invoked in the right order is not safe."""
        with pytest.raises(SQLValidationError) as excinfo:
            executor.execute("DELETE FROM orders")

        assert excinfo.value.error_type == "not_read_only"

    def test_a_data_modifying_cte_is_refused_at_execution_too(self, executor: SQLExecutor) -> None:
        with pytest.raises(SQLValidationError):
            executor.execute("WITH gone AS (DELETE FROM orders RETURNING id) SELECT * FROM gone")

    def test_an_unreadable_table_raises_permission_denied(self, executor: SQLExecutor) -> None:
        with pytest.raises((PermissionDeniedError, SQLValidationError)):
            executor.execute("SELECT amount FROM internal_payroll")

    def test_the_statement_timeout_is_enforced(
        self, ro_connection: Conn, catalog_schema: None, audit: AuditLog
    ) -> None:
        settings = ExecutionSettings(statement_timeout_ms=100, statement_timeout_ceiling_ms=200)
        validator = SQLValidator(ro_connection, SchemaCatalog({}), settings)
        executor = SQLExecutor(
            SingleConnectionSource(ro_connection), validator, settings, audit=audit
        )

        with pytest.raises(StatementTimeoutError):
            executor.execute("SELECT pg_sleep(3)")


class TestAudit:
    def test_a_successful_query_is_recorded(
        self, executor: SQLExecutor, owner_connection: Conn
    ) -> None:
        executor.execute("SELECT n FROM many_rows ORDER BY n", request_id="req-1")

        row = owner_connection.execute(
            "SELECT outcome, row_count, truncated, db_role, request_id "
            "FROM agent_meta.query_audit WHERE request_id = 'req-1'"
        ).fetchone()

        assert row == ("success", 10, True, "sql_agent_ro", "req-1")

    def test_a_rejected_query_is_recorded_too(
        self, executor: SQLExecutor, owner_connection: Conn
    ) -> None:
        """An attempt that never ran is exactly what an audit trail is for."""
        with pytest.raises(SQLValidationError):
            executor.execute("DROP TABLE orders", request_id="req-2")

        row = owner_connection.execute(
            "SELECT outcome, error_type FROM agent_meta.query_audit WHERE request_id = 'req-2'"
        ).fetchone()

        assert row == ("rejected", "not_read_only")

    def test_result_values_are_never_stored(
        self, executor: SQLExecutor, owner_connection: Conn
    ) -> None:
        """Writing rows here would copy the protected data into a second store
        and defeat the point of bounding what the read-only role can reach."""
        executor.execute("SELECT email FROM customers", request_id="req-3")

        row = owner_connection.execute(
            "SELECT generated_sql, question FROM agent_meta.query_audit WHERE request_id = 'req-3'"
        ).fetchone()

        assert row is not None
        assert "example.com" not in str(row)

    def test_the_read_only_role_cannot_read_its_own_audit_trail(
        self, ro_connection: Conn, executor: SQLExecutor
    ) -> None:
        executor.execute("SELECT n FROM many_rows", request_id="req-4")

        with pytest.raises(psycopg.Error):
            ro_connection.execute("SELECT * FROM agent_meta.query_audit")
