"""Why a question failed, in categories that point at different fixes.

An aggregate score says how much is broken. It says nothing about *what*, and
the two failures that most often share a score need opposite responses: a
retrieval miss is fixed by the Stage 5 fine-tune, a wrong literal format by
profiling. Reporting them together makes both ablations unreadable.

The categories are EVALUATION.md section 5, with one refinement that section
now carries: ``filter_error`` is split. A wrong *predicate* is a reasoning
failure; a wrong *literal format* -- ``'Finland'`` against a column holding
``'FI'`` -- is an information failure, and it is the one `profile_table` exists
to fix. Left merged, the profiling ablation cannot be read.

**Gold errors are a category, not a discard.** Benchmarks contain reference
queries that are simply wrong, especially BIRD. They cap achievable accuracy,
and dropping them quietly inflates every score by however many there were.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from evals.comparison import Comparison, Verdict
from evals.recall import RecallResult

logger = logging.getLogger(__name__)


class FailureCategory(StrEnum):
    """One reason a question did not score.

    ``UNCATEGORISED`` exists on purpose. A taxonomy that always finds a bucket
    is not classifying, it is guessing, and a count of failures nobody could
    explain is a more useful thing to report than a confident wrong label.
    """

    NONE = "none"
    RETRIEVAL_MISS = "retrieval_miss"
    UNKNOWN_IDENTIFIER = "unknown_identifier"
    SYNTAX_UNRECOVERABLE = "syntax_unrecoverable"
    NOT_READ_ONLY = "not_read_only"
    TIMEOUT = "timeout"
    ROW_ORDER = "row_order"
    WRONG_SHAPE = "wrong_shape"
    WRONG_VALUES = "wrong_values"
    GOLD_ERROR = "gold_error"
    UNANSWERABLE = "unanswerable"
    UNCATEGORISED = "uncategorised"


def classify(
    *,
    comparison: Comparison | None,
    recall: RecallResult | None = None,
    error_type: str | None = None,
    gold_failed: bool = False,
    recall_floor: float = 1.0,
) -> FailureCategory:
    """Assign one category, most-specific cause first.

    Ordering is the whole design. A question can be several of these at once --
    a retrieval miss usually *also* produces an unknown identifier, and a
    timeout usually also produces no result to compare. Classifying by the
    earliest cause is what makes the counts add up to something actionable:
    fixing retrieval removes the retrieval misses and the unknown identifiers
    they caused, and a taxonomy that counted the symptom would over-report the
    remaining work.

    Args:
        recall_floor: Recall@k below which the failure is attributed to
            retrieval. Defaults to 1.0 -- if any needed element was missing,
            the model was never shown what it needed, whatever else went wrong
            afterwards.
    """
    if gold_failed:
        # First, unconditionally. Nothing about the system under test can be
        # concluded from a question whose reference answer does not run.
        return FailureCategory.GOLD_ERROR

    if error_type == "unanswerable":
        return FailureCategory.UNANSWERABLE

    if recall is not None and not recall.skipped and recall.at_k:
        achieved = max(recall.at_k.values())
        if achieved < recall_floor:
            return FailureCategory.RETRIEVAL_MISS

    if error_type:
        return _FROM_ERROR_TYPE.get(error_type, FailureCategory.UNCATEGORISED)

    if comparison is None:
        return FailureCategory.UNCATEGORISED
    if comparison.matched:
        return FailureCategory.NONE

    return _FROM_VERDICT.get(comparison.verdict, FailureCategory.UNCATEGORISED)


_FROM_ERROR_TYPE: dict[str, FailureCategory] = {
    "unknown_identifier": FailureCategory.UNKNOWN_IDENTIFIER,
    "syntax_error": FailureCategory.SYNTAX_UNRECOVERABLE,
    "multiple_statements": FailureCategory.SYNTAX_UNRECOVERABLE,
    "explain_failed": FailureCategory.SYNTAX_UNRECOVERABLE,
    "not_read_only": FailureCategory.NOT_READ_ONLY,
    "statement_timeout": FailureCategory.TIMEOUT,
}

_FROM_VERDICT: dict[Verdict, FailureCategory] = {
    Verdict.ORDER_MISMATCH: FailureCategory.ROW_ORDER,
    Verdict.SHAPE_MISMATCH: FailureCategory.WRONG_SHAPE,
    Verdict.VALUE_MISMATCH: FailureCategory.WRONG_VALUES,
    Verdict.NO_COLUMN_BIJECTION: FailureCategory.WRONG_VALUES,
}


def parse_category(value: str) -> FailureCategory:
    """Read a category back from an artifact, tolerating anything unfamiliar.

    A resumable harness reads artifacts that a *different version of itself*
    wrote, so a category string this build has never heard of is a routine
    occurrence rather than corruption. Mapping it to ``UNCATEGORISED`` keeps
    the question in the totals; raising would make a summary impossible to
    produce for a run that is otherwise complete -- which is the one moment
    the whole resumption design exists to protect.
    """
    try:
        return FailureCategory(value)
    except ValueError:
        logger.warning("unrecognised failure category %r, counting as uncategorised", value)
        return FailureCategory.UNCATEGORISED


def counts(categories: list[FailureCategory]) -> dict[str, int]:
    """Category counts, with every category present even at zero.

    Absent keys and zero counts read identically in a report, and they are not
    the same claim: one says nothing failed that way, the other says nobody
    looked.
    """
    tally = dict.fromkeys((c.value for c in FailureCategory), 0)
    for category in categories:
        tally[category.value] += 1
    return tally


__all__ = ["FailureCategory", "classify", "counts", "parse_category"]
