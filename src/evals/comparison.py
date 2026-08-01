"""Does this result set mean the same thing as the gold one?

Every accuracy number this project will ever publish is this function's opinion,
which makes it the highest-stakes pure logic in the codebase: a bug here does
not produce an error, it produces a *number*, and the number looks fine.

The rules come from docs/ml/EVALUATION.md section 1.1, and each is a judgement
call that moves the score:

===================  ==========================================================
Column order         Ignored -- semantically irrelevant
Column names         Ignored -- aliasing is stylistic
Row order            Ignored **unless the gold query has ORDER BY**
Duplicate rows       Significant -- ``DISTINCT`` changes meaning
Floats               Equal within 1e-6
NULL vs empty string Distinct -- they mean different things
Empty result         Matches only an empty gold result
===================  ==========================================================

Ignoring both column order and column names has a consequence the table does
not spell out: **there is nothing left to match columns by except their
contents.** So comparison is a search for a bijection between predicted and
gold columns, not a positional zip. That is the bulk of the code here, and
:func:`compare` documents the bound placed on that search.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import permutations
from typing import Any

import sqlglot

FLOAT_TOLERANCE = 1e-6
"""From EVALUATION.md section 1.1. Aggregation ordering causes drift well below
this, and nothing this benchmark asks for is sensitive at that scale."""

FLOAT_PLACES = 6
"""Decimal places floats are rounded to for hashing and sorting.

Deliberately consistent with :data:`FLOAT_TOLERANCE`, and deliberately *not*
the same operation -- see :func:`canonical` for why an equality that is not
transitive cannot be used here at all.
"""

MAX_COLUMNS_FOR_PERMUTATION = 8
"""Above this width, columns are matched positionally instead of searched.

