"""Recall, the failure taxonomy, and the property the whole harness rests on:
that a run interrupted halfway is a run that can be finished.

No model and no database anywhere in this file. That is the point of the
answerer seam — the machinery that decides what a number *means* has to be
trustworthy before any tokens are spent producing one, and a test that needed
a live provider to check a taxonomy branch would never be run.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, ClassVar

import pytest

from evals import artifacts as artifacts_module
from evals.artifacts import (
    MAX_FILENAME_STEM,
    MAX_PERSISTED_ROWS,
    QuestionArtifact,
    RunManifest,
    RunStore,
    artifact_filename,
    current_commit,
)
from evals.comparison import Comparison, Verdict
from evals.dataset import Question, Split, load_questions, write_questions
from evals.recall import RecallResult, aggregate, compute_recall, extract_gold_elements
from evals.runner import MAX_LOGGED_MESSAGE_CHARS, Attempt, EvalRunner, _one_line
from evals.taxonomy import FailureCategory, classify, counts, is_infrastructure

pytestmark = pytest.mark.unit


def manifest(**overrides: Any) -> RunManifest:
    base = {
        "run_id": "test-run",
        "dataset": "demo",
        "split": "dev",
        "model": "model-a",
        "retriever_model_version": "hash-v1",
        "prompt_version": "sql_gen/v1",
        "commit": "abc1234",
    }
    return RunManifest(**{**base, **overrides})


def question(qid: str = "q1", gold: str = "SELECT id FROM customers") -> Question:
    return Question(question_id=qid, question="how many?", gold_sql=gold, split=Split.DEV)


class TestGoldElementExtraction:
    def test_a_simple_query_yields_its_table_and_columns(self) -> None:
        gold = extract_gold_elements("SELECT id, name FROM customers")

        assert gold.tables == {"customers"}
        assert gold.columns == {("customers", "id"), ("customers", "name")}

    def test_aliases_resolve_to_real_table_names(self) -> None:
        """The retriever returns real names, so a denominator full of `o` and
        `c` would score zero against a perfect retrieval."""
        gold = extract_gold_elements(
            "SELECT o.total, c.name FROM orders o JOIN customers c ON c.id = o.customer_id"
        )

        assert ("orders", "total") in gold.columns
        assert ("customers", "name") in gold.columns

    def test_an_ambiguous_column_is_recorded_as_unresolved(self) -> None:
        """With two tables in scope, `id` genuinely could be either. Guessing
        would put a fabricated element in the denominator."""
        gold = extract_gold_elements("SELECT id FROM orders, customers")

        assert gold.unresolved == {"id"}
        assert gold.columns == frozenset()

    def test_cte_names_are_not_treated_as_tables(self) -> None:
        """A CTE is query-local. Asking the retriever for it would count a
        miss for something the schema does not contain."""
        gold = extract_gold_elements("WITH recent AS (SELECT id FROM orders) SELECT id FROM recent")

        assert gold.tables == {"orders"}

    def test_subquery_columns_are_attributed_to_their_own_scope(self) -> None:
        gold = extract_gold_elements(
            "SELECT name FROM customers WHERE id IN (SELECT customer_id FROM orders)"
        )

        assert ("orders", "customer_id") in gold.columns
        assert ("customers", "customer_id") not in gold.columns

    def test_an_unparseable_gold_query_is_marked_not_guessed(self) -> None:
        gold = extract_gold_elements("SELCT ??? FROM")

        assert gold.parse_failed is True
        assert gold.is_usable is False


class TestRecall:
    def test_perfect_retrieval_scores_one(self) -> None:
        gold = extract_gold_elements("SELECT id FROM customers")

        result = compute_recall(gold, [("customers", "id"), ("customers", None)])

        assert result.at_k[5] == 1.0

    def test_rank_order_matters(self) -> None:
        """Truncating at k is the whole metric, so an unordered sequence
        produces a number that means nothing."""
        gold = extract_gold_elements("SELECT id FROM customers")
        ranked = [("orders", "total"), ("customers", "id"), ("customers", None)]

        result = compute_recall(gold, ranked, k_values=(1, 5))

        assert result.at_k[1] == 0.0
        assert result.at_k[5] == 1.0

    def test_retrieving_the_table_credits_its_columns(self) -> None:
        """A table-level hit puts every column in the prompt, so counting the
        column as missed would understate what the model was shown."""
        gold = extract_gold_elements("SELECT id, name FROM customers")

        result = compute_recall(gold, [("customers", None)])

        assert result.at_k[5] == 1.0

    def test_an_unusable_gold_query_is_skipped_not_scored_zero(self) -> None:
        """The retriever was never asked. Scoring it zero would blame it for a
        broken benchmark row."""
        result = compute_recall(extract_gold_elements("SELCT ???"), [("customers", "id")])

        assert result.skipped is True
        assert result.at_k == {}

    def test_aggregation_reports_what_it_skipped(self) -> None:
        """A mean over 2 of 3 questions is a different claim from a mean over
        3, and a row carrying only the number cannot be told apart."""
        results = [
            RecallResult(at_k={5: 1.0}),
            RecallResult(at_k={5: 0.0}),
            RecallResult(skipped=True),
        ]

        summary = aggregate(results)

        assert summary["recall@5"] == 0.5
        assert summary["scored"] == 2
        assert summary["skipped"] == 1

    def test_unresolved_references_are_carried_into_the_aggregate(self) -> None:
        summary = aggregate([RecallResult(at_k={5: 1.0}, unresolved_count=3)])

        assert summary["unresolved_references"] == 3


class TestTaxonomy:
    def test_a_gold_error_wins_over_everything(self) -> None:
        """Nothing about the system under test can be concluded from a
        question whose reference answer does not run."""
        category = classify(
            comparison=Comparison(Verdict.VALUE_MISMATCH, matched=False),
            error_type="syntax_error",
            gold_failed=True,
        )

        assert category is FailureCategory.GOLD_ERROR

    def test_a_retrieval_miss_outranks_the_unknown_identifier_it_caused(self) -> None:
        """Classifying by earliest cause is what makes the counts actionable:
        fixing retrieval removes both, and counting the symptom would
        over-report the remaining work."""
        category = classify(
            comparison=None,
            recall=RecallResult(at_k={5: 0.5}),
            error_type="unknown_identifier",
        )

        assert category is FailureCategory.RETRIEVAL_MISS

    def test_full_recall_lets_the_real_error_through(self) -> None:
        category = classify(
            comparison=None,
            recall=RecallResult(at_k={5: 1.0}),
            error_type="unknown_identifier",
        )

        assert category is FailureCategory.UNKNOWN_IDENTIFIER

    @pytest.mark.parametrize(
        ("error_type", "expected"),
        [
            ("statement_timeout", FailureCategory.TIMEOUT),
            ("not_read_only", FailureCategory.NOT_READ_ONLY),
            ("syntax_error", FailureCategory.SYNTAX_UNRECOVERABLE),
        ],
    )
    def test_error_types_map_to_categories(
        self, error_type: str, expected: FailureCategory
    ) -> None:
        assert classify(comparison=None, error_type=error_type) is expected

    def test_an_unrecognised_error_is_uncategorised_not_guessed(self) -> None:
        """A taxonomy that always finds a bucket is guessing. A count of
        failures nobody could explain is more useful than a wrong label."""
        assert (
            classify(comparison=None, error_type="something_new") is FailureCategory.UNCATEGORISED
        )

    def test_a_match_is_not_a_failure(self) -> None:
        assert classify(comparison=Comparison(Verdict.MATCH, matched=True)) is FailureCategory.NONE

    def test_counts_include_every_category_at_zero(self) -> None:
        """An absent key and a zero read identically in a report and are not
        the same claim: one says nothing failed that way, the other says
        nobody looked."""
        tally = counts([FailureCategory.TIMEOUT])

        assert tally["timeout"] == 1
        assert tally["row_order"] == 0
        assert set(tally) == {c.value for c in FailureCategory}


class TestDatasetLoading:
    def test_questions_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "q.jsonl"
        write_questions(path, [question("a"), question("b")])

        assert [q.question_id for q in load_questions(path)] == ["a", "b"]

    def test_a_split_filter_applies(self, tmp_path: Path) -> None:
        path = tmp_path / "q.jsonl"
        held = Question(question_id="h", question="?", gold_sql="SELECT 1", split=Split.HELD_OUT)
        write_questions(path, [question("a"), held])

        assert [q.question_id for q in load_questions(path, split=Split.HELD_OUT)] == ["h"]

    def test_a_missing_field_names_the_line(self, tmp_path: Path) -> None:
        """A question silently dropped for a typo'd key changes the
        denominator of every score computed from the file."""
        path = tmp_path / "q.jsonl"
        path.write_text('{"question_id": "a", "question": "?"}\n', encoding="utf-8")

        with pytest.raises(ValueError, match="is missing"):
            load_questions(path)

    def test_duplicate_ids_are_refused(self, tmp_path: Path) -> None:
        """They would collide as artifact filenames, and the loss would look
        like questions that were skipped."""
        path = tmp_path / "q.jsonl"
        write_questions(path, [question("a"), question("a")])

        with pytest.raises(ValueError, match="duplicate"):
            load_questions(path)


class TestResumption:
    """The property that makes the harness usable on a free tier.

    A 200-question run spans most of a daily token budget, so being stopped at
    question 140 is an ordinary operating condition. If that loses 140
    questions of work, nothing else here matters.
    """

    def test_a_finished_question_is_not_asked_again(self, tmp_path: Path) -> None:
        store = RunStore(tmp_path, manifest())
        store.start()
        store.record(QuestionArtifact(question_id="q1", question="?", gold_sql="SELECT 1"))

        asked: list[str] = []

        def answerer(q: Question) -> Attempt:
            asked.append(q.question_id)
            return Attempt(sql="SELECT id FROM customers")

        runner = EvalRunner(RunStore(tmp_path, manifest()), answerer, _rows({}))
        runner.run([question("q1"), question("q2")])

        assert asked == ["q2"]

    def test_the_summary_covers_the_whole_run_not_this_invocation(self, tmp_path: Path) -> None:
        """Summarised from disk. Otherwise a resumed run reports over only the
        questions the second invocation happened to answer."""
        store = RunStore(tmp_path, manifest())
        store.start()
        store.record(
            QuestionArtifact(question_id="q1", question="?", gold_sql="SELECT 1", matched=True)
        )

        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(sql="SELECT id FROM customers"),
            _rows({"SELECT id FROM customers": [[1]]}),
        )
        summary = runner.run([question("q1"), question("q2")])

        assert summary.total == 2

    def test_resuming_a_different_model_is_refused(self, tmp_path: Path) -> None:
        """The point of the fingerprint. Half one model and half another is not
        a measurement of either, and nothing downstream would show it."""
        RunStore(tmp_path, manifest(model="model-a")).start()

        with pytest.raises(ValueError, match="different configuration"):
            RunStore(tmp_path, manifest(model="model-b")).resume()

    def test_resuming_a_different_commit_is_refused(self, tmp_path: Path) -> None:
        """Fix a bug, re-run, and half the questions were answered by the old
        code — the easiest way to produce a result nobody can interpret."""
        RunStore(tmp_path, manifest(commit="aaa")).start()

        with pytest.raises(ValueError, match="different configuration"):
            RunStore(tmp_path, manifest(commit="bbb")).resume()

    def test_resuming_a_different_baseline_is_refused(self, tmp_path: Path) -> None:
        """The baselines exist to be *compared*, so mixing two is the one
        blend nobody would ever mean to make -- and `model` does not catch it,
        because two baselines share a model and differ in whether the schema
        was retrieved or handed over whole."""
        RunStore(tmp_path, manifest(baseline="retrieval-only")).start()

        with pytest.raises(ValueError, match="different configuration"):
            RunStore(tmp_path, manifest(baseline="full-schema")).resume()

    def test_a_different_run_id_is_not_a_conflict(self, tmp_path: Path) -> None:
        RunStore(tmp_path, manifest(run_id="one")).start()

        assert RunStore(tmp_path, manifest(run_id="one", notes="different note")).resume() == set()

    def test_the_manifest_is_written_before_any_question(self, tmp_path: Path) -> None:
        """So a run interrupted at question 1 still records what it was
        trying to do."""
        store = RunStore(tmp_path, manifest())
        store.start()

        assert (store.root / "manifest.json").exists()


class TestAFailedBudgetIsNotAnAnsweredQuestion:
    """Resumption skips what was *answered*, not what was written.

    The distinction the first full-split attempt was missing. It spent a daily
    token budget, recorded 308 questions as `llm_failed`, and every one of them
    was thereafter permanently done -- so the run could never be finished by
    resuming it, only restarted from nothing.
    """

    INFRASTRUCTURE: ClassVar[list[str]] = [
        "llm_failed",
        "scope_unavailable",
        "retrieval_failed",
        "internal_error",
    ]

    VERDICTS: ClassVar[list[str]] = ["unanswerable", "execution_failed"]

    @pytest.mark.parametrize("error_type", INFRASTRUCTURE)
    def test_an_infrastructure_failure_is_re_attempted(
        self, tmp_path: Path, error_type: str
    ) -> None:
        store = RunStore(tmp_path, manifest())
        store.start()
        store.record(
            QuestionArtifact(
                question_id="q1", question="?", gold_sql="SELECT 1", error_type=error_type
            )
        )

        assert RunStore(tmp_path, manifest()).resume() == frozenset()

    @pytest.mark.parametrize("error_type", VERDICTS)
    def test_a_real_verdict_is_not_re_attempted(self, tmp_path: Path, error_type: str) -> None:
        """The other half, and the reason this cannot just retry every failure.

        `unanswerable` and `execution_failed` are things the run *learned*
        about the model. Retrying them would spend the budget re-deriving
        results already in hand, and on a free tier that is the same defect
        with the sign flipped.
        """
        store = RunStore(tmp_path, manifest())
        store.start()
        store.record(
            QuestionArtifact(
                question_id="q1", question="?", gold_sql="SELECT 1", error_type=error_type
            )
        )

        assert RunStore(tmp_path, manifest()).resume() == {"q1"}

    def test_a_correct_answer_is_not_re_attempted(self, tmp_path: Path) -> None:
        store = RunStore(tmp_path, manifest())
        store.start()
        store.record(
            QuestionArtifact(question_id="q1", question="?", gold_sql="SELECT 1", matched=True)
        )

        assert RunStore(tmp_path, manifest()).resume() == {"q1"}

    def test_the_predicate_matches_what_leaves_the_denominator(self) -> None:
        """The two decisions this predicate now serves must be the same one.

        A question excluded from the score because the model was never asked is
        exactly a question a resumed run must ask. If these ever disagree, one
        of the two is wrong and the run either loses questions or re-spends
        budget on questions it already answered.
        """
        for error_type in self.INFRASTRUCTURE + self.VERDICTS:
            excluded = classify(comparison=None, error_type=error_type) is (
                FailureCategory.INFRASTRUCTURE
            )
            assert is_infrastructure(error_type) is excluded, error_type

    @pytest.mark.parametrize("bad", [["llm_failed"], {"type": "llm_failed"}, 7])
    def test_a_corrupt_error_type_costs_one_question_not_the_resume(
        self, tmp_path: Path, bad: object
    ) -> None:
        """The membership test is a `frozenset` lookup, which raises
        `TypeError` on an unhashable value. Letting that escape would abort
        the one operation whose whole purpose is surviving a bad situation --
        and this function already decided the opposite for unreadable files."""
        store = RunStore(tmp_path, manifest())
        store.start()
        store.record(QuestionArtifact(question_id="q1", question="?", gold_sql="SELECT 1"))
        artifact = next((store.root / "questions").glob("*.json"))
        record = json.loads(artifact.read_text(encoding="utf-8"))
        record["error_type"] = bad
        artifact.write_text(json.dumps(record), encoding="utf-8")

        assert RunStore(tmp_path, manifest()).resume() == frozenset()

    def test_a_retry_overwrites_the_record_rather_than_adding_one(self, tmp_path: Path) -> None:
        """Resume depends on a deterministic filename, so a re-attempt has to
        land on the file it replaces. Two files for one question would count it
        twice in every summary."""
        store = RunStore(tmp_path, manifest())
        store.start()
        store.record(
            QuestionArtifact(
                question_id="q1",
                question="?",
                gold_sql="SELECT id FROM customers",
                error_type="llm_failed",
            )
        )

        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(sql="SELECT id FROM customers"),
            _rows({"SELECT id FROM customers": [[1]]}),
        )
        summary = runner.run([question("q1")])

        assert len(list((store.root / "questions").glob("*.json"))) == 1
        assert summary.total == 1
        assert summary.matched == 1

    def test_a_run_stopped_by_a_spent_budget_finishes_on_the_next_day(self, tmp_path: Path) -> None:
        """The scenario the whole change exists for, end to end.

        Day one answers two questions and then the provider stops. Day two
        answers the rest. What matters is that the second run *re-attempts* the
        three the budget failed, and that the finished directory holds five
        answered questions rather than two answers and three excuses.
        """
        questions = [question(f"q{i}") for i in range(5)]
        budget = {"left": 2}

        def rationed(q: Question) -> Attempt:
            if budget["left"] <= 0:
                return Attempt(error_type="llm_failed", error_message="daily budget spent")
            budget["left"] -= 1
            return Attempt(sql="SELECT id FROM customers")

        rows = _rows({"SELECT id FROM customers": [[1]]})
        day_one = EvalRunner(RunStore(tmp_path, manifest()), rationed, rows, halt_after=2)
        first = day_one.run(questions)

        assert first.matched == 2
        assert first.scored == 2, "the three the budget failed must not be scored as wrong"

        budget["left"] = 5
        second = EvalRunner(RunStore(tmp_path, manifest()), rationed, rows).run(questions)

        assert second.total == 5
        assert second.matched == 5
        assert second.infrastructure_errors == 0


class TestHaltingOnAWall:
    """A spent budget does not recover inside the run.

    Continuing means asking a dead provider once per remaining question. It
    costs wall clock, buries the cause under identical records, and leaves the
    summary describing a directory that is mostly noise -- which is what 308
    consecutive `llm_failed` records looked like.
    """

    def test_it_stops_after_the_configured_run_of_failures(self, tmp_path: Path) -> None:
        asked: list[str] = []

        def dead(q: Question) -> Attempt:
            asked.append(q.question_id)
            return Attempt(error_type="llm_failed", error_message="out of budget")

        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            dead,
            _rows({"SELECT id FROM customers": [[1]]}),
            halt_after=3,
        )
        runner.run([question(f"q{i}") for i in range(20)])

        assert len(asked) == 3

    def test_the_untouched_questions_are_not_recorded(self, tmp_path: Path) -> None:
        """Halting must leave them genuinely unattempted. Recording them would
        reintroduce the bug the halt exists to contain."""
        store = RunStore(tmp_path, manifest())
        runner = EvalRunner(
            store,
            lambda q: Attempt(error_type="llm_failed"),
            _rows({"SELECT id FROM customers": [[1]]}),
            halt_after=2,
        )
        runner.run([question(f"q{i}") for i in range(10)])

        assert len(list((store.root / "questions").glob("*.json"))) == 2

    def test_a_success_resets_the_run(self, tmp_path: Path) -> None:
        """Consecutive, not total. A provider that blips and recovers must not
        end a run that is making progress."""
        answers = iter(
            [
                Attempt(error_type="llm_failed"),
                Attempt(error_type="llm_failed"),
                Attempt(sql="SELECT id FROM customers"),
                Attempt(error_type="llm_failed"),
                Attempt(error_type="llm_failed"),
                Attempt(sql="SELECT id FROM customers"),
            ]
        )
        asked: list[str] = []

        def flaky(q: Question) -> Attempt:
            asked.append(q.question_id)
            return next(answers)

        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            flaky,
            _rows({"SELECT id FROM customers": [[1]]}),
            halt_after=3,
        )
        runner.run([question(f"q{i}") for i in range(6)])

        assert len(asked) == 6

    def test_it_can_be_disabled(self, tmp_path: Path) -> None:
        asked: list[str] = []

        def dead(q: Question) -> Attempt:
            asked.append(q.question_id)
            return Attempt(error_type="llm_failed")

        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            dead,
            _rows({"SELECT id FROM customers": [[1]]}),
            halt_after=None,
        )
        runner.run([question(f"q{i}") for i in range(8)])

        assert len(asked) == 8

    def test_a_summary_is_still_written(self, tmp_path: Path) -> None:
        """A halted run is a run that stopped, not a run that crashed. The
        operator needs the summary to see what it got through."""
        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(error_type="llm_failed"),
            _rows({"SELECT id FROM customers": [[1]]}),
            halt_after=1,
        )
        summary = runner.run([question(f"q{i}") for i in range(5)])

        assert summary.infrastructure_errors == 1
        assert summary.execution_accuracy is None, "nothing scored is not zero accuracy"

    def test_a_provider_message_cannot_forge_a_log_record(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CWE-117. `error_message` is `str(exc)` from the provider or the
        database -- text this project does not author. A newline in a log
        record is a record separator, and forged entries in the log of an
        outage are read during the incident they would mislead."""
        forged = "rate limited\nINFO evals.runner: run completed successfully"
        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(error_type="llm_failed", error_message=forged),
            _rows({"SELECT id FROM customers": [[1]]}),
            halt_after=1,
        )
        with caplog.at_level(logging.ERROR, logger="evals.runner"):
            runner.run([question("q0")])

        halting = [r for r in caplog.records if "halting" in r.getMessage()]
        assert halting, "the halt must be logged"
        assert "\n" not in halting[0].getMessage()

    def test_an_enormous_provider_message_is_bounded(self, tmp_path: Path) -> None:
        """A provider returning its whole response body in an exception should
        not put it in the operator's terminal."""
        assert len(_one_line("x" * 5000)) <= MAX_LOGGED_MESSAGE_CHARS + 3

    def test_ordinary_messages_are_left_readable(self) -> None:
        assert _one_line("out of budget") == "out of budget"

    @pytest.mark.parametrize("halt_after", [0, -1])
    def test_a_meaningless_threshold_is_refused(self, tmp_path: Path, halt_after: int) -> None:
        """Zero reads as "never halt" to one person and "halt immediately" to
        the comparison. Refusing beats picking one."""
        with pytest.raises(ValueError, match="at least 1"):
            EvalRunner(
                RunStore(tmp_path, manifest()),
                lambda q: Attempt(),
                _rows({}),
                halt_after=halt_after,
            )


