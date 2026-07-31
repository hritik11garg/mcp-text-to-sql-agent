"""The execution sandbox: limits a caller cannot raise.

Every test here supplies a hostile value for something the agent controls. The
claim under test is not "the limit works" but "the limit cannot be removed by
the party the limit exists to constrain".
"""

from __future__ import annotations

import psycopg
import pytest

from core.exceptions import SQLValidationError, StatementTimeoutError
from core.settings import ExecutionSettings
from execution.executor import AuditLog, SingleConnectionSource, SQLExecutor, apply_row_limit
from schema.catalog import SchemaCatalog
from validation.validator import SQLValidator

pytestmark = pytest.mark.security

type Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture(scope="module")
def bulk_table(owner_connection: Conn) -> None:
    owner_connection.execute(
        "CREATE TABLE IF NOT EXISTS public.many_rows AS SELECT g AS n FROM generate_series(1, 50) g"
    )
    owner_connection.execute("GRANT SELECT ON public.many_rows TO sql_agent_ro")


@pytest.fixture
def executor(ro_connection: Conn, catalog_schema: None, bulk_table: None) -> SQLExecutor:
    settings = ExecutionSettings(max_rows_default=5, max_rows_ceiling=10)
    validator = SQLValidator(ro_connection, SchemaCatalog({}), settings)
    return SQLExecutor(SingleConnectionSource(ro_connection), validator, settings)


class TestTheCallerCannotRaiseALimit:
    @pytest.mark.parametrize("requested", [11, 100, 10_000, 2**31])
    def test_row_ceiling_holds_against_any_request(
        self, executor: SQLExecutor, requested: int
    ) -> None:
        result = executor.execute("SELECT n FROM many_rows ORDER BY n", max_rows=requested)

        assert result.row_count <= 10

    @pytest.mark.parametrize("requested", [0, -1, -10_000])
    def test_nonsense_row_counts_are_floored_not_crashed(
        self, executor: SQLExecutor, requested: int
    ) -> None:
        result = executor.execute("SELECT n FROM many_rows ORDER BY n", max_rows=requested)

        assert result.row_count == 1

    def test_a_limit_written_into_the_sql_cannot_exceed_the_ceiling(
        self, executor: SQLExecutor
    ) -> None:
        """The model writes the SQL, so it can write its own LIMIT.

        The injected limit is applied to the parse tree afterwards, so the
        model's number is an upper bound it can lower and never raise.
        """
        result = executor.execute("SELECT n FROM many_rows ORDER BY n LIMIT 9999")

        assert result.row_count == 5

    def test_timeout_ceiling_holds(self, ro_connection: Conn, catalog_schema: None) -> None:
        settings = ExecutionSettings(statement_timeout_ms=100, statement_timeout_ceiling_ms=200)
        validator = SQLValidator(ro_connection, SchemaCatalog({}), settings)
        executor = SQLExecutor(SingleConnectionSource(ro_connection), validator, settings)

        with pytest.raises(StatementTimeoutError):
            executor.execute("SELECT pg_sleep(5)", timeout_ms=60_000)


class TestLimitInjectionCannotBeEscaped:
    """A row limit appended as text is escapable. On the AST it is not."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT n FROM many_rows -- trailing comment",
            "SELECT n FROM many_rows /* block comment */",
            "SELECT n FROM many_rows ;",
            "SELECT n FROM many_rows UNION ALL SELECT n FROM many_rows",
            "SELECT n FROM many_rows ORDER BY n LIMIT 40 OFFSET 5",
        ],
    )
    def test_the_limit_survives_hostile_shapes(self, executor: SQLExecutor, sql: str) -> None:
        result = executor.execute(sql)

        assert result.row_count <= 5

    def test_a_comment_cannot_swallow_the_limit(self) -> None:
        """`sql + " LIMIT 5"` after a line comment yields an unlimited query.

        This is the concrete reason the limit is set on the parse tree.
        """
        bounded, effective, _ = apply_row_limit("SELECT n FROM many_rows -- x", 5)

        assert effective == 5
        assert bounded.rstrip().endswith("LIMIT 6")


class TestWritesStillRefused:
    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM public.orders",
            "WITH gone AS (DELETE FROM public.orders RETURNING id) SELECT * FROM gone",
            "SELECT * INTO public.stolen2 FROM public.customers",
            "SELECT 1; DROP TABLE public.orders",
        ],
    )
    def test_execution_revalidates_and_refuses(self, executor: SQLExecutor, sql: str) -> None:
        """Execution never assumes validation was called first."""
        with pytest.raises(SQLValidationError):
            executor.execute(sql)


class TestAuditIntegrity:
    def test_generated_sql_cannot_reach_the_audit_trail(
        self, ro_connection: Conn, catalog_schema: None
    ) -> None:
        """The audit runs as the owner on a separate connection precisely so
        that a query cannot read, alter, or erase the record of itself."""
        for statement in (
            "SELECT * FROM agent_meta.query_audit",
            "DELETE FROM agent_meta.query_audit",
            "INSERT INTO agent_meta.query_audit (outcome) VALUES ('fake')",
        ):
            with pytest.raises(psycopg.Error):
                ro_connection.execute(statement)  # type: ignore[arg-type]

    def test_an_audit_failure_does_not_lose_the_query_result(
        self, ro_connection: Conn, owner_connection: Conn, catalog_schema: None, bulk_table: None
    ) -> None:
        """Availability over audit completeness, deliberately.

        A transient problem writing to agent_meta must not fail every read the
        system serves. The compensating control is that the error is logged
        with the same fields -- see the docstring on AuditLog.record.
        """
        settings = ExecutionSettings(max_rows_default=5, max_rows_ceiling=10)
        validator = SQLValidator(ro_connection, SchemaCatalog({}), settings)
        # An audit pointed at the read-only connection cannot write to
        # agent_meta -- the same shape as a broken audit connection.
        broken = AuditLog(ro_connection, db_role="sql_agent_ro")
        executor = SQLExecutor(
            SingleConnectionSource(ro_connection), validator, settings, audit=broken
        )

        result = executor.execute("SELECT n FROM many_rows ORDER BY n")

        assert result.row_count == 5