8! is 40,320 candidate orderings, which is already generous for a benchmark
query. The cap exists so a pathological wide result cannot turn one comparison
into minutes of work, and :class:`Comparison` reports when it applied so a
score is never quietly computed under a stricter rule than the one documented.
"""


class Verdict(StrEnum):
    """Why two result sets did or did not match.

    A bare boolean would make every failure look alike, and the failure
    taxonomy in EVALUATION.md section 5 needs to tell "wrong number of rows"
    from "right rows, wrong values" -- those point at different bugs.
    """

    MATCH = "match"
    SHAPE_MISMATCH = "shape_mismatch"
    VALUE_MISMATCH = "value_mismatch"
    ORDER_MISMATCH = "order_mismatch"
    NO_COLUMN_BIJECTION = "no_column_bijection"


@dataclass(frozen=True, slots=True)
class Comparison:
    """The outcome, with enough detail to debug a wrong score."""

    verdict: Verdict
    matched: bool
    detail: str = ""
    order_enforced: bool = False
    """Whether row order was required, i.e. the gold query had ``ORDER BY``."""

    positional_fallback: bool = False
    """Whether the column-permutation search was skipped for width.

    Reported rather than silent: a run with this set on some questions scored
    those questions under a stricter rule than EVALUATION.md documents, and an
    aggregate that hides it is not the metric it claims to be.
    """


def compare(
    predicted: Sequence[Sequence[Any]],
    gold: Sequence[Sequence[Any]],
    *,
    gold_sql: str | None = None,
    order_matters: bool | None = None,
) -> Comparison:
    """Compare two result sets under the EVALUATION.md section 1.1 rules.

    Args:
        predicted: Rows from the generated query.
        gold: Rows from the reference query.
        gold_sql: The reference SQL. Used only to decide whether row order is
            significant, by looking for a top-level ``ORDER BY``.
        order_matters: Overrides the ``gold_sql`` inspection. Pass it when the
            caller already knows -- a dataset that records orderedness per
            question should not have it re-derived by a parser.

    A note on what is *not* done: predicted SQL is never inspected. The
    question is whether the answer is right, and a query that produces the
    correct rows in the correct order has answered an ordered question
    correctly whether or not it said ``ORDER BY`` to get there.
    """
    enforced = _order_matters(gold_sql) if order_matters is None else order_matters

    if len(predicted) != len(gold):
        return Comparison(
            Verdict.SHAPE_MISMATCH,
            matched=False,
            detail=f"{len(predicted)} rows, expected {len(gold)}",
            order_enforced=enforced,
        )

    if not gold:
        # Both empty. EVALUATION.md is explicit that an empty result matches
        # only an empty gold result -- the length check above already enforced
        # the other direction, which is what stops a query returning nothing
        # from scoring on every question whose answer is nothing.
        return Comparison(Verdict.MATCH, matched=True, order_enforced=enforced)

    width = len(gold[0])
    if any(len(row) != width for row in gold) or any(len(row) != width for row in predicted):
        return Comparison(
            Verdict.SHAPE_MISMATCH,
            matched=False,
            detail="ragged rows",
            order_enforced=enforced,
        )

    pred_columns = _columns(predicted, width)
    gold_columns = _columns(gold, width)

    if width > MAX_COLUMNS_FOR_PERMUTATION:
        matched = _rows_equal(pred_columns, gold_columns, ordered=enforced)
        return Comparison(
            Verdict.MATCH if matched else Verdict.VALUE_MISMATCH,
            matched=matched,
            detail="" if matched else f"no positional match across {width} columns",
            order_enforced=enforced,
            positional_fallback=True,
        )

    for candidate in _candidate_orderings(pred_columns, gold_columns, ordered=enforced):
        if _rows_equal(candidate, gold_columns, ordered=enforced):
            return Comparison(Verdict.MATCH, matched=True, order_enforced=enforced)

    return Comparison(
        _diagnose(pred_columns, gold_columns, enforced),
        matched=False,
        detail=_explain(pred_columns, gold_columns, enforced),
        order_enforced=enforced,
    )


def _columns(rows: Sequence[Sequence[Any]], width: int) -> tuple[tuple[Any, ...], ...]:
    """Transpose to columns, canonicalising every value on the way.

    Column-major because every operation here is per column: matching them by
    content, permuting them, comparing their multisets.
    """
    return tuple(tuple(canonical(row[i]) for row in rows) for i in range(width))


def _candidate_orderings(
    pred_columns: tuple[tuple[Any, ...], ...],
    gold_columns: tuple[tuple[Any, ...], ...],
    *,
    ordered: bool,
) -> Iterable[tuple[tuple[Any, ...], ...]]:
    """Predicted-column orderings worth testing, cheapest first.

    The identity ordering comes first because it is overwhelmingly the common
    case -- a correct query usually selects columns in the order the question
    implies -- and returning it immediately keeps the normal path free of any
    permutation work at all.

    After that, the search is pruned by content: a predicted column can only
    stand in for a gold column whose *multiset of values* it matches, so
    columns are grouped by that signature and only consistent assignments are
    generated. In practice this collapses to a single candidate unless two
    columns genuinely hold the same values, in which case swapping them cannot
    change the answer anyway.
    """
    yield pred_columns

    signature = _signature_ordered if ordered else _signature_unordered
    gold_signatures = [signature(column) for column in gold_columns]
    pred_signatures = [signature(column) for column in pred_columns]

    if Counter(gold_signatures) != Counter(pred_signatures):
        # No bijection can exist, so there is nothing to search. This is the
        # early exit that makes a wrong answer cheap to reject.
        return

    for order in permutations(range(len(pred_columns))):
        if all(pred_signatures[order[i]] == gold_signatures[i] for i in range(len(order))):
            yield tuple(pred_columns[i] for i in order)


def _signature_unordered(column: Sequence[Any]) -> tuple[tuple[Any, int], ...]:
    """A column's contents, ignoring row order. Hashable, so it can be counted."""
    return tuple(sorted(Counter(column).items(), key=lambda item: repr(item[0])))


