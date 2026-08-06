"""Orchestration: ask, execute, compare, classify, record, and survive a stop.

The runner owns none of the pipeline. It takes an *answerer* -- anything that
turns a question into candidate SQL -- and a *query runner* that executes SQL.
Two consequences, both deliberate:

**The whole harness is testable without a model or a database.** A scripted
answerer and an in-memory query runner exercise every branch here, which is
what lets the comparison and taxonomy logic be trusted before a single token is
spent on them.

**Baselines are configurations of the answerer, not flags in the runner.**
EVALUATION.md section 4 lists five: no-retrieval, retrieval-only, with
validation, with self-correction, fine-tuned retriever. Each is a different
answerer over the same orchestration, so adding one is not a change here.

One rule that is not negotiable and is easy to get wrong: **gold and predicted
SQL are executed through the same query runner.** Running them through
different connections, drivers or type adapters produces `Decimal('1')` on one
side and `1.0` on the other, and the comparison reports a value mismatch for a
correct answer. Identical machinery is what makes a difference in the *result*
the only thing a difference in the comparison can mean.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from evals.artifacts import QuestionArtifact, RunStore
from evals.comparison import Comparison, compare
from evals.dataset import Question
from evals.recall import compute_recall, extract_gold_elements
from evals.taxonomy import (
    FailureCategory,
    classify,
    counts,
    is_infrastructure,
    parse_category,
)

DEFAULT_HALT_AFTER = 10
"""Consecutive infrastructure failures that stop a run.

A spent daily token budget does not recover within the run, so continuing
means asking a dead provider several hundred more times: it costs wall clock,
it buries the cause under identical records, and the summary written at the
end describes a directory that is mostly noise. The first full-split attempt
did this 308 times.

Consecutive rather than total, because a transient blip that the provider
recovers from must not stop a run -- and by the time an ``llm_failed`` reaches
here the client's own retries are already exhausted, so ten in a row is a wall
rather than a bad minute.

