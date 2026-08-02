"""Recall, the failure taxonomy, and the property the whole harness rests on:
that a run interrupted halfway is a run that can be finished.

No model and no database anywhere in this file. That is the point of the
answerer seam — the machinery that decides what a number *means* has to be
trustworthy before any tokens are spent producing one, and a test that needed
a live provider to check a taxonomy branch would never be run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.artifacts import MAX_PERSISTED_ROWS, QuestionArtifact, RunManifest, RunStore
from evals.comparison import Comparison, Verdict
from evals.dataset import Question, Split, load_questions, write_questions
from evals.recall import RecallResult, aggregate, compute_recall, extract_gold_elements
from evals.runner import Attempt, EvalRunner
from evals.taxonomy import FailureCategory, classify, counts

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
