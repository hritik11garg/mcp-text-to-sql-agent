"""The parts of retrieval that need neither a database nor a model.

Argument validation and result ordering are separated from the ANN query for
exactly this reason: the rules that protect the query are the ones worth
testing exhaustively, and they are pure.
"""

from __future__ import annotations

import pytest

from schema.models import ForeignKey
from schema.retrieval import (
    MAX_TABLE_FILTER,
    RetrievalResult,
    _clamp,
    _clean_table_filter,
    _rank,
)

pytestmark = pytest.mark.unit


def row(table: str, column: str | None, score: float) -> tuple[object, ...]:
    """One row shaped exactly as the search statement returns it."""
    kind = "table" if column is None else "column"
    return (kind, table, column, "text", None, f"{table}.{column}", score)


class TestRanking:
    def test_orders_by_score_descending(self) -> None:
        elements = _rank([row("a", "x", 0.1), row("b", "y", 0.9), row("c", "z", 0.5)])
        assert [e.table for e in elements] == ["b", "c", "a"]

    def test_ties_break_deterministically(self) -> None:
        """Stable output keeps the prompt built from it stable, which is what
        makes prompt caching possible at all."""
        first = _rank([row("orders", "id", 0.5), row("customers", "id", 0.5)])
        second = _rank([row("customers", "id", 0.5), row("orders", "id", 0.5)])

        assert [e.qualified_name for e in first] == [e.qualified_name for e in second]
        assert [e.qualified_name for e in first] == ["customers.id", "orders.id"]

    def test_table_elements_have_no_column(self) -> None:
        (element,) = _rank([row("orders", None, 0.7)])

        assert element.element_type == "table"
        assert element.column is None
        assert element.qualified_name == "orders"

    def test_empty_input_gives_empty_output(self) -> None:
        assert _rank([]) == ()


class TestRetrievalResult:
    def test_tables_are_distinct_and_in_rank_order(self) -> None:
        result = RetrievalResult(
            elements=_rank(
                [row("orders", "total", 0.9), row("customers", "id", 0.8), row("orders", "id", 0.7)]
            )
        )
        assert result.tables == ("orders", "customers")

    def test_empty_result_is_usable(self) -> None:
        """A retrieval miss is a normal outcome, not an error state."""
        result = RetrievalResult()

        assert result.elements == ()
        assert result.foreign_keys == ()
        assert result.tables == ()

    def test_carries_foreign_keys(self) -> None:
        edge = ForeignKey("orders", "customer_id", "customers", "id")
        assert RetrievalResult(foreign_keys=(edge,)).foreign_keys == (edge,)


class TestTableFilter:
    def test_none_means_no_restriction(self) -> None:
        assert _clean_table_filter(None) is None

    def test_names_are_stripped_and_deduplicated(self) -> None:
        assert _clean_table_filter([" orders ", "orders", "customers"]) == ("orders", "customers")

    def test_empty_sequence_is_rejected(self) -> None:
        """Not treated as "no filter".

        Reading an empty restriction as an absent one widens a limit the caller
        asked for, and widening on ambiguous input is how filters stop being
        filters.
        """
        with pytest.raises(ValueError, match="non-empty table names"):
            _clean_table_filter([])

    def test_blank_entries_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty table names"):
            _clean_table_filter(["orders", "   "])

    def test_oversized_filter_is_rejected(self) -> None:
        names = [f"t{i}" for i in range(MAX_TABLE_FILTER + 1)]
        with pytest.raises(ValueError, match="at most"):
            _clean_table_filter(names)

    def test_filter_at_the_limit_is_accepted(self) -> None:
        names = [f"t{i}" for i in range(MAX_TABLE_FILTER)]
        assert len(_clean_table_filter(names) or ()) == MAX_TABLE_FILTER


class TestClamp:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(-5, 1), (0, 1), (1, 1), (25, 25), (50, 50), (51, 50), (10_000, 50)],
    )
    def test_bounds_are_inclusive(self, value: int, expected: int) -> None:
        assert _clamp(value, 1, 50) == expected