It is deliberately low enough to trip on a whole database failing as
``scope_unavailable``. That is a deployment fault, and stopping to report it
beats scoring around it -- which is what the previous run did with 84
questions.
"""

logger = logging.getLogger(__name__)

Rows = list[list[Any]]

_UNSCORED = frozenset({FailureCategory.GOLD_ERROR, FailureCategory.INFRASTRUCTURE})
"""Categories that leave the denominator. See :meth:`EvalRunner._summarise`."""


@dataclass(frozen=True, slots=True)
class Attempt:
    """What an answerer produced for one question."""

    sql: str | None = None
    error_type: str | None = None
    error_message: str = ""
    validation_attempts: int = 0
    retrieved: tuple[tuple[str, str | None], ...] = ()
    """``(table, column)`` in **rank order**. Recall is meaningless otherwise."""

    input_tokens: int = 0
    output_tokens: int = 0
    answering_model: str = ""


class Answerer(Protocol):
    """Anything that turns a question into candidate SQL.

    The seam every baseline varies at. It returns an :class:`Attempt` rather
    than raising, because a failure to produce SQL is a *result* for a
    benchmark -- it belongs in the failure taxonomy, not in a traceback that
    ends the run.
    """

    def __call__(self, question: Question) -> Attempt: ...


class QueryRunner(Protocol):
    """Executes SQL and returns rows. Used for gold and predicted alike."""

    def __call__(self, sql: str, *, db_id: str = "") -> Rows: ...


@dataclass(frozen=True, slots=True)
class RunSummary:
    """The aggregate, and enough context that it cannot be read as more than it is."""

    total: int = 0
    scored: int = 0
    matched: int = 0
    gold_errors: int = 0
    infrastructure_errors: int = 0

    answered_by: dict[str, int] = field(default_factory=dict)
    """Questions answered, per model, and it belongs beside the score.

    A fallback chain switches models when a free-tier limit is hit, so a run can
    silently be a blend. Measured: one run was 100% `gpt-oss-120b` at 73%, and
    the next -- same code, same k -- fell back for 27 questions to a model that
    scored 0%, reporting 64.7% overall. Neither number is a model score, and
    nothing in the summary said so until this field existed.
    """

    failures: dict[str, int] = field(default_factory=dict)
    recall: dict[str, float | int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0.0

    @property
    def single_model(self) -> bool:
        """Whether one model answered every question that reached one.

        False means the accuracy figure is a weighted average of two systems.
        Reported rather than corrected: which model answered is a property of
        the provider's rate limiter, not something the harness should pretend
        away by discarding questions.
        """
        return len(self.answered_by) <= 1

    @property
    def execution_accuracy(self) -> float | None:
        """Matched over *scored*, where scored excludes what was never asked.

        ``None`` rather than ``0.0`` when nothing scored. A zero would enter a
        BENCHMARKS table looking like a measured result, and "the harness ran
        and everything failed" is a different finding from "nothing ran".
        """
        if not self.scored:
            return None
        return round(self.matched / self.scored, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "scored": self.scored,
            "matched": self.matched,
            "gold_errors": self.gold_errors,
            "infrastructure_errors": self.infrastructure_errors,
            "execution_accuracy": self.execution_accuracy,
            "answered_by": self.answered_by,
            "single_model": self.single_model,
            "failures": self.failures,
            "recall": self.recall,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "duration_ms": round(self.duration_ms, 1),
        }


class EvalRunner:
    """Runs a split, one question at a time, resuming what is already done.

    Args:
        store: Where artifacts land. Its manifest also decides whether a
            partially completed run may be continued.
        answerer: The pipeline under test.
        run_query: Executes SQL. **The same one is used for gold.**
        on_progress: Called after each question, for a progress line. Kept as a
            callback so the runner writes nothing to stdout itself.
        halt_after: Consecutive infrastructure failures that stop the run. See
            :data:`DEFAULT_HALT_AFTER`. ``None`` disables the check.
    """

    def __init__(
        self,
        store: RunStore,
        answerer: Answerer,
        run_query: QueryRunner,
        *,
        on_progress: Callable[[QuestionArtifact], None] | None = None,
        halt_after: int | None = DEFAULT_HALT_AFTER,
    ) -> None:
        self._store = store
        self._answerer = answerer
        self._run_query = run_query
        self._on_progress = on_progress
        if halt_after is not None and halt_after < 1:
            # Not clamped to 1 and not silently treated as "off". Zero means
            # two opposite things to two readers -- "never halt" and "halt
            # immediately" -- and the loop's comparison would pick the second,
            # ending every run at question one with a summary over nothing.
            raise ValueError(f"halt_after must be at least 1 or None, got {halt_after}")
        self._halt_after = halt_after

    def run(self, questions: Sequence[Question]) -> RunSummary:
        """Evaluate every question not already recorded.

        Raises:
            ValueError: the run directory holds a different configuration.
                Raised by the store before any question is attempted, so a
                mismatched resume costs nothing.
        """
        self._store.start()
        done = self._store.resume()
        pending = [q for q in questions if q.question_id not in done]

        if done:
            logger.info("skipping %d already-answered question(s)", len(questions) - len(pending))

        consecutive = 0
        for position, question in enumerate(pending):
            artifact = self._evaluate(question)
            self._store.record(artifact)
            if self._on_progress is not None:
                self._on_progress(artifact)

            consecutive = consecutive + 1 if is_infrastructure(artifact.error_type) else 0
            if self._halt_after is not None and consecutive >= self._halt_after:
                logger.error(
                    "halting: %d consecutive infrastructure failures, last was %s (%s). "
                    "The remaining %d question(s) are untouched and these %d will be "
                    "re-attempted -- resume this run id once the cause is cleared",
                    consecutive,
                    artifact.error_type,
                    artifact.error_message,
                    len(pending) - position - 1,
                    consecutive,
                )
                break

        # Summarised from what is on disk, not from this process's results, so
        # a resumed run reports over every question rather than only the ones
        # this invocation happened to answer.
        summary = self._summarise(self._store.artifacts())
        self._store.write_summary({**summary.to_dict(), "manifest": self._store.manifest.to_dict()})
        return summary

    def _evaluate(self, question: Question) -> QuestionArtifact:
        started = time.perf_counter()

        gold_rows, gold_failed = self._execute(question.gold_sql, question.db_id)
        gold_elements = extract_gold_elements(question.gold_sql)
        attempt = self._answer(question)
        recall = compute_recall(gold_elements, attempt.retrieved)

        predicted_rows: Rows = []
        comparison: Comparison | None = None
        error_type = attempt.error_type
        error_message = attempt.error_message

        if not gold_failed and attempt.sql:
            predicted_rows, failed = self._execute(attempt.sql, question.db_id)
            if failed:
                # The generated query did not run. That is an execution
                # failure, not a wrong answer, and conflating them would put a
                # crash in the same bucket as a miscounted aggregate.
                error_type = error_type or "execution_failed"
                error_message = error_message or "the generated query did not execute"
            else:
                comparison = compare(predicted_rows, gold_rows, gold_sql=question.gold_sql)

        category = classify(
            comparison=comparison,
            recall=recall,
            error_type=error_type,
            gold_failed=gold_failed,
        )

        return QuestionArtifact(
            question_id=question.question_id,
            question=question.question,
            gold_sql=question.gold_sql,
            generated_sql=attempt.sql,
            matched=None if comparison is None else comparison.matched,
            verdict=comparison.verdict.value if comparison else "",
            failure_category=category.value,
            validation_attempts=attempt.validation_attempts,
            error_type=error_type,
            error_message=error_message,
            recall_at_k=recall.at_k,
            gold_element_count=recall.gold_size,
            unresolved_references=recall.unresolved_count,
            duration_ms=(time.perf_counter() - started) * 1000,
            input_tokens=attempt.input_tokens,
            output_tokens=attempt.output_tokens,
            answering_model=attempt.answering_model,
            gold_rows=gold_rows,
            predicted_rows=predicted_rows,
        )

    def _answer(self, question: Question) -> Attempt:
        """Call the answerer, converting an unexpected failure into a result.

        A pipeline that raises mid-run would otherwise lose the remaining
        questions to a traceback. One question that blew up is a data point;
        an aborted run is not.
        """
        try:
            return self._answerer(question)
        except Exception as exc:
            logger.exception("answerer failed on %s", question.question_id)
            return Attempt(error_type="internal_error", error_message=type(exc).__name__)

    def _execute(self, sql: str, db_id: str) -> tuple[Rows, bool]:
        try:
            return self._run_query(sql, db_id=db_id), False
        except Exception as exc:
            logger.debug("query failed: %s", type(exc).__name__)
            return [], True

    def _summarise(self, artifacts: Sequence[QuestionArtifact]) -> RunSummary:
        gold_errors = sum(1 for a in artifacts if a.failure_category == FailureCategory.GOLD_ERROR)
        infrastructure = sum(
            1 for a in artifacts if a.failure_category == FailureCategory.INFRASTRUCTURE
        )
        # Both exclusions say the same thing: nothing about the system under
        # test can be concluded from this question. A gold error means the
        # reference answer does not run; an infrastructure failure means the
        # model was never asked. Counting either as a wrong answer reports a
        # provider outage as a model that got worse.
        scorable = [a for a in artifacts if a.failure_category not in _UNSCORED]

        recall_values: dict[int, list[float]] = {}
        for artifact in artifacts:
            for k, value in artifact.recall_at_k.items():
                recall_values.setdefault(k, []).append(value)

        answered_by: dict[str, int] = {}
        for artifact in artifacts:
            if artifact.answering_model:
                answered_by[artifact.answering_model] = (
                    answered_by.get(artifact.answering_model, 0) + 1
                )

        recall: dict[str, float | int] = {
            "questions": len(artifacts),
            "scored": sum(1 for a in artifacts if a.recall_at_k),
            "unresolved_references": sum(a.unresolved_references for a in artifacts),
        }
        for k, values in sorted(recall_values.items()):
            recall[f"recall@{k}"] = round(sum(values) / len(values), 4)

        return RunSummary(
            total=len(artifacts),
            scored=len(scorable),
            matched=sum(1 for a in scorable if a.matched),
            gold_errors=gold_errors,
            infrastructure_errors=infrastructure,
            answered_by=answered_by,
            failures=counts([parse_category(a.failure_category) for a in artifacts]),
            recall=recall,
            input_tokens=sum(a.input_tokens for a in artifacts),
            output_tokens=sum(a.output_tokens for a in artifacts),
            duration_ms=sum(a.duration_ms for a in artifacts),
        )


__all__ = ["Answerer", "Attempt", "EvalRunner", "QueryRunner", "RunSummary"]
