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
from evals.taxonomy import FailureCategory, classify, counts, parse_category

logger = logging.getLogger(__name__)

Rows = list[list[Any]]


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
    failures: dict[str, int] = field(default_factory=dict)
    recall: dict[str, float | int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0.0

    @property
    def execution_accuracy(self) -> float | None:
        """Matched over *scored*, where scored excludes gold errors.

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
            "execution_accuracy": self.execution_accuracy,
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
    """

    def __init__(
        self,
        store: RunStore,
        answerer: Answerer,
        run_query: QueryRunner,
        *,
        on_progress: Callable[[QuestionArtifact], None] | None = None,
    ) -> None:
        self._store = store
        self._answerer = answerer
        self._run_query = run_query
        self._on_progress = on_progress

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
            logger.info("skipping %d already-recorded question(s)", len(questions) - len(pending))

        for question in pending:
            artifact = self._evaluate(question)
            self._store.record(artifact)
            if self._on_progress is not None:
                self._on_progress(artifact)

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
        scorable = [a for a in artifacts if a.failure_category != FailureCategory.GOLD_ERROR]

        recall_values: dict[int, list[float]] = {}
        for artifact in artifacts:
            for k, value in artifact.recall_at_k.items():
                recall_values.setdefault(k, []).append(value)

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
            failures=counts([parse_category(a.failure_category) for a in artifacts]),
            recall=recall,
            input_tokens=sum(a.input_tokens for a in artifacts),
            output_tokens=sum(a.output_tokens for a in artifacts),
            duration_ms=sum(a.duration_ms for a in artifacts),
        )


__all__ = ["Answerer", "Attempt", "EvalRunner", "QueryRunner", "RunSummary"]
