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
from benchmark.verify import Outcome, schema_names, transpile_to_postgres
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