class TestRunnerBehaviour:
    def test_a_correct_answer_scores(self, tmp_path: Path) -> None:
        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(sql="SELECT id FROM customers"),
            _rows({"SELECT id FROM customers": [[1], [2]]}),
        )

        summary = runner.run([question(gold="SELECT id FROM customers")])

        assert summary.matched == 1
        assert summary.execution_accuracy == 1.0

    def test_an_answerer_that_raises_costs_one_question_not_the_run(self, tmp_path: Path) -> None:
        """A pipeline that blows up mid-run would otherwise lose every
        remaining question to a traceback."""

        def exploding(q: Question) -> Attempt:
            if q.question_id == "q1":
                raise RuntimeError("boom")
            return Attempt(sql="SELECT id FROM customers")

        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            exploding,
            _rows({"SELECT id FROM customers": [[1]]}),
        )
        summary = runner.run([question("q1"), question("q2")])

        assert summary.total == 2

    def test_a_failing_gold_query_is_a_gold_error(self, tmp_path: Path) -> None:
        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(sql="SELECT id FROM customers"),
            _rows({}),  # nothing resolves, so gold fails
        )
        summary = runner.run([question()])

        assert summary.gold_errors == 1

    def test_gold_errors_are_excluded_from_the_denominator(self, tmp_path: Path) -> None:
        """They cap achievable accuracy. Dropping them quietly inflates every
        score by however many there were."""
        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(sql="SELECT id FROM customers"),
            _rows({}),
        )
        summary = runner.run([question()])

        assert summary.scored == 0
        assert summary.execution_accuracy is None

    def test_execution_accuracy_is_none_rather_than_zero_when_nothing_scored(
        self, tmp_path: Path
    ) -> None:
        """A zero would enter a BENCHMARKS table looking like a measurement."""
        runner = EvalRunner(RunStore(tmp_path, manifest()), lambda q: Attempt(), _rows({}))

        assert runner.run([]).execution_accuracy is None

    def test_artifacts_are_written_for_failures_too(self, tmp_path: Path) -> None:
        """A run that persists only successes cannot answer the question a
        failure analysis asks."""
        store = RunStore(tmp_path, manifest())
        runner = EvalRunner(
            store,
            lambda q: Attempt(error_type="syntax_error", error_message="bad"),
            _rows({"SELECT id FROM customers": [[1]]}),
        )
        runner.run([question()])

        assert [a.error_type for a in store.artifacts()] == ["syntax_error"]

    def test_persisted_rows_are_bounded(self, tmp_path: Path) -> None:
        """Artifacts are real data in a second store — the same argument that
        keeps result values out of the audit log."""
        wide = [[i] for i in range(MAX_PERSISTED_ROWS + 25)]
        store = RunStore(tmp_path, manifest())
        runner = EvalRunner(
            store,
            lambda q: Attempt(sql="SELECT id FROM customers"),
            _rows({"SELECT id FROM customers": wide}),
        )
        runner.run([question(gold="SELECT id FROM customers")])

        artifact = store.artifacts()[0]
        assert len(artifact.gold_rows) == MAX_PERSISTED_ROWS
        assert artifact.rows_truncated is True

    def test_the_summary_is_written_as_json(self, tmp_path: Path) -> None:
        store = RunStore(tmp_path, manifest())
        runner = EvalRunner(store, lambda q: Attempt(), _rows({}))
        runner.run([question()])

        payload = json.loads((store.root / "summary.json").read_text(encoding="utf-8"))
        assert payload["manifest"]["commit"] == "abc1234"

    def test_the_answering_model_is_recorded_per_question(self, tmp_path: Path) -> None:
        """A fallback chain switches model on a 429, so a run can span two
        models without anything else noticing."""
        store = RunStore(tmp_path, manifest())
        runner = EvalRunner(
            store,
            lambda q: Attempt(sql="SELECT id FROM customers", answering_model="fallback-b"),
            _rows({"SELECT id FROM customers": [[1]]}),
        )
        runner.run([question(gold="SELECT id FROM customers")])

        assert store.artifacts()[0].answering_model == "fallback-b"


