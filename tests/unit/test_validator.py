"""The four validation stages that never touch the database.

The connection these tests pass in **raises if it is used**. That is the point:
validation is advertised to the agent as safe to call in a loop, and a stage
that quietly opened a cursor would make the cheap tier expensive without
anything failing.
"""

from __future__ import annotations

from typing import Any, NoReturn

import pytest

from core.settings import ExecutionSettings
from schema.catalog import SchemaCatalog
from validation.validator import (
    MAX_SQL_CHARS,
    SQLValidator,
    ValidationStage,
)

pytestmark = pytest.mark.unit


class ExplodingConnection:
    """A connection that fails the test if a stage reaches for the database."""

    def cursor(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("this stage must not touch the database")

    def transaction(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("this stage must not touch the database")


@pytest.fixture
def catalog() -> SchemaCatalog:
    return SchemaCatalog(
        {
            "orders": frozenset({"id", "total_amount", "customer_id", "placed_at"}),
            "customers": frozenset({"id", "name", "email", "country"}),
        }
    )


@pytest.fixture
def validator(catalog: SchemaCatalog) -> SQLValidator:
    return SQLValidator(ExplodingConnection(), catalog, ExecutionSettings())  # type: ignore[arg-type]


class TestParseStage:
    def test_empty_query_is_refused(self, validator: SQLValidator) -> None:
        result = validator.validate_static("   ")

        assert not result.valid
        assert result.stage_failed is ValidationStage.PARSE
        assert result.error_type == "syntax_error"

    def test_syntax_error_is_reported_with_the_parser_message(
        self, validator: SQLValidator
    ) -> None:
        result = validator.validate_static("SELECT nope FROM")

        assert not result.valid
        assert result.stage_failed is ValidationStage.PARSE
        assert result.message

    def test_oversized_input_is_refused_before_parsing(self, validator: SQLValidator) -> None:
        """A megabyte of nested parentheses is a DoS primitive, not a query."""
        result = validator.validate_static("SELECT " + "(" * (MAX_SQL_CHARS + 10))

        assert not result.valid
        assert result.stage_failed is ValidationStage.PARSE
        assert "limit" in (result.message or "")


class TestSingleStatement:
    def test_stacked_statements_are_refused(self, validator: SQLValidator) -> None:
        result = validator.validate_static("SELECT 1; DROP TABLE orders")

        assert not result.valid
        assert result.stage_failed is ValidationStage.SINGLE_STATEMENT
        assert result.error_type == "multiple_statements"

    def test_two_selects_are_also_refused(self, validator: SQLValidator) -> None:
        """Nothing about the second statement being harmless makes it allowed."""
        result = validator.validate_static("SELECT 1; SELECT 2")

        assert not result.valid
        assert result.error_type == "multiple_statements"

    def test_a_trailing_semicolon_is_fine(self, validator: SQLValidator) -> None:
        result = validator.validate_static("SELECT id FROM orders;")

        assert result.valid


class TestReadOnlyStage:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO orders (id) VALUES (1)",
            "UPDATE orders SET total_amount = 0",
            "DELETE FROM orders",
            "CREATE TABLE evil (a int)",
            "DROP TABLE orders",
            "ALTER TABLE orders ADD COLUMN x int",
            "TRUNCATE orders",
        ],
    )
    def test_write_statements_are_refused(self, validator: SQLValidator, sql: str) -> None:
        result = validator.validate_static(sql)

        assert not result.valid
        assert result.stage_failed is ValidationStage.READ_ONLY
        assert result.error_type == "not_read_only"

    def test_data_modifying_cte_is_refused(self, validator: SQLValidator) -> None:
        """The bypass a root-node check misses.

        PostgreSQL allows DML inside a CTE. This parses with a ``Select`` at
        the root and deletes every row in the table, so only the full tree walk
        catches it.
        """
        result = validator.validate_static(
            "WITH gone AS (DELETE FROM orders RETURNING id) SELECT * FROM gone"
        )

        assert not result.valid
        assert result.stage_failed is ValidationStage.READ_ONLY
        assert "CTE" in (result.message or "")

    def test_insert_inside_a_cte_is_refused(self, validator: SQLValidator) -> None:
        result = validator.validate_static(
            "WITH added AS (INSERT INTO orders (id) VALUES (1) RETURNING id) SELECT * FROM added"
        )

        assert not result.valid
        assert result.error_type == "not_read_only"

    def test_select_into_is_refused(self, validator: SQLValidator) -> None:
        """SELECT ... INTO creates a table. Root is Select, tree holds no DDL
        node, so it is invisible to both other checks."""
        result = validator.validate_static("SELECT * INTO stolen FROM customers")

        assert not result.valid
        assert result.stage_failed is ValidationStage.READ_ONLY
        assert "INTO" in (result.message or "")

    def test_row_locking_is_refused(self, validator: SQLValidator) -> None:
        result = validator.validate_static("SELECT id FROM orders FOR UPDATE")

        assert not result.valid
        assert result.error_type == "not_read_only"

    def test_unmodelled_commands_are_refused(self, validator: SQLValidator) -> None:
        """sqlglot parses anything it does not understand into ``Command``.

        Accepting an opaque node means trusting the parser exactly where it
        says it does not know what the input is.
        """
        result = validator.validate_static("VACUUM FULL orders")

        assert not result.valid
        assert result.stage_failed is ValidationStage.READ_ONLY

    def test_plain_select_passes_the_stage(self, validator: SQLValidator) -> None:
        result = validator.validate_static("SELECT id FROM orders")

        assert result.valid

    def test_read_only_cte_passes_the_stage(self, validator: SQLValidator) -> None:
        result = validator.validate_static(
            "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent"
        )

        assert result.valid

    def test_set_operations_pass_the_stage(self, validator: SQLValidator) -> None:
        """UNION parses with a Union root, not a Select one."""
        result = validator.validate_static("SELECT id FROM orders UNION SELECT id FROM customers")

        assert result.valid


