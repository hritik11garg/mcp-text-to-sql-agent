"""AST-level row limiting, which is pure and therefore worth testing hard.

The limit is the difference between a question that returns a page and a
question that returns a table, so every branch of "whose limit wins" has a
case here.
"""

from __future__ import annotations

import pytest

from execution.executor import apply_row_limit

pytestmark = pytest.mark.unit

CEILING = 500


class TestWhoseLimitWins:
    def test_no_limit_takes_the_ceiling(self) -> None:
        sql, effective, capped = apply_row_limit("SELECT id FROM orders", CEILING)

        assert effective == CEILING
        assert capped
        assert sql.endswith("LIMIT 501")

    def test_a_smaller_caller_limit_is_honoured(self) -> None:
        """The caller asked for ten. Ten is what the query should return."""
        _, effective, capped = apply_row_limit("SELECT id FROM orders LIMIT 10", CEILING)

        assert effective == 10
        assert not capped

    def test_a_larger_caller_limit_is_clamped(self) -> None:
        _, effective, capped = apply_row_limit("SELECT id FROM orders LIMIT 10000", CEILING)

        assert effective == CEILING
        assert capped

    def test_a_limit_equal_to_the_ceiling_is_not_a_cap(self) -> None:
        """Boundary: reaching a limit you asked for is not truncation."""
        _, effective, capped = apply_row_limit("SELECT id FROM orders LIMIT 500", CEILING)

        assert effective == CEILING
        assert not capped

    def test_one_extra_row_is_always_requested(self) -> None:
        """So a full page and a truncated page can be told apart without a
        second query."""
        sql, effective, _ = apply_row_limit("SELECT id FROM orders LIMIT 7", CEILING)

        assert f"LIMIT {effective + 1}" in sql


class TestStructuresThatBreakStringAppending:
    """Every case here is one where `sql + " LIMIT n"` produces wrong SQL."""

    def test_union_is_limited_as_a_whole(self) -> None:
        sql, _, _ = apply_row_limit("SELECT a FROM t UNION SELECT b FROM u", CEILING)

        assert sql.count("LIMIT") == 1
        assert sql.strip().endswith("LIMIT 501")

    def test_an_existing_limit_is_replaced_not_appended(self) -> None:
        sql, _, _ = apply_row_limit("SELECT id FROM orders LIMIT 10", CEILING)

        assert sql.count("LIMIT") == 1

    def test_offset_survives(self) -> None:
        sql, effective, _ = apply_row_limit(
            "SELECT id FROM orders ORDER BY id LIMIT 5 OFFSET 20", CEILING
        )

        assert effective == 5
        assert "OFFSET 20" in sql

    def test_trailing_semicolon_does_not_produce_broken_sql(self) -> None:
        sql, _, _ = apply_row_limit("SELECT id FROM orders;", CEILING)

        assert ";" not in sql
        assert sql.endswith("LIMIT 501")

    def test_a_limit_inside_a_cte_is_left_alone(self) -> None:
        """The inner LIMIT is part of the query's meaning, not its result size."""
        sql, effective, _ = apply_row_limit(
            "WITH c AS (SELECT id FROM orders LIMIT 3) SELECT id FROM c", CEILING
        )

        assert "LIMIT 3" in sql
        assert effective == CEILING
        assert sql.strip().endswith("LIMIT 501")


class TestHostileLimits:
    def test_a_non_literal_limit_falls_back_to_the_ceiling(self) -> None:
        """An expression cannot be evaluated statically.

        Treating an unreadable limit as absent is the safe direction: assuming
        a value is the one way to end up applying a limit *larger* than the
        ceiling.
        """
        _, effective, capped = apply_row_limit("SELECT id FROM orders LIMIT (SELECT 9999)", CEILING)

        assert effective == CEILING
        assert capped

    def test_zero_is_honoured(self) -> None:
        _, effective, capped = apply_row_limit("SELECT id FROM orders LIMIT 0", CEILING)

        assert effective == 0
        assert not capped

    @pytest.mark.parametrize("ceiling", [1, 2, 10, 5_000])
    def test_the_ceiling_is_never_exceeded(self, ceiling: int) -> None:
        _, effective, _ = apply_row_limit("SELECT id FROM orders LIMIT 999999", ceiling)

        assert effective <= ceiling