def _rows(table: dict[str, list[list[Any]]]) -> Any:
    """A query runner backed by a dict. Unknown SQL raises, which is how a
    gold query is made to fail without a database."""

    def run(sql: str, *, db_id: str = "") -> list[list[Any]]:
        if sql not in table:
            raise KeyError(sql)
        return table[sql]

    return run


class TestArtifactFilenames:
    """A question id is benchmark data, not a path component.

    Found by the first real corpus: Spider's ids are `spider:dev:00000`, and a
    colon is illegal in a Windows filename, so the run died at question one.
    """

    def test_a_colon_does_not_reach_the_filesystem(self) -> None:
        assert ":" not in artifact_filename("spider:dev:00000")

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../../../etc/cron.d/payload",
            r"..\..\windows\system32\x",
            "/etc/passwd",
            "..",
            "C:relative-to-drive",
        ],
    )
    def test_a_hostile_id_cannot_escape_the_directory(self, tmp_path: Path, hostile: str) -> None:
        """The security half of the same fact. BIRD ships its own ids, and a
        corpus is a file someone downloaded.

        Asserted by resolving the path rather than by checking for substrings:
        containment is the property that matters, and a substring check passes
        for reasons that can stop being true."""
        resolved = (tmp_path / artifact_filename(hostile)).resolve()

        assert resolved.parent == tmp_path.resolve()

    def test_two_ids_that_sanitise_alike_still_get_different_files(self) -> None:
        """Sanitising alone is lossy -- `a:b` and `a/b` both become `a-b`.

        One file for two questions means the run reports fewer questions than
        it was given, and the difference looks like questions that were skipped.
        """
        assert artifact_filename("a:b") != artifact_filename("a/b")

    def test_the_readable_part_survives(self) -> None:
        # An artifact directory nobody can skim is an artifact directory nobody
        # reads, which defeats the point of writing one file per question.
        assert artifact_filename("spider:dev:00042").startswith("spider-dev-00042-")

    def test_a_very_long_id_is_bounded(self) -> None:
        name = artifact_filename("x" * 500)

        assert len(name) <= MAX_FILENAME_STEM + len("-12345678.json")

    def test_it_is_deterministic(self) -> None:
        # Resume depends on writing the same question to the same file.
        assert artifact_filename("spider:dev:1") == artifact_filename("spider:dev:1")


