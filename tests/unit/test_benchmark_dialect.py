"""The gold-SQL repairs and classifications, all decidable without a database.

Every rule here was written against a measurement from the real Spider dev
split, and each docstring records the number, because "this seemed like a good
idea" and "this recovered 213 of 1034 questions" are different justifications.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from benchmark.convert import (
    BIGINT,
    BYTEA,
    DOUBLE,
    TEXT,
    pg_type_for_storage_classes,
    plan_database,
)
from benchmark.sqlite_source import open_database
from benchmark.verify import (
    Outcome,
    QueryCheck,
    VerificationReport,
    limit_cut_is_tied,
    schema_names,
    transpile_to_postgres,
)
from core.settings import BenchmarkSettings

KNOWN = frozenset({"course", "student", "name", "age", "title"})


class TestQuotedLiteralRepair:
    """SQLite treats a double-quoted token that names nothing as a *string*.

    PostgreSQL has no such fallback. Measured on Spider dev: 213 of 1034
    questions failed with `column "..." does not exist`, and every single
    `undefined_column` failure in the run was this and nothing else.
    """

    @pytest.mark.parametrize(
        ("gold", "expected_literal"),
        [
            ('SELECT name FROM student WHERE course = "Math"', "'Math'"),
            ('SELECT name FROM student WHERE name LIKE "%w%"', "'%w%'"),
            ('SELECT title FROM student WHERE title = "Robbin CV"', "'Robbin CV'"),
        ],
    )
    def test_an_unknown_quoted_token_becomes_a_string(
        self, gold: str, expected_literal: str
    ) -> None:
        rendered = transpile_to_postgres(gold, known_names=KNOWN)
        assert expected_literal in rendered

    def test_a_quoted_token_that_is_a_real_column_stays_an_identifier(self) -> None:
        # Load-bearing. Spider really does have columns like
        # `Official_ratings_(millions)`, and turning one into a string literal
        # would silently change the query into nonsense that still runs.
        rendered = transpile_to_postgres('SELECT "name" FROM student', known_names=KNOWN)
        assert '"name"' in rendered
        assert "'name'" not in rendered

    def test_a_qualified_quoted_token_is_left_alone(self) -> None:
        rendered = transpile_to_postgres('SELECT s."zzz" FROM student s', known_names=KNOWN)
        assert '"zzz"' in rendered

    def test_without_a_schema_nothing_is_rewritten(self) -> None:
        # No evidence, no repair. Guessing without the name set is exactly the
        # move that would turn a real column into a string.
        rendered = transpile_to_postgres('SELECT name FROM student WHERE course = "Math"')
        assert "'Math'" not in rendered

    def test_single_quoted_strings_are_untouched(self) -> None:
        assert "'Math'" in transpile_to_postgres(
            "SELECT name FROM student WHERE course = 'Math'", known_names=KNOWN
        )

    def test_an_unparseable_query_still_raises(self) -> None:
        with pytest.raises(ValueError, match=r".+"):
            transpile_to_postgres("SELECT FROM WHERE ((((", known_names=KNOWN)


class TestLikeIsCaseInsensitive:
    """SQLite's LIKE folds case; PostgreSQL's does not.

    Measured on Spider dev: 3 of 1034 questions returned different rows for
    this reason alone, and every one was counted as a conversion mismatch until
    the transpilation said what SQLite meant.
    """

    @pytest.mark.parametrize(
        "gold",
        [
            "SELECT name FROM student WHERE name LIKE 'korea'",
            "SELECT name FROM student WHERE name LIKE '%w%'",
        ],
    )
    def test_like_becomes_ilike(self, gold: str) -> None:
        rendered = transpile_to_postgres(gold, known_names=KNOWN)
        assert "ILIKE" in rendered.upper()

    def test_not_like_keeps_its_negation(self) -> None:
        # Load-bearing. sqlglot models NOT LIKE as a Like node carrying
        # `negate=True`, not as a Not wrapping a Like, so rebuilding the node
        # from `this` and `expression` alone silently inverts the predicate.
        # This assertion is what caught that.
        rendered = transpile_to_postgres(
            "SELECT name FROM student WHERE name NOT LIKE 'a%'", known_names=KNOWN
        ).upper()
        assert "NOT ILIKE" in rendered

    def test_an_escape_clause_survives(self) -> None:
        rendered = transpile_to_postgres(
            r"SELECT name FROM student WHERE name LIKE '100!%' ESCAPE '!'", known_names=KNOWN
        ).upper()
        assert "ILIKE" in rendered
        assert "ESCAPE" in rendered

    def test_a_query_without_like_is_untouched(self) -> None:
        rendered = transpile_to_postgres("SELECT name FROM student", known_names=KNOWN)
        assert "ILIKE" not in rendered.upper()

    def test_the_repaired_quoted_literal_is_still_a_like_pattern(self) -> None:
        # Both repairs run on the same statement, and this is the case that
        # needs both: SQLite reads `"%w%"` as a string *and* folds the case.
        rendered = transpile_to_postgres(
            'SELECT name FROM student WHERE name LIKE "%w%"', known_names=KNOWN
        )
        assert "'%w%'" in rendered
        assert "ILIKE" in rendered.upper()


class TestLimitCutIsTied:
    """The check that separates an arbitrary answer from a conversion defect.

    Both look identical from outside -- same rows overall, different prefix --
    and only one of them is the benchmark's fault. On Spider dev this rule
    separated 16 real ties from 2 questions where the engines ordered the same
    key differently because a mixed column had to become text.
    """

    @pytest.fixture
    def towns(self, make_sqlite_db: Callable[..., Path]) -> Path:
        return make_sqlite_db(
            "t",
            """
            CREATE TABLE teacher (name TEXT, hometown TEXT);
            INSERT INTO teacher VALUES
                ('a', 'Bristol'), ('b', 'Leeds'), ('c', 'York'), ('d', 'Hull'), ('e', 'Hull');
            """,
        )

    def test_a_tie_at_the_cut_is_reported(self, towns: Path) -> None:
        # Bristol, Leeds and York all have one teacher, so "the town with the
        # fewest" has three equally correct answers.
        gold = "SELECT hometown FROM teacher GROUP BY hometown ORDER BY count(*) ASC LIMIT 1"
        with open_database(towns, db_id="t") as database:
            assert limit_cut_is_tied(database, gold) is True

    def test_a_distinct_top_row_is_not_a_tie(self, towns: Path) -> None:
        # Hull has two teachers and nothing else has more. One correct answer.
        gold = "SELECT hometown FROM teacher GROUP BY hometown ORDER BY count(*) DESC LIMIT 1"
        with open_database(towns, db_id="t") as database:
            assert limit_cut_is_tied(database, gold) is False

    def test_a_limit_with_no_order_by_is_undetermined(self, towns: Path) -> None:
        with open_database(towns, db_id="t") as database:
            assert limit_cut_is_tied(database, "SELECT name FROM teacher LIMIT 1") is True

    def test_a_limit_that_never_cut_is_not_undetermined(self, towns: Path) -> None:
        # Nothing was excluded, so the LIMIT cannot explain any difference.
        gold = "SELECT name FROM teacher ORDER BY name LIMIT 50"
        with open_database(towns, db_id="t") as database:
            assert limit_cut_is_tied(database, gold) is False

    def test_a_query_with_no_limit_is_not_undetermined(self, towns: Path) -> None:
        with open_database(towns, db_id="t") as database:
            assert limit_cut_is_tied(database, "SELECT name FROM teacher") is False

    def test_distinct_is_refused_rather_than_guessed(self, towns: Path) -> None:
        # The probe adds the ordering key to the select list, which changes
        # which rows a DISTINCT collapses -- so the cut would land somewhere
        # else and prove nothing. Conservative: report a mismatch instead.
        gold = "SELECT DISTINCT hometown FROM teacher ORDER BY hometown LIMIT 1"
        with open_database(towns, db_id="t") as database:
            assert limit_cut_is_tied(database, gold) is False

    def test_a_non_literal_limit_is_refused(self, towns: Path) -> None:
        # `LIMIT 1 + 1` cuts after two rows, and sorted by hometown those are
        # Bristol and Hull -- not a tie. But sqlglot answers '1' for the count,
        # because `.name` returns the leftmost leaf of the expression, so a
        # naive read would test rows 1 and 2 (Bristol, Hull) instead of 2 and 3
        # (Hull, Hull) and report a tie that is not there.
        gold = "SELECT hometown FROM teacher ORDER BY hometown LIMIT 1 + 1"
        with open_database(towns, db_id="t") as database:
            assert limit_cut_is_tied(database, gold) is False

    def test_an_offset_moves_the_cut(self, towns: Path) -> None:
        # Sorted by hometown: Bristol, Hull, Hull, Leeds, York. A cut after two
        # rows falls between the two Hulls; without the OFFSET it would fall
        # after Bristol and there would be nothing ambiguous about it.
        with open_database(towns, db_id="t") as database:
            tied = "SELECT hometown FROM teacher ORDER BY hometown LIMIT 1 OFFSET 1"
            plain = "SELECT hometown FROM teacher ORDER BY hometown LIMIT 1"
            assert limit_cut_is_tied(database, tied) is True
            assert limit_cut_is_tied(database, plain) is False

    def test_an_unparseable_query_is_refused(self, towns: Path) -> None:
        with open_database(towns, db_id="t") as database:
            assert limit_cut_is_tied(database, "SELECT FROM WHERE ((((") is False


class TestSchemaNames:
    def test_collects_tables_and_columns_lowercased(
        self, make_sqlite_db: Callable[..., Path]
    ) -> None:
        path = make_sqlite_db("s", "CREATE TABLE Stadium (Stadium_ID INT, Name TEXT);")
        with open_database(path, db_id="s") as database:
            assert schema_names(database) == frozenset({"stadium", "stadium_id", "name"})


class TestStorageClassInference:
    """Exact inference, replacing a row sample that could not see far enough."""

    @pytest.mark.parametrize(
        ("classes", "expected"),
        [
            ({"integer"}, BIGINT),
            ({"integer", "null"}, BIGINT),
            ({"real"}, DOUBLE),
            ({"integer", "real"}, DOUBLE),
            ({"text"}, TEXT),
            ({"integer", "text"}, TEXT),
            ({"blob"}, BYTEA),
            ({"blob", "integer"}, TEXT),
            ({"null"}, TEXT),
        ],
    )
    def test_widening_rules(self, classes: set[str], expected: str) -> None:
        assert pg_type_for_storage_classes(
            classes, declared="TEXT" if not classes - {"null"} else ""
        ) == (expected if classes - {"null"} else TEXT)

    def test_an_all_null_column_falls_back_to_the_declaration(self) -> None:
        assert pg_type_for_storage_classes({"null"}, declared="INTEGER") == BIGINT
        assert pg_type_for_storage_classes(set(), declared="REAL") == DOUBLE


class TestForeignKeyTypeUnification:
    """Spider declares a foreign key column TEXT and its referenced key INT.

    SQLite joins them because comparing TEXT affinity to INTEGER affinity
    applies *numeric* affinity to the text operand. PostgreSQL answers
    `operator does not exist: text = bigint`. Measured: 35 of 769 foreign keys
    across 21 of 166 databases.
    """

    def test_a_text_fk_adopts_the_numeric_type_of_its_referent(
        self, make_sqlite_db: Callable[..., Path], benchmark_settings: BenchmarkSettings
    ) -> None:
        path = make_sqlite_db(
            "concert",
            """
            CREATE TABLE stadium (Stadium_ID INT PRIMARY KEY, Name TEXT);
            CREATE TABLE concert (
                concert_ID INT PRIMARY KEY,
                Stadium_ID TEXT REFERENCES stadium(Stadium_ID)
            );
            INSERT INTO stadium VALUES (1, 'Hampden'), (2, 'Ibrox');
            INSERT INTO concert VALUES (1, '1'), (2, '2');
            """,
        )
        with open_database(path, db_id="concert") as database:
            _, plans = plan_database(database, settings=benchmark_settings)

        concert = next(p for p in plans if p.target_name == "concert")
        assert next(c for c in concert.columns if c.target_name == "stadium_id").pg_type == BIGINT

    def test_unification_never_runs_toward_text(
        self, make_sqlite_db: Callable[..., Path], benchmark_settings: BenchmarkSettings
    ) -> None:
        # The direction matters. `'01' = 1` is true under SQLite's numeric
        # affinity but `'01' = '1'` is false as text, so widening the numeric
        # side to text would silently change which rows join.
        path = make_sqlite_db(
            "rev",
            """
            CREATE TABLE parent (id TEXT PRIMARY KEY);
            CREATE TABLE child (pid INT REFERENCES parent(id));
            INSERT INTO parent VALUES ('1'), ('2');
            INSERT INTO child VALUES (1), (2);
            """,
        )
        with open_database(path, db_id="rev") as database:
            _, plans = plan_database(database, settings=benchmark_settings)

        types = {p.target_name: {c.target_name: c.pg_type for c in p.columns} for p in plans}
        assert types["child"]["pid"] == BIGINT
        assert types["parent"]["id"] == BIGINT

    def test_a_column_that_cannot_convert_is_left_alone(
        self, make_sqlite_db: Callable[..., Path], benchmark_settings: BenchmarkSettings
    ) -> None:
        # SQLite would coerce `'unknown'` to 0 in a numeric comparison. That is
        # not reproducible, so the types stay put and the constraint is dropped
        # and reported -- the honest outcome.
        path = make_sqlite_db(
            "dirty",
            """
            CREATE TABLE parent (id INT PRIMARY KEY);
            CREATE TABLE child (pid TEXT REFERENCES parent(id));
            INSERT INTO parent VALUES (1);
            INSERT INTO child VALUES ('1'), ('unknown');
            """,
        )
        with open_database(path, db_id="dirty") as database:
            _, plans = plan_database(database, settings=benchmark_settings)

        types = {p.target_name: {c.target_name: c.pg_type for c in p.columns} for p in plans}
        assert types["child"]["pid"] == TEXT
        assert types["parent"]["id"] == BIGINT


class TestOutcomes:
    def test_ambiguous_order_is_not_a_mismatch(self) -> None:
        # It is counted as matched, because the rows are identical and the gold
        # query never determined their order.
        assert Outcome.AMBIGUOUS_ORDER is not Outcome.MISMATCH

    def test_dialect_error_is_distinct_from_a_conversion_fault(self) -> None:
        assert Outcome.DIALECT_ERROR is not Outcome.POSTGRES_ERROR

    def test_an_undetermined_limit_is_not_an_ambiguous_order(self) -> None:
        # They are not two names for one thing, and the difference decides how
        # each is counted. Ambiguous order returns the *same rows* and is
        # counted as agreement; an undetermined limit returns *different rows*
        # and is excluded, because nothing about the data follows from it.
        assert Outcome.UNDETERMINED_LIMIT is not Outcome.AMBIGUOUS_ORDER

    def test_an_undetermined_limit_leaves_the_denominator(self) -> None:
        report = VerificationReport(db_id="d", schema="s")
        report.checks = [
            QueryCheck("q1", Outcome.MATCH),
            QueryCheck("q2", Outcome.UNDETERMINED_LIMIT),
        ]
        assert report.comparable == 1
        assert report.unscoreable == 1
        assert report.verified is True


class TestGoldEntries:
    """What verification hands the eval harness.

    This is the join between two numbers that must not drift apart: conversion
    fidelity is *matched / comparable*, and execution accuracy is measured over
    the questions `comparable` counts. One definition, exported once.
    """

    def report(self) -> VerificationReport:
        report = VerificationReport(db_id="concert_singer", schema="spider_concert_singer")
        report.checks = [
            QueryCheck("q1", Outcome.MATCH, postgres_sql="SELECT 1"),
            QueryCheck("q2", Outcome.AMBIGUOUS_ORDER, postgres_sql="SELECT 2"),
            QueryCheck("q3", Outcome.MISMATCH, postgres_sql="SELECT 3"),
            QueryCheck("q4", Outcome.UNDETERMINED_LIMIT, postgres_sql="SELECT 4"),
            QueryCheck("q5", Outcome.DIALECT_ERROR, postgres_sql="SELECT 5"),
            QueryCheck("q6", Outcome.GOLD_ERROR),
            QueryCheck("q7", Outcome.TRANSPILE_ERROR),
        ]
        return report

    def test_every_checked_question_gets_an_entry(self) -> None:
        """Including the unusable ones.

        Emitting only the scoreable questions would leave the harness unable to
        distinguish "verified and excluded, for this reason" from "never
        verified at all" -- and those demand opposite responses: report the
        first, refuse to start on the second.
        """
        entries = self.report().gold_entries()

        assert [entry["question_id"] for entry in entries] == [f"q{n}" for n in range(1, 8)]

    def test_scoreable_matches_the_comparable_set_exactly(self) -> None:
        report = self.report()

        scoreable = [entry["question_id"] for entry in report.gold_entries() if entry["scoreable"]]

        assert scoreable == ["q1", "q2", "q3"]
        assert len(scoreable) == report.comparable

    def test_an_excluded_entry_still_carries_its_reason(self) -> None:
        entries = {entry["question_id"]: entry for entry in self.report().gold_entries()}

        assert entries["q4"]["outcome"] == "undetermined_limit"
        assert entries["q5"]["outcome"] == "dialect_error"

    def test_the_statement_carried_across_is_the_one_that_was_verified(self) -> None:
        """Not re-transpiled later.

        A change to the transpiler would otherwise alter every reference answer
        with nothing re-checking it against SQLite, which is the one thing
        verification exists to prevent.
        """
        entries = {entry["question_id"]: entry for entry in self.report().gold_entries()}

        assert entries["q1"]["sql"] == "SELECT 1"

    def test_a_query_that_never_reached_postgres_carries_no_sql(self) -> None:
        entries = {entry["question_id"]: entry for entry in self.report().gold_entries()}

        assert entries["q6"]["sql"] == ""
        assert entries["q6"]["scoreable"] is False

    def test_the_schema_travels_with_every_entry(self) -> None:
        # The query runner needs it to set search_path; deriving it a second
        # time from db_id is how the two names come to disagree.
        assert all(
            entry["schema"] == "spider_concert_singer" for entry in self.report().gold_entries()
        )