class TestIdentifierStage:
    def test_unknown_table_is_reported_with_a_suggestion(self, validator: SQLValidator) -> None:
        result = validator.validate_static("SELECT id FROM order")

        assert not result.valid
        assert result.stage_failed is ValidationStage.IDENTIFIERS
        assert result.error_type == "table_not_found"
        assert result.identifier == "order"
        assert result.suggestion == "orders"

    def test_unknown_column_is_reported_with_a_suggestion(self, validator: SQLValidator) -> None:
        result = validator.validate_static("SELECT total_amunt FROM orders")

        assert not result.valid
        assert result.error_type == "unknown_identifier"
        assert result.identifier == "total_amunt"
        assert result.suggestion == "total_amount"

    def test_qualified_unknown_column_names_its_table(self, validator: SQLValidator) -> None:
        result = validator.validate_static("SELECT o.revenu FROM orders o")

        assert not result.valid
        assert result.error_type == "unknown_identifier"
        assert 'table "orders"' in (result.message or "")

    def test_a_hopeless_name_gets_no_suggestion(self, validator: SQLValidator) -> None:
        """A wrong suggestion is worse than none -- the agent acts on it."""
        result = validator.validate_static("SELECT xyzzy FROM orders")

        assert not result.valid
        assert result.suggestion is None

    def test_feedback_line_carries_the_suggestion(self, validator: SQLValidator) -> None:
        result = validator.validate_static("SELECT total_amunt FROM orders")

        assert "Nearest match: total_amount" in result.feedback

    def test_join_across_two_known_tables_resolves(self, validator: SQLValidator) -> None:
        result = validator.validate_static(
            "SELECT c.name, o.total_amount FROM orders o JOIN customers c ON c.id = o.customer_id"
        )

        assert result.valid

    def test_output_aliases_are_not_treated_as_schema_columns(
        self, validator: SQLValidator
    ) -> None:
        """`ORDER BY revenue` references a name the query invented."""
        result = validator.validate_static(
            "SELECT sum(total_amount) AS revenue FROM orders ORDER BY revenue"
        )

        assert result.valid

    def test_cte_names_are_not_treated_as_missing_tables(self, validator: SQLValidator) -> None:
        result = validator.validate_static(
            "WITH per_country AS (SELECT country, count(*) AS n FROM customers GROUP BY country) "
            "SELECT country, n FROM per_country"
        )

        assert result.valid

    def test_derived_table_aliases_are_not_treated_as_missing_tables(
        self, validator: SQLValidator
    ) -> None:
        result = validator.validate_static("SELECT t.id FROM (SELECT id FROM orders) AS t")

        assert result.valid

    def test_star_is_not_an_identifier(self, validator: SQLValidator) -> None:
        result = validator.validate_static("SELECT * FROM orders")

        assert result.valid

    def test_identifiers_are_matched_case_insensitively(self, validator: SQLValidator) -> None:
        """PostgreSQL folds unquoted identifiers, so these name the same thing."""
        result = validator.validate_static("SELECT ID FROM Orders")

        assert result.valid

    def test_an_empty_catalog_skips_the_stage(self) -> None:
        """Better to defer to EXPLAIN than to reject everything when the
        catalog has not been indexed yet."""
        validator = SQLValidator(
            ExplodingConnection(),  # type: ignore[arg-type]
            SchemaCatalog({}),
            ExecutionSettings(),
        )
        result = validator.validate_static("SELECT anything FROM nowhere")

        assert result.valid