class TestResumeReadsIdsFromContent:
    def test_a_sanitised_filename_still_resumes(self, tmp_path: Path) -> None:
        """Resume must not infer an id from a name that went through a
        substitution -- that is how it comes to believe the wrong question is
        done."""
        store = RunStore(tmp_path, manifest())
        store.start()
        store.record(
            QuestionArtifact(question_id="spider:dev:00000", question="?", gold_sql="SELECT 1")
        )

        assert RunStore(tmp_path, manifest()).resume() == {"spider:dev:00000"}

    def test_an_unreadable_artifact_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """Re-answering one question is the cheap, correct response to a file
        written mid-crash. Refusing the whole resume is not."""
        store = RunStore(tmp_path, manifest())
        store.start()
        store.record(QuestionArtifact(question_id="q1", question="?", gold_sql="SELECT 1"))
        (tmp_path / manifest().run_id / "questions" / "truncated.json").write_text(
            '{"question_id":', encoding="utf-8"
        )

        assert RunStore(tmp_path, manifest()).resume() == {"q1"}


class TestEveryEmittedErrorTypeIsClassified:
    """No `error_type` any component produces may land in UNCATEGORISED.

    That bucket means "a failure nobody could explain". The first real run put
    12 of 150 questions in it for a cause the runner had already written down --
    `execution_failed` -- because the taxonomy had no entry. This walks the
    emitters so the next one added cannot repeat it.
    """

    EMITTED: ClassVar[list[str]] = [
        # evals/runner.py
        "execution_failed",
        "internal_error",
        # evals/pipeline.py
        "scope_unavailable",
        "retrieval_failed",
        "unanswerable",
        "llm_failed",
        # validation/validator.py and execution/executor.py, reaching the
        # answerer through ValidationResult
        "syntax_error",
        "multiple_statements",
        "not_read_only",
        "table_not_found",
        "unknown_identifier",
        "cost_exceeded",
        "explain_failed",
        "statement_timeout",
        "permission_denied",
    ]

    @pytest.mark.parametrize("error_type", EMITTED)
    def test_it_has_a_category(self, error_type: str) -> None:
        category = classify(comparison=None, error_type=error_type)

        assert category is not FailureCategory.UNCATEGORISED

    def test_the_two_structures_agree(self) -> None:
        """`_INFRASTRUCTURE` exists only to express *ordering*; if it and the
        lookup table disagreed, a type would be classified one way before recall
        and another way after."""
        from evals.taxonomy import _FROM_ERROR_TYPE, _INFRASTRUCTURE

        assert all(
            _FROM_ERROR_TYPE[name] is FailureCategory.INFRASTRUCTURE for name in _INFRASTRUCTURE
        )

    def test_an_unknown_type_still_falls_through_rather_than_raising(self) -> None:
        """The bucket keeps its job: a type from a future version is counted,
        not fatal."""
        assert (
            classify(comparison=None, error_type="invented_later") is FailureCategory.UNCATEGORISED
        )


