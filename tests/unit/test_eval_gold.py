"""The file that carries verification's result into the eval.

What is being tested here is a *denominator*. Every question this module drops
is one the accuracy figure is not divided by, and every question it lets
through unverified is one the figure is divided by without anyone having
checked the data behind it. Both mistakes produce a number that looks exactly
like a correct one, which is why these are unit tests with hard assertions on
counts rather than a smoke check that the file parses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.dataset import Question, Split
from evals.gold import GoldEntry, apply_verified_gold, load_verified_gold


def write_gold(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def entry(
    question_id: str,
    *,
    sql: str = "SELECT 1",
    outcome: str = "match",
    scoreable: bool = True,
    db_id: str = "concert_singer",
    schema: str = "spider_concert_singer",
) -> dict[str, object]:
    return {
        "question_id": question_id,
        "sql": sql,
        "outcome": outcome,
        "scoreable": scoreable,
        "db_id": db_id,
        "schema": schema,
    }


def question(
    question_id: str, *, gold_sql: str = 'SELECT "x"', db_id: str = "concert_singer"
) -> Question:
    return Question(
        question_id=question_id,
        question="how many singers?",
        gold_sql=gold_sql,
        dataset="spider",
        split=Split.DEV,
        db_id=db_id,
    )


class TestLoading:
    def test_reads_one_entry_per_line(self, tmp_path: Path) -> None:
        path = write_gold(tmp_path / "gold.jsonl", [entry("a"), entry("b")])

        gold = load_verified_gold(path)

        assert sorted(gold) == ["a", "b"]
        assert gold["a"].schema == "spider_concert_singer"

    def test_a_repeated_question_id_is_refused(self, tmp_path: Path) -> None:
        """Two reference answers for one question, and which one wins is file order.

        Silently keeping the last would make the score depend on how the file
        was concatenated.
        """
        path = write_gold(
            tmp_path / "gold.jsonl",
            [entry("a", sql="SELECT 1"), entry("a", sql="SELECT 2")],
        )

        with pytest.raises(ValueError, match="repeats question_id"):
            load_verified_gold(path)

    def test_a_missing_field_names_the_line(self, tmp_path: Path) -> None:
        path = write_gold(tmp_path / "gold.jsonl", [entry("a"), {"question_id": "b"}])

        with pytest.raises(ValueError, match=r"gold\.jsonl:2 is missing"):
            load_verified_gold(path)

    def test_scoreable_without_sql_is_a_contradiction(self, tmp_path: Path) -> None:
        """ "Scoreable" means the statement ran and its results were compared.

        A blank one cannot have done either, so the file disagrees with itself
        and the run must not start on it.
        """
        path = write_gold(tmp_path / "gold.jsonl", [entry("a", sql="   ")])

        with pytest.raises(ValueError, match="scoreable but carries no SQL"):
            load_verified_gold(path)

    def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "gold.jsonl"
        path.write_text(json.dumps(entry("a")) + "\n\n", encoding="utf-8")

        assert list(load_verified_gold(path)) == ["a"]


class TestApplying:
    def test_gold_sql_is_replaced_by_the_verified_statement(self) -> None:
        """The whole point: the eval runs what was verified, not what shipped.

        The split file's SQL is SQLite -- here a double-quoted string literal,
        which PostgreSQL reads as a column name.
        """
        questions = [question("a", gold_sql='SELECT * FROM t WHERE name = "Bob"')]
        gold = {"a": load_one(entry("a", sql="SELECT * FROM t WHERE name = 'Bob'"))}

        applied = apply_verified_gold(questions, gold)

        assert applied.questions[0].gold_sql == "SELECT * FROM t WHERE name = 'Bob'"
        assert applied.questions[0].question_id == "a"

    def test_unscoreable_questions_are_dropped_and_counted_by_reason(self) -> None:
        questions = [question(name) for name in ("a", "b", "c", "d")]
        gold = {
            "a": load_one(entry("a")),
            "b": load_one(entry("b", outcome="dialect_error", sql="", scoreable=False)),
            "c": load_one(entry("c", outcome="undetermined_limit", sql="", scoreable=False)),
            "d": load_one(entry("d", outcome="dialect_error", sql="", scoreable=False)),
        }

        applied = apply_verified_gold(questions, gold)

        assert [q.question_id for q in applied.questions] == ["a"]
        assert applied.excluded == {"dialect_error": 2, "undetermined_limit": 1}
        assert applied.excluded_total == 3

    def test_a_mismatch_is_still_scoreable(self) -> None:
        """A known conversion difference is reported, not excluded.

        The gold query runs on PostgreSQL and returns *an* answer; it just is
        not the answer SQLite gives. Scoring against it stays internally
        consistent, and R-04 carries the caveat. Excluding it would delete the
        one finding the verification produced.
        """
        applied = apply_verified_gold(
            [question("a")], {"a": load_one(entry("a", outcome="mismatch"))}
        )

        assert len(applied.questions) == 1
        assert applied.excluded == {}

    def test_an_unverified_question_stops_the_run(self) -> None:
        """The failure that must never be tolerated.

        A question with no entry is one nobody checked the conversion against.
        Dropping it quietly would shrink the denominator; keeping it would
        score against data of unknown fidelity. Neither is a measurement.
        """
        questions = [question("a"), question("b")]

        with pytest.raises(ValueError, match="never verified"):
            apply_verified_gold(questions, {"a": load_one(entry("a"))})

    def test_schemas_are_collected_for_the_query_runner(self) -> None:
        applied = apply_verified_gold(
            [question("a", db_id="wta_1")],
            {"a": load_one(entry("a", db_id="wta_1", schema="spider_wta_1"))},
        )

        assert applied.schemas == {"wta_1": "spider_wta_1"}

    def test_the_summary_reports_what_was_removed(self) -> None:
        """The exclusion travels with the score or it is not an exclusion."""
        applied = apply_verified_gold(
            [question("a"), question("b")],
            {
                "a": load_one(entry("a")),
                "b": load_one(entry("b", outcome="gold_error", sql="", scoreable=False)),
            },
        )

        assert applied.to_dict() == {
            "scoreable": 1,
            "excluded": 1,
            "excluded_by_outcome": {"gold_error": 1},
        }


def load_one(row: dict[str, object]) -> GoldEntry:
    """One entry, built directly. Parsing is covered by :class:`TestLoading`."""
    return GoldEntry(
        question_id=str(row["question_id"]),
        sql=str(row["sql"]),
        outcome=str(row["outcome"]),
        scoreable=bool(row["scoreable"]),
        db_id=str(row["db_id"]),
        schema=str(row["schema"]),
    )