def _signature_ordered(column: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(column)


def _rows_equal(
    pred_columns: Sequence[Sequence[Any]],
    gold_columns: Sequence[Sequence[Any]],
    *,
    ordered: bool,
) -> bool:
    """Compare row-wise once the columns are aligned.

    Duplicates are significant, so this is a multiset comparison rather than a
    set one -- ``DISTINCT`` changes what a query means and a comparison that
    ignored it would score a missing ``DISTINCT`` as correct.
    """
    pred_rows = list(zip(*pred_columns, strict=True))
    gold_rows = list(zip(*gold_columns, strict=True))

    if ordered:
        return pred_rows == gold_rows
    return Counter(pred_rows) == Counter(gold_rows)


def _diagnose(
    pred_columns: tuple[tuple[Any, ...], ...],
    gold_columns: tuple[tuple[Any, ...], ...],
    ordered: bool,
) -> Verdict:
    """Separate "wrong values" from "right values, wrong order".

    Worth the extra check: an order mismatch on an ordered question is usually
    a missing or reversed ``ORDER BY``, which is a different bug from a wrong
    aggregate and belongs in a different row of the failure taxonomy.
    """
    if ordered and _rows_equal(pred_columns, gold_columns, ordered=False):
        return Verdict.ORDER_MISMATCH

    unordered_pred = Counter(_signature_unordered(c) for c in pred_columns)
    unordered_gold = Counter(_signature_unordered(c) for c in gold_columns)
    if unordered_pred != unordered_gold:
        return Verdict.NO_COLUMN_BIJECTION
    return Verdict.VALUE_MISMATCH


def _explain(
    pred_columns: tuple[tuple[Any, ...], ...],
    gold_columns: tuple[tuple[Any, ...], ...],
    ordered: bool,
) -> str:
    if ordered and _rows_equal(pred_columns, gold_columns, ordered=False):
        return "same rows in a different order, and the gold query is ordered"
    return "no assignment of predicted columns to gold columns matches"


def canonical(value: Any) -> Any:
    """Reduce a driver value to something comparable, hashable and sortable.

    **Numeric types are unified; strings are not.** ``int``, ``float`` and
    ``Decimal`` all become a rounded float, because ``SUM(x)`` returns a
    ``Decimal`` where ``x`` returns an ``int`` and no meaningful difference is
    being papered over. ``'1'`` stays a string and therefore never equals
    ``1`` -- a query that returns text where the reference returns a number has
    a real defect, and the strict reading is the one that catches it.

    **Rounding, not a tolerance.** EVALUATION.md specifies equality within
    1e-6, and a tolerance-based ``==`` is *not transitive*: with a=1.0000000,
    b=1.0000005 and c=1.0000010, a≈b and b≈c but a≉c. Sorting and multiset
    comparison both require transitivity, so a non-transitive equality would
    make the result depend on the order values happened to arrive in -- which
    is exactly the property a benchmark cannot have. Rounding to
    :data:`FLOAT_PLACES` is transitive by construction. It differs from a true
    tolerance only for pairs straddling a rounding boundary, where it is the
    stricter of the two, and being stricter is the safe direction for a
    correctness metric.

    ``None`` is preserved. EVALUATION.md is explicit that NULL and the empty
    string are distinct, and the coercion that erases that is the easy one to
    write by accident.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # Tagged, not returned as-is. `bool` is a subclass of `int` *and*
        # `True == 1.0` is true in Python, so simply skipping the numeric
        # branch is not enough -- an untagged `True` would still compare and
        # hash equal to `1.0` and a boolean column would match a numeric one.
        #
        # EVALUATION.md section 1.1 does not cover this case; the rule chosen
        # is the one consistent with `'1' != 1`, and it errs strict, which
        # understates accuracy rather than inflating it.
        return ("bool", value)
    if isinstance(value, Decimal | float | int):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return repr(number)
        return round(number, FLOAT_PLACES) + 0.0  # +0.0 normalises -0.0
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    if isinstance(value, list | tuple):
        return tuple(canonical(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((str(k), canonical(v)) for k, v in value.items()))
    return value


def _order_matters(gold_sql: str | None) -> bool:
    """Whether the reference query asked for a particular row order.

    A parse failure means *no* ordering is enforced. That is the lenient
    direction, and it is the right one: a gold query this project cannot parse
    is a defect in the benchmark or the parser, and it should not silently
    fail every question by imposing an order requirement nobody verified.
    """
    if not gold_sql:
        return False
    try:
        statement = sqlglot.parse_one(gold_sql, read="postgres")
    except Exception:
        return False

    # Only the outermost ORDER BY counts, so this reads `args` on the root
    # rather than walking the tree. Walking would find an ordering inside a CTE
    # or a subquery -- neither of which orders what the caller receives -- and
    # would then enforce row order on questions the benchmark never asked to be
    # ordered, failing correct answers.
    #
    # A `UNION` carries its ORDER BY on the union node itself, so the root
    # check covers that case too without special handling.
    return statement.args.get("order") is not None


__all__ = [
    "FLOAT_PLACES",
    "FLOAT_TOLERANCE",
    "MAX_COLUMNS_FOR_PERMUTATION",
    "Comparison",
    "Verdict",
    "canonical",
    "compare",
]
