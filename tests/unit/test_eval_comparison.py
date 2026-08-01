"""The seven comparison rules, one test each and then the awkward cases.

Every accuracy number this project publishes is `compare`'s opinion. A bug here
does not raise, it returns a *number*, and the number looks exactly like a
correct one — so this file is written as an executable copy of EVALUATION.md
section 1.1 rather than as coverage of a function.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from evals.comparison import (
    MAX_COLUMNS_FOR_PERMUTATION,
    Verdict,
    canonical,
    compare,
)

pytestmark = pytest.mark.unit


def matched(predicted: Any, gold: Any, **kwargs: Any) -> bool:
    return compare(predicted, gold, **kwargs).matched


class TestColumnOrderIsIgnored:
    def test_swapped_columns_still_match(self) -> None:
        assert matched([["a", 1]], [[1, "a"]]) is True

    def test_a_three_column_permutation_matches(self) -> None:
        assert matched([[3, 1, 2]], [[1, 2, 3]]) is True

    def test_column_names_are_never_consulted(self) -> None:
        """There is nothing here *but* values. Names cannot leak in, because
        the function is never given them -- which is the strongest form of
        "column names are ignored" available."""
        assert matched([[1, 2]], [[1, 2]]) is True


class TestRowOrder:
    def test_ignored_when_gold_is_unordered(self) -> None:
        assert matched([[2], [1]], [[1], [2]], gold_sql="SELECT x FROM t") is True

    def test_enforced_when_gold_has_order_by(self) -> None:
        assert matched([[2], [1]], [[1], [2]], gold_sql="SELECT x FROM t ORDER BY x") is False

    def test_the_reason_is_reported_as_an_order_mismatch(self) -> None:
        """Distinct from a value mismatch: it means a missing or reversed
        ORDER BY, which is a different bug from a wrong aggregate."""
        result = compare([[2], [1]], [[1], [2]], gold_sql="SELECT x FROM t ORDER BY x")

        assert result.verdict is Verdict.ORDER_MISMATCH

    def test_an_order_by_inside_a_cte_does_not_count(self) -> None:
        """It does not order what the caller receives. Enforcing on it would
        fail correct answers to questions that never asked for an order."""
        gold_sql = "WITH c AS (SELECT x FROM t ORDER BY x) SELECT x FROM c"

        assert matched([[2], [1]], [[1], [2]], gold_sql=gold_sql) is True

    def test_an_order_by_inside_a_subquery_does_not_count(self) -> None:
        gold_sql = "SELECT x FROM (SELECT x FROM t ORDER BY x) s"

        assert matched([[2], [1]], [[1], [2]], gold_sql=gold_sql) is True

    def test_an_unparseable_gold_query_does_not_enforce_order(self) -> None:
        """The lenient direction, deliberately. A gold query this project
        cannot parse is a defect in the benchmark or the parser, and it must
        not silently fail every question by imposing an unverified rule."""
        assert matched([[2], [1]], [[1], [2]], gold_sql="SELCT ??? FROM") is True

    def test_the_caller_can_state_it_directly(self) -> None:
        """A dataset that records orderedness per question should not have it
        re-derived by a parser that might disagree."""
        assert matched([[2], [1]], [[1], [2]], order_matters=True) is False

    def test_predicted_sql_is_never_consulted(self) -> None:
        """`compare` takes no predicted SQL at all. A query that produces the
        right rows in the right order has answered correctly whether or not it
        said ORDER BY to get there."""
        assert matched([[1], [2]], [[1], [2]], gold_sql="SELECT x FROM t ORDER BY x") is True


class TestDuplicatesAreSignificant:
    def test_an_extra_duplicate_row_fails(self) -> None:
        """A missing DISTINCT changes what a query means."""
        assert matched([[1], [1]], [[1]]) is False

    def test_multiplicity_must_match_exactly(self) -> None:
        assert matched([[1], [1], [2]], [[1], [2], [2]]) is False

    def test_matching_multiplicity_passes(self) -> None:
        assert matched([[1], [2], [1]], [[1], [1], [2]]) is True


class TestFloatTolerance:
    def test_drift_below_the_tolerance_matches(self) -> None:
        assert matched([[1.0000001]], [[1.0]]) is True

    def test_a_real_difference_does_not(self) -> None:
        assert matched([[1.01]], [[1.0]]) is False

    def test_decimal_and_int_are_the_same_number(self) -> None:
        """`SUM(x)` returns Decimal where `x` returns int, and no meaningful
        difference is being papered over."""
        assert matched([[Decimal("1.0")]], [[1]]) is True

    def test_negative_zero_equals_zero(self) -> None:
        assert matched([[-0.0]], [[0.0]]) is True

    def test_comparison_is_transitive(self) -> None:
        """The reason rounding is used rather than |a-b| < tol.

        With a tolerance, a≈b and b≈c without a≈c — and sorting and multiset
        comparison both require transitivity, so a non-transitive equality
        would make the verdict depend on the order rows arrived in. That is
        the one property a benchmark cannot have.
        """
        a, b, c = canonical(1.0000000), canonical(1.0000005), canonical(1.0000010)

        assert not (a == b and b == c and a != c)


class TestNullIsNotEmptyString:
    def test_they_do_not_match(self) -> None:
        assert matched([[None]], [[""]]) is False

    def test_null_matches_null(self) -> None:
        assert matched([[None]], [[None]]) is True

    def test_null_does_not_match_zero(self) -> None:
        assert matched([[None]], [[0]]) is False


class TestEmptyResults:
    def test_empty_matches_empty(self) -> None:
        assert matched([], []) is True

    def test_empty_does_not_match_a_populated_gold(self) -> None:
        """Otherwise a broken query returning nothing scores on every question
        whose answer happens to be nothing."""
        assert matched([], [[1]]) is False

    def test_a_populated_result_does_not_match_an_empty_gold(self) -> None:
        assert matched([[1]], []) is False


class TestTypesAreNotCoercedAcrossKinds:
    def test_a_number_and_its_string_are_different(self) -> None:
        """A query returning text where the reference returns a number has a
        real defect. The strict reading is the one that catches it."""
        assert matched([["1"]], [[1]]) is False

    def test_a_boolean_is_not_one(self) -> None:
        """`bool` is a subclass of `int`, so folding it into the numeric branch
        would make a boolean column match a numeric one."""
        assert matched([[True]], [[1]]) is False

    def test_dates_compare_by_value(self) -> None:
        assert matched([[dt.date(2026, 8, 1)]], [[dt.date(2026, 8, 1)]]) is True

    def test_different_dates_do_not(self) -> None:
        assert matched([[dt.date(2026, 8, 1)]], [[dt.date(2026, 8, 2)]]) is False


class TestShapeMismatches:
    def test_a_different_row_count_is_a_shape_mismatch(self) -> None:
        result = compare([[1]], [[1], [2]])

        assert result.verdict is Verdict.SHAPE_MISMATCH

    def test_a_different_column_count_never_matches(self) -> None:
        assert matched([[1, 2]], [[1]]) is False

    def test_ragged_rows_are_refused(self) -> None:
        result = compare([[1, 2], [3]], [[1, 2], [3, 4]])

        assert result.verdict is Verdict.SHAPE_MISMATCH


class TestTheColumnSearchIsBounded:
    def test_a_wide_result_falls_back_to_positional(self) -> None:
        """Above the cap, permutation search is skipped so one pathological
        result cannot turn a comparison into minutes of work."""
        width = MAX_COLUMNS_FOR_PERMUTATION + 1
        row = list(range(width))

        result = compare([row], [row])

        assert result.matched is True
        assert result.positional_fallback is True

    def test_the_fallback_is_reported(self) -> None:
        """It scores under a *stricter* rule than EVALUATION.md documents, so
        an aggregate that hid it would not be the metric it claims to be."""
        width = MAX_COLUMNS_FOR_PERMUTATION + 1
        row = list(range(width))
        shuffled = [row[-1], *row[:-1]]

        result = compare([shuffled], [row])

        assert result.matched is False
        assert result.positional_fallback is True

    def test_at_the_cap_permutation_still_applies(self) -> None:
        row = list(range(MAX_COLUMNS_FOR_PERMUTATION))
        shuffled = [row[-1], *row[:-1]]

        result = compare([shuffled], [row])

        assert result.matched is True
        assert result.positional_fallback is False

    def test_identical_columns_do_not_explode_the_search(self) -> None:
        """Eight identical columns is the worst case for permutation search,
        and the identity ordering matches immediately."""
        row = [7] * MAX_COLUMNS_FOR_PERMUTATION

        assert matched([row], [row]) is True


class TestCanonical:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            (1, 1.0),
            (Decimal("2.50"), 2.5),
            (True, ("bool", True)),
            ("text", "text"),
            (b"bytes", b"bytes"),
        ],
    )
    def test_values_reduce_predictably(self, value: Any, expected: Any) -> None:
        assert canonical(value) == expected

    def test_nan_does_not_compare_equal_to_itself_by_accident(self) -> None:
        """NaN != NaN in IEEE, which would make a NaN column never match
        itself. Canonicalising to a repr makes the comparison decidable —
        worth stating because it is a deliberate departure from float
        semantics."""
        assert canonical(float("nan")) == canonical(float("nan"))

    def test_everything_it_returns_is_hashable(self) -> None:
        """Multiset comparison requires it. A value that came back unhashable
        would fail deep inside a Counter with an unhelpful error."""
        for value in (None, 1, Decimal("1"), "x", b"x", dt.date(2026, 1, 1), [1, 2], {"a": 1}):
            hash(canonical(value))