class TestInfrastructureLeavesTheDenominator:
    def test_a_provider_outage_is_not_a_wrong_answer(self, tmp_path: Path) -> None:
        """Counting it as one reports a ten-minute rate limit as a model that
        got worse -- the same argument that excludes gold errors."""
        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: (
                Attempt(sql="SELECT id FROM customers", retrieved=(("customers", "id"),))
                if q.question_id == "q1"
                else Attempt(
                    error_type="llm_failed",
                    error_message="429",
                    retrieved=(("customers", "id"),),
                )
            ),
            _rows({"SELECT id FROM customers": [[1]]}),
        )

        summary = runner.run([question("q1"), question("q2")])

        assert summary.total == 2
        assert summary.scored == 1
        assert summary.infrastructure_errors == 1
        assert summary.execution_accuracy == 1.0

    def test_a_failed_execution_is_still_scored(self, tmp_path: Path) -> None:
        """The model wrote SQL and the database refused it. That is the model's
        failure and it belongs in the denominator -- it is what the
        invalid-query rate measures."""
        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(sql="SELECT nope FROM customers", retrieved=(("customers", "id"),)),
            _rows({"SELECT id FROM customers": [[1]]}),
        )

        summary = runner.run([question("q1")])

        assert summary.scored == 1
        assert summary.matched == 0
        assert summary.failures["execution_failed"] == 1


