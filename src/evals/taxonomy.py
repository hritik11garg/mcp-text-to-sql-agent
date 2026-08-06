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

    EXECUTION_FAILED = "execution_failed"
    """The generated query parsed but the database refused to run it.

    A model failure, and the one the invalid-query-rate metric counts. It had no
    category until the first real run put 12 of 150 questions into
    ``UNCATEGORISED`` -- a bucket named after the unexplained, collecting a
    failure whose cause the runner already knew and had written down.
    """

    INFRASTRUCTURE = "infrastructure"
    """The system under test never got to answer.

    A provider outage, a spent rate limit, a database that was never indexed, an
    internal error in the harness. **Excluded from the scored denominator**, for
    the reason :attr:`GOLD_ERROR` is: nothing about the model can be concluded
    from a question it was never asked. Counting these as wrong answers means a
    ten-minute rate limit is reported as a model that got worse.
    """

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

    if error_type in _INFRASTRUCTURE:
        # Before the recall branch, and that ordering is load-bearing. A
        # database that was never indexed retrieves nothing, so its Recall@k is
        # 0 and the branch below would file it as a retrieval miss -- reporting
        # a missing catalog as a failing retriever, which is the one thing that
        # taxonomy is used to decide. Same argument as `gold_failed`: nothing
        # about the system under test can be concluded from a question it was
        # never asked.
        return FailureCategory.INFRASTRUCTURE

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


def is_infrastructure(error_type: str | None) -> bool:
    """Whether this failure means the system under test was never asked.

    One predicate behind two decisions that have to agree: which questions
    leave the scored denominator, and which questions a resumed run must
    *retry*. They are the same claim -- nothing was learned about the model --
    so deriving both from one set is what stops them drifting apart.

    They were not derived from one set. Scoring excluded these; resumption did
    not, so a run that spent its daily token budget recorded 308 questions as
    permanently answered, having asked none of them. See
    :meth:`evals.artifacts.RunStore.resume`.
    """
    return error_type in _INFRASTRUCTURE


_FROM_ERROR_TYPE: dict[str, FailureCategory] = {
    "unknown_identifier": FailureCategory.UNKNOWN_IDENTIFIER,
    "syntax_error": FailureCategory.SYNTAX_UNRECOVERABLE,
    "multiple_statements": FailureCategory.SYNTAX_UNRECOVERABLE,
    "explain_failed": FailureCategory.SYNTAX_UNRECOVERABLE,
    "not_read_only": FailureCategory.NOT_READ_ONLY,
    "statement_timeout": FailureCategory.TIMEOUT,
    "permission_denied": FailureCategory.NOT_READ_ONLY,
    "table_not_found": FailureCategory.UNKNOWN_IDENTIFIER,
    "cost_exceeded": FailureCategory.TIMEOUT,
    # Everything the runner and the pipeline emit. Each one existed and none of
    # them were mapped, so every one landed in UNCATEGORISED -- which is how a
    # bucket meant for "nobody could explain this" fills with failures the code
    # had already explained one layer up.
    "execution_failed": FailureCategory.EXECUTION_FAILED,
    "llm_failed": FailureCategory.INFRASTRUCTURE,
    "scope_unavailable": FailureCategory.INFRASTRUCTURE,
    "retrieval_failed": FailureCategory.INFRASTRUCTURE,
    "internal_error": FailureCategory.INFRASTRUCTURE,
}
"""Every ``error_type`` any component produces, mapped to a cause.

Kept exhaustive on purpose, and there is a test that walks the codebase's
emitters to prove it stays that way. An unmapped type does not raise -- it
becomes ``UNCATEGORISED``, which reads in a report as "a failure nobody could
explain" and is the most expensive kind of quiet wrongness this taxonomy can
produce.
"""

_INFRASTRUCTURE = frozenset(
    {"llm_failed", "scope_unavailable", "retrieval_failed", "internal_error"}
)
"""Error types meaning the system under test never got to answer.

Consulted *before* recall, and the set is separate from
:data:`_FROM_ERROR_TYPE` only so that ordering can be expressed. Both map to
:attr:`FailureCategory.INFRASTRUCTURE`; a test asserts they agree.
"""


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
