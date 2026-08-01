"""Conversion planning, split assignment, and question readers.

All of it decidable without a database. The type-inference tests in particular
are the ones that decide whether a gold query still means what it meant: a
column that becomes ``text`` when it should have been ``bigint`` does not fail
the load, it changes the answers.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from benchmark import readers, splits
from benchmark.convert import (
    BIGINT,
    BYTEA,
    DOUBLE,
    TEXT,
    adapt,
    infer_pg_type,
    plan_database,
    schema_name_for,
    sqlite_affinity,
)
from benchmark.sqlite_source import open_database
from core.exceptions import BenchmarkError, ConversionError, UnsafeIdentifierError
from core.settings import BenchmarkSettings
from evals.dataset import Question, Split

CONCERT = """
CREATE TABLE Stadium (
    Stadium_ID INTEGER PRIMARY KEY,
    Name TEXT,
    Capacity INT,
    Average REAL
);
CREATE TABLE Singer (
    Singer_ID INTEGER PRIMARY KEY,
    Name TEXT,
    Stadium_ID INTEGER REFERENCES Stadium(Stadium_ID)
);
INSERT INTO Stadium VALUES (1, 'Hampden', 52000, 31.4), (2, 'Ibrox', 50817, 29.0);
INSERT INTO Singer VALUES (1, 'Ada', 1), (2, 'Grace', 2);
"""


class TestTypeInference:
    @pytest.mark.parametrize(
        ("declared", "affinity"),
        [
            ("INTEGER", "INTEGER"),
            ("int", "INTEGER"),
            ("VARCHAR(20)", "TEXT"),
            ("CLOB", "TEXT"),
            ("", "BLOB"),
            ("BLOB", "BLOB"),
            ("REAL", "REAL"),
            ("DOUBLE PRECISION", "REAL"),
            ("DECIMAL(10,5)", "NUMERIC"),
            # SQLite's rules are ordered, not semantic: POINT contains "INT".
            ("POINT", "INTEGER"),
        ],
    )
    def test_affinity_follows_sqlites_own_rules(self, declared: str, affinity: str) -> None:
        assert sqlite_affinity(declared) == affinity

    def test_all_integers_become_bigint(self) -> None:
        assert infer_pg_type([1, 2, 3], declared="INTEGER") == BIGINT

    def test_mixed_integers_and_floats_widen_to_double(self) -> None:
        assert infer_pg_type([1, 2.5], declared="INTEGER") == DOUBLE

    def test_text_in_a_column_declared_integer_wins(self) -> None:
        # SQLite does not enforce declared types and benchmark data exploits
        # that. Trusting the declaration here would fail the load on one row in
        # a hundred thousand -- or, worse, drop it.
        assert infer_pg_type([1, 2, "unknown"], declared="INTEGER") == TEXT

    def test_nulls_are_ignored_as_evidence(self) -> None:
        assert infer_pg_type([None, 1, None], declared="INTEGER") == BIGINT

    def test_an_all_null_column_falls_back_to_the_declaration(self) -> None:
        assert infer_pg_type([None, None], declared="VARCHAR(10)") == TEXT
        assert infer_pg_type([None], declared="INTEGER") == BIGINT

    def test_an_empty_column_falls_back_to_the_declaration(self) -> None:
        assert infer_pg_type([], declared="REAL") == DOUBLE

    def test_blobs_become_bytea(self) -> None:
        assert infer_pg_type([b"\x00\x01"], declared="BLOB") == BYTEA

    def test_blobs_mixed_with_numbers_have_no_common_type_and_become_text(self) -> None:
        assert infer_pg_type([b"\x00", 1], declared="BLOB") == TEXT


class TestAdapt:
    def test_none_stays_none(self) -> None:
        assert adapt(None, TEXT) is None

    def test_a_number_landing_in_a_text_column_is_stringified(self) -> None:
        assert adapt(5, TEXT) == "5"

    def test_a_blob_landing_in_a_text_column_is_decoded_not_hex_encoded(self) -> None:
        # Passed through unchanged, psycopg would send a bytea literal and the
        # column would hold "\x6869" rather than "hi" -- which no gold query
        # would ever match, and nothing would report as an error.
        assert adapt(b"hi", TEXT) == "hi"

    def test_undecodable_bytes_in_a_text_column_are_replaced_not_raised(self) -> None:
        assert adapt(b"\xff", TEXT) == "�"

    def test_numeric_targets_coerce(self) -> None:
        assert adapt("7", BIGINT) == 7
        assert adapt(1, DOUBLE) == 1.0


class TestPlanning:
    def test_plans_lowercased_names_and_inferred_types(
        self, make_sqlite_db: Callable[..., Path], benchmark_settings: BenchmarkSettings
    ) -> None:
        path = make_sqlite_db("concert_singer", CONCERT)

        with open_database(path, db_id="concert_singer") as database:
            schema, plans = plan_database(database, settings=benchmark_settings)

        assert schema == "concert_singer"
        by_name = {plan.target_name: plan for plan in plans}
        assert set(by_name) == {"stadium", "singer"}

        stadium = by_name["stadium"]
        assert stadium.source_name == "Stadium"
        assert [column.target_name for column in stadium.columns] == [
            "stadium_id",
            "name",
            "capacity",
            "average",
        ]
        assert [column.pg_type for column in stadium.columns] == [BIGINT, TEXT, BIGINT, DOUBLE]
        assert stadium.primary_key == ("stadium_id",)

    def test_foreign_keys_survive_case_folding(
        self, make_sqlite_db: Callable[..., Path], benchmark_settings: BenchmarkSettings
    ) -> None:
        # `REFERENCES Stadium` has to resolve against the table planned as
        # `stadium`. Matching the raw strings would drop every constraint the
        # source engine honours, and the generator would lose every join.
        path = make_sqlite_db("concert_singer", CONCERT)

        with open_database(path, db_id="concert_singer") as database:
            _, plans = plan_database(database, settings=benchmark_settings)

        singer = next(plan for plan in plans if plan.target_name == "singer")
        assert singer.foreign_keys == ((("stadium_id",), "stadium", ("stadium_id",)),)

    def test_sqlite_itself_prevents_a_case_only_table_collision(
        self, make_sqlite_db: Callable[..., Path]
    ) -> None:
        # Worth pinning down rather than assuming either way. The collision
        # guard in IdentifierMap was written for this case and this case cannot
        # reach it: SQLite compares table names case-insensitively, so a source
        # database holding both `Song` and `song` does not exist. The guard
        # stays -- it is cheap, and it still covers a set of db_ids assembled
        # from directory names on a case-sensitive filesystem -- but it is
        # defence behind the source engine, not the only thing standing there.
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            make_sqlite_db("collide", 'CREATE TABLE "Song" (a INT); CREATE TABLE "song" (b INT);')

    def test_a_name_too_long_for_postgres_refuses_the_database(
        self, make_sqlite_db: Callable[..., Path], benchmark_settings: BenchmarkSettings
    ) -> None:
        # This one *is* reachable: SQLite has no identifier length limit and
        # PostgreSQL truncates at 63 bytes rather than erroring, so two long
        # names sharing a prefix would silently become one table.
        long_name = "t" * 70
        path = make_sqlite_db("long", f'CREATE TABLE "{long_name}" (a INT);')
        with (
            open_database(path, db_id="long") as database,
            pytest.raises(UnsafeIdentifierError, match="truncate"),
        ):
            plan_database(database, settings=benchmark_settings)

    def test_a_database_with_no_tables_is_refused(
        self, make_sqlite_db: Callable[..., Path], benchmark_settings: BenchmarkSettings
    ) -> None:
        path = make_sqlite_db("empty", "CREATE VIEW v AS SELECT 1;")
        with (
            open_database(path, db_id="empty") as database,
            pytest.raises(ConversionError, match="no ordinary tables"),
        ):
            plan_database(database, settings=benchmark_settings)

    def test_type_inference_sees_a_value_far_past_any_sample_window(
        self, make_sqlite_db: Callable[..., Path], benchmark_settings: BenchmarkSettings
    ) -> None:
        # The bug this replaced a sampling cap to fix. Spider's `wta_1.rankings`
        # has 510,437 rows and exactly one empty-string `player_id` at rowid
        # 1,593,272 -- past a 200,000-row sample. The column was inferred
        # `bigint`, the load ran, and it died on that single value.
        #
        # `group_concat(DISTINCT typeof(col))` is exact over the whole column,
        # so position in the table cannot change the answer.
        values = ", ".join(f"({n})" for n in range(500))
        script = (
            "CREATE TABLE t (a INT); "  # noqa: S608
            f"INSERT INTO t VALUES {values}; "
            "INSERT INTO t VALUES ('');"
        )
        path = make_sqlite_db("late_outlier", script)

        with open_database(path, db_id="late_outlier") as database:
            _, plans = plan_database(database, settings=benchmark_settings)

        assert plans[0].columns[0].pg_type == TEXT

    def test_views_and_virtual_tables_are_not_converted(
        self, make_sqlite_db: Callable[..., Path], benchmark_settings: BenchmarkSettings
    ) -> None:
        path = make_sqlite_db(
            "mixed",
            "CREATE TABLE t (a INT); CREATE VIEW v AS SELECT a FROM t;",
        )
        with open_database(path, db_id="mixed") as database:
            _, plans = plan_database(database, settings=benchmark_settings)

        assert [plan.target_name for plan in plans] == ["t"]


class TestSchemaNaming:
    def test_a_prefix_separates_two_benchmarks(self) -> None:
        # Spider and BIRD both ship a database called `movie`; loading the
        # second over the first would silently replace it.
        assert schema_name_for("movie", prefix="spider_") == "spider_movie"
        assert schema_name_for("movie", prefix="bird_") == "bird_movie"

    def test_a_prefix_that_overflows_the_limit_is_refused(self) -> None:
        with pytest.raises(UnsafeIdentifierError):
            schema_name_for("a" * 60, prefix="spider_")


class TestSplits:
    def test_assignment_is_stable_across_processes(self) -> None:
        first = splits.assign(["alpha", "beta", "gamma", "delta"])
        second = splits.assign(["delta", "gamma", "beta", "alpha"])
        assert first == second

    def test_adding_a_database_does_not_move_the_others(self) -> None:
        # The property a seeded shuffle does not have, and the reason this is a
        # hash. A moved database means training on what used to be held out,
        # and the split file looks just as deterministic as before.
        before = splits.assign([f"db{n}" for n in range(30)])
        after = splits.assign([f"db{n}" for n in range(40)])
        assert all(after[name] == split for name, split in before.items())

    def test_a_different_seed_produces_a_different_assignment(self) -> None:
        default = splits.assign([f"db{n}" for n in range(50)])
        reseeded = splits.assign(
            [f"db{n}" for n in range(50)], policy=splits.SplitPolicy(seed="other")
        )
        assert default != reseeded

    def test_smoke_membership_is_also_stable_under_growth(self) -> None:
        # The bug this caught: smoke used to be "the five lowest-bucket dev
        # databases", which is a rank within a set, so a newly added database
        # could displace one and the per-commit check would silently start
        # measuring something else.
        before = splits.assign([f"db{n}" for n in range(200)])
        after = splits.assign([f"db{n}" for n in range(400)])
        smoke_before = {name for name, split in before.items() if split is Split.SMOKE}
        assert smoke_before
        assert all(after[name] is Split.SMOKE for name in smoke_before)

    def test_smoke_comes_out_of_dev_never_out_of_held_out(self) -> None:
        # Smoke runs per commit. Taking it from held-out would mean the set
        # reserved for reported numbers is touched on every push; taking it
        # from train would measure schemas the retriever was fitted to.
        strict = splits.SplitPolicy(train=0.70, dev=0.15, held_out=0.15, smoke=0.15)
        assignment = splits.assign([f"db{n}" for n in range(500)], policy=strict)
        assert Split.DEV not in assignment.values()
        assert sum(1 for split in assignment.values() if split is Split.HELD_OUT) > 0

    def test_smoke_cannot_exceed_the_dev_share(self) -> None:
        with pytest.raises(ValueError, match="carved out of dev"):
            splits.SplitPolicy(smoke=0.5)

    def test_proportions_are_approximately_honoured(self) -> None:
        assignment = splits.assign([f"db{n}" for n in range(1000)])
        held_out = sum(1 for split in assignment.values() if split is Split.HELD_OUT)
        assert 100 < held_out < 200

    def test_proportions_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError, match=r"sum to 1\.0"):
            splits.SplitPolicy(train=0.5, dev=0.3, held_out=0.3)

    def test_apply_stamps_each_question(self) -> None:
        assignment = {"alpha": Split.HELD_OUT}
        question = Question(question_id="q1", question="?", gold_sql="SELECT 1", db_id="alpha")
        assert splits.apply([question], assignment)[0].split is Split.HELD_OUT

    def test_an_unassigned_database_raises_rather_than_defaulting_to_dev(self) -> None:
        question = Question(question_id="q1", question="?", gold_sql="SELECT 1", db_id="ghost")
        with pytest.raises(KeyError, match="no split assignment"):
            splits.apply([question], {"alpha": Split.TRAIN})

    def test_summarise_counts_databases_and_questions(self) -> None:
        assignment = {"alpha": Split.DEV, "beta": Split.DEV}
        questions = [
            Question(question_id="q1", question="?", gold_sql="SELECT 1", db_id="alpha"),
            Question(question_id="q2", question="?", gold_sql="SELECT 1", db_id="beta"),
            Question(question_id="q3", question="?", gold_sql="SELECT 1", db_id="beta"),
        ]
        summary = splits.summarise(assignment, questions)
        assert summary["dev"] == {"databases": 2, "questions": 3}


class TestReaders:
    def test_spider_records_become_questions(self, tmp_path: Path) -> None:
        path = tmp_path / "dev.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "db_id": "concert_singer",
                        "question": "How many?",
                        "query": "SELECT count(*)",
                    },
                    {"db_id": "car_1", "question": "Which?", "query": "SELECT name"},
                ]
            ),
            encoding="utf-8",
        )
        questions = readers.read_spider(path)

        assert [q.question_id for q in questions] == ["spider:dev:00000", "spider:dev:00001"]
        assert questions[0].gold_sql == "SELECT count(*)"
        assert questions[0].db_id == "concert_singer"

    def test_bird_records_keep_their_own_ids(self, tmp_path: Path) -> None:
        path = tmp_path / "dev.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "question_id": 42,
                        "db_id": "california_schools",
                        "question": "How many?",
                        "SQL": "SELECT count(*)",
                        "evidence": "eligible free rate = free/total",
                    }
                ]
            ),
            encoding="utf-8",
        )
        questions = readers.read_bird(path)

        assert questions[0].question_id == "bird:42"
        assert questions[0].gold_sql == "SELECT count(*)"
        # `evidence` is a human-written hint naming the columns involved.
        # Carrying it into the question would measure the model plus an oracle.
        assert "eligible free rate" not in questions[0].question

    def test_a_missing_gold_query_names_the_record(self, tmp_path: Path) -> None:
        path = tmp_path / "dev.json"
        path.write_text(json.dumps([{"db_id": "x", "question": "?"}]), encoding="utf-8")
        with pytest.raises(BenchmarkError, match=r"\[0\] has no 'query'"):
            readers.read_spider(path)

    def test_a_file_that_is_not_an_array_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "dev.json"
        path.write_text(json.dumps({"questions": []}), encoding="utf-8")
        with pytest.raises(BenchmarkError, match="JSON array"):
            readers.read_spider(path)

    def test_find_databases_matches_on_the_folder_name(
        self, tmp_path: Path, make_sqlite_db: Callable[..., Path]
    ) -> None:
        root = tmp_path / "database"
        make_sqlite_db("concert_singer", "CREATE TABLE t (a INT);", root=root)
        # A stray extra file in the folder must not be picked instead.
        (root / "concert_singer" / "extra.sqlite").write_bytes(b"")

        found = readers.find_databases(root)
        assert set(found) == {"concert_singer"}
        assert found["concert_singer"].name == "concert_singer.sqlite"

    def test_a_missing_root_is_empty_rather_than_an_error(self, tmp_path: Path) -> None:
        assert readers.find_databases(tmp_path / "nope") == {}