class TestModelMixIsVisible:
    """A run answered by two models is not one measurement.

    Measured: one run was 100% `gpt-oss-120b` at 73%; the next, same code and
    same k, fell back for 27 questions to a model that scored 0% and reported
    64.7% overall. Neither figure is a score for any single model, and nothing
    in the summary said so.
    """

    def test_one_model_reads_as_a_single_model_run(self, tmp_path: Path) -> None:
        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(
                sql="SELECT id FROM customers",
                retrieved=(("customers", "id"),),
                answering_model="gpt-oss-120b",
            ),
            _rows({"SELECT id FROM customers": [[1]]}),
        )

        summary = runner.run([question("q1"), question("q2")])

        assert summary.answered_by == {"gpt-oss-120b": 2}
        assert summary.single_model is True

    def test_a_fallback_makes_the_blend_visible(self, tmp_path: Path) -> None:
        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(
                sql="SELECT id FROM customers",
                retrieved=(("customers", "id"),),
                answering_model="gpt-oss-120b" if q.question_id == "q1" else "qwen-27b",
            ),
            _rows({"SELECT id FROM customers": [[1]]}),
        )

        summary = runner.run([question("q1"), question("q2")])

        assert summary.answered_by == {"gpt-oss-120b": 1, "qwen-27b": 1}
        assert summary.single_model is False

    def test_a_question_that_never_reached_a_model_is_not_counted(self, tmp_path: Path) -> None:
        """An outage leaves no answering model, and inventing one would put a
        question in a bucket no model earned."""
        runner = EvalRunner(
            RunStore(tmp_path, manifest()),
            lambda q: Attempt(error_type="llm_failed", retrieved=(("customers", "id"),)),
            _rows({"SELECT id FROM customers": [[1]]}),
        )

        summary = runner.run([question("q1")])

        assert summary.answered_by == {}
        assert summary.single_model is True


class TestCommitProvenance:
    """The commit field is the reproducibility record, so it must not overstate.

    Found by reading the five recorded runs back: every one was made while its
    fixes were still uncommitted, so every manifest names the commit *before*
    the code that produced the number.
    """

    def test_a_dirty_tree_is_marked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            artifacts_module,
            "_git",
            lambda *args: "abc1234\n" if args[0] == "rev-parse" else " M src/x.py\n",
        )

        assert current_commit() == "abc1234-dirty"

    def test_a_clean_tree_is_not_marked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            artifacts_module,
            "_git",
            lambda *args: "abc1234\n" if args[0] == "rev-parse" else "",
        )

        assert current_commit() == "abc1234"

    def test_an_unknown_status_does_not_pass_as_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failing the status check must not silently downgrade to a bare hash.

        A bare hash is a positive claim that the tree was clean, which is the
        one claim this function exists to stop making without evidence.
        """
        monkeypatch.setattr(
            artifacts_module,
            "_git",
            lambda *args: "abc1234\n" if args[0] == "rev-parse" else None,
        )

        assert current_commit() == "abc1234-unverified"

    def test_no_repository_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(artifacts_module, "_git", lambda *args: None)

        assert current_commit() == "unknown"
