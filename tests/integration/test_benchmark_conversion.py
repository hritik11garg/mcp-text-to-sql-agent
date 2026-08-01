"""Converting a SQLite database into PostgreSQL, and proving it still answers.

Against a real PostgreSQL, because the whole point of the conversion is that a
different engine gives the same answers, and nothing but the other engine can
establish that.

The centrepiece is :class:`TestVerification`: gold queries executed on both
sides and compared with the eval harness's own comparator. A conversion defect
does not raise anywhere -- it lowers an accuracy number weeks later, and the
investigation that follows looks at the model.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from benchmark.convert import ConversionReport, convert_database, plan_database
from benchmark.sqlite_source import open_database
from benchmark.verify import Outcome, transpile_to_postgres, verify_database
from core.exceptions import ConversionError
from core.settings import BenchmarkSettings
from evals.dataset import Question

type Conn = psycopg.Connection[tuple[object, ...]]

SCHOOL = """
CREATE TABLE Student (
    StuID   INTEGER PRIMARY KEY,
    LName   TEXT NOT NULL,
    Age     INT,
    GPA     REAL,
    Advisor INTEGER
);
CREATE TABLE Enrolment (
    StuID     INTEGER REFERENCES Student(StuID),
    Course    TEXT,
    Grade     TEXT,
    PRIMARY KEY (StuID, Course)
);
INSERT INTO Student VALUES
    (1, 'Hopper',  35, 3.9,  7),
    (2, 'Lovelace',28, 3.75, 7),
    (3, 'Turing',  41, 3.2,  NULL);
INSERT INTO Enrolment VALUES
    (1, 'Compilers', 'A'),
    (1, 'Databases', 'B'),
    (2, 'Databases', 'A'),
    (3, 'Compilers', 'C');
"""

MESSY = """
CREATE TABLE Reading (
    id      INTEGER PRIMARY KEY,
    Value   INTEGER,
    Payload BLOB,
    Note    TEXT
);
INSERT INTO Reading VALUES (1, 5, X'00FF', 'ok');
INSERT INTO Reading VALUES (2, 'unavailable', X'0102', NULL);
"""


@pytest.fixture
def settings() -> BenchmarkSettings:
    return BenchmarkSettings()


@pytest.fixture
def loader_connection(postgres_url: str) -> Iterator[Conn]:
    """A dedicated owner connection, so a rolled-back conversion cannot affect
    the session-scoped fixtures every other integration test shares."""
    url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as connection:
        yield connection


@dataclass(frozen=True, slots=True)
class Loaded:
    """A converted database and the SQLite file it came from.

    The path is returned rather than rebuilt, because ``make_sqlite_db`` writes
    to a fixed location per ``db_id`` and calling it twice replays the script
    against a database that already has the tables.
    """

    schema: str
    report: ConversionReport
    path: Path


def _load(
    connection: Conn,
    make_sqlite_db: Callable[..., Path],
    settings: BenchmarkSettings,
    db_id: str,
    script: str,
    *,
    prefix: str = "test_",
) -> Loaded:
    path = make_sqlite_db(db_id, script)
    with open_database(path, db_id=db_id) as database:
        schema, plans = plan_database(database, settings=settings, prefix=prefix)
        report = convert_database(
            connection,
            database,
            schema=schema,
            plans=plans,
            settings=settings,
            readonly_role="sql_agent_ro",
            replace=True,
        )
    return Loaded(schema=schema, report=report, path=path)


class TestConversion:
    def test_tables_rows_and_types_land(
        self,
        loader_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        loaded = _load(loader_connection, make_sqlite_db, settings, "school", SCHOOL)

        rows = loader_connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s "
            "ORDER BY table_name",
            (loaded.schema,),
        ).fetchall()
        assert [row[0] for row in rows] == ["enrolment", "student"]

        types = loader_connection.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'student' ORDER BY ordinal_position",
            (loaded.schema,),
        ).fetchall()
        assert types == [
            ("stuid", "bigint"),
            ("lname", "text"),
            ("age", "bigint"),
            ("gpa", "double precision"),
            ("advisor", "bigint"),
        ]

    def test_a_composite_primary_key_survives(
        self,
        loader_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        loaded = _load(loader_connection, make_sqlite_db, settings, "school", SCHOOL)

        columns = loader_connection.execute(
            """
            SELECT a.attname
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
            WHERE n.nspname = %s AND c.relname = 'enrolment' AND con.contype = 'p'
            ORDER BY k.ord
            """,
            (loaded.schema,),
        ).fetchall()
        assert [row[0] for row in columns] == ["stuid", "course"]

    def test_foreign_keys_are_visible_to_introspection(
        self,
        loader_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        # They exist so the generator can see the join. If they vanished, the
        # loss would show up as worse accuracy on multi-table questions and
        # nowhere else.
        loaded = _load(loader_connection, make_sqlite_db, settings, "school", SCHOOL)

        count = loader_connection.execute(
            "SELECT count(*) FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND con.contype = 'f'",
            (loaded.schema,),
        ).fetchone()
        assert count is not None
        assert count[0] == 1

    def test_a_mixed_type_column_becomes_text_and_keeps_every_row(
        self,
        loader_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        # The alternative -- trusting the INTEGER declaration and dropping the
        # row that does not fit -- changes the answers rather than failing.
        loaded = _load(loader_connection, make_sqlite_db, settings, "messy", MESSY)

        values = loader_connection.execute(
            psycopg.sql.SQL("SELECT value FROM {}.reading ORDER BY id").format(
                psycopg.sql.Identifier(loaded.schema)
            )
        ).fetchall()
        assert [row[0] for row in values] == ["5", "unavailable"]

    def test_blobs_round_trip_as_bytea(
        self,
        loader_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        loaded = _load(loader_connection, make_sqlite_db, settings, "messy", MESSY)

        payload = loader_connection.execute(
            psycopg.sql.SQL("SELECT payload FROM {}.reading WHERE id = 1").format(
                psycopg.sql.Identifier(loaded.schema)
            )
        ).fetchone()
        assert payload is not None
        assert bytes(payload[0]) == b"\x00\xff"

    def test_reloading_without_replace_is_refused(
        self,
        loader_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        loaded = _load(loader_connection, make_sqlite_db, settings, "school", SCHOOL)
        with open_database(loaded.path, db_id="school") as database:
            schema, plans = plan_database(database, settings=settings, prefix="test_")
            with pytest.raises(ConversionError, match="already exists"):
                convert_database(
                    loader_connection,
                    database,
                    schema=schema,
                    plans=plans,
                    settings=settings,
                    readonly_role="sql_agent_ro",
                    replace=False,
                )

    def test_the_report_names_the_coerced_column(
        self,
        loader_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        loaded = _load(loader_connection, make_sqlite_db, settings, "messy", MESSY)
        assert "reading.value:text" in loaded.report.coerced_columns


class TestReadOnlyBoundary:
    def test_the_readonly_role_can_select_from_a_converted_schema(
        self,
        loader_connection: Conn,
        ro_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        # Migration 002 grants on `public` only, so without the loader's own
        # grant a converted database is invisible and every generated query
        # against it fails on permissions.
        loaded = _load(loader_connection, make_sqlite_db, settings, "school", SCHOOL)

        rows = ro_connection.execute(
            psycopg.sql.SQL("SELECT count(*) FROM {}.student").format(
                psycopg.sql.Identifier(loaded.schema)
            )
        ).fetchone()
        assert rows is not None
        assert rows[0] == 3

    def test_the_readonly_role_still_cannot_write_to_it(
        self,
        loader_connection: Conn,
        ro_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        # The load grants USAGE and SELECT. A loader that widened this to make
        # something work would remove the boundary every security claim rests
        # on, and nothing else in the system would notice.
        loaded = _load(loader_connection, make_sqlite_db, settings, "school", SCHOOL)

        with pytest.raises(psycopg.errors.Error):
            ro_connection.execute(
                psycopg.sql.SQL("INSERT INTO {}.student (stuid) VALUES (99)").format(
                    psycopg.sql.Identifier(loaded.schema)
                )
            )

    def test_the_readonly_role_cannot_create_tables_in_it(
        self,
        loader_connection: Conn,
        ro_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        loaded = _load(loader_connection, make_sqlite_db, settings, "school", SCHOOL)

        with pytest.raises(psycopg.errors.Error):
            ro_connection.execute(
                psycopg.sql.SQL("CREATE TABLE {}.smuggled (a int)").format(
                    psycopg.sql.Identifier(loaded.schema)
                )
            )


class TestVerification:
    """Gold queries must return identical results on both engines."""

    def _questions(self, pairs: list[tuple[str, str]]) -> list[Question]:
        return [
            Question(question_id=f"q{n}", question=text, gold_sql=sql, db_id="school")
            for n, (text, sql) in enumerate(pairs)
        ]

    def test_matching_gold_queries_verify(
        self,
        loader_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        loaded = _load(loader_connection, make_sqlite_db, settings, "school", SCHOOL)
        questions = self._questions(
            [
                ("How many students?", "SELECT count(*) FROM Student"),
                ("Oldest student?", "SELECT LName FROM Student ORDER BY Age DESC LIMIT 1"),
                ("Average GPA?", "SELECT avg(GPA) FROM Student"),
                (
                    "Who took Databases?",
                    "SELECT s.LName FROM Student s JOIN Enrolment e ON s.StuID = e.StuID "
                    "WHERE e.Course = 'Databases' ORDER BY s.LName",
                ),
                ("Students with no advisor?", "SELECT count(*) FROM Student WHERE Advisor IS NULL"),
            ]
        )

        with open_database(loaded.path, db_id="school") as database:
            report = verify_database(
                loader_connection,
                database,
                questions,
                schema=loaded.schema,
                statement_timeout_ms=10_000,
            )

        assert report.counts()["mismatch"] == 0
        assert report.comparable == len(questions)
        assert report.verified is True

    def test_a_gold_query_that_fails_on_sqlite_is_not_blamed_on_the_conversion(
        self,
        loader_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        # A benchmark reference query that does not run on the database it
        # shipped with says nothing about the conversion, and counting it as a
        # conversion failure would send the investigation to the wrong place.
        loaded = _load(loader_connection, make_sqlite_db, settings, "school", SCHOOL)
        questions = self._questions([("Broken", "SELECT nope FROM Student")])
        with open_database(loaded.path, db_id="school") as database:
            report = verify_database(
                loader_connection,
                database,
                questions,
                schema=loaded.schema,
                statement_timeout_ms=10_000,
            )

        assert report.checks[0].outcome is Outcome.GOLD_ERROR
        assert report.comparable == 0
        # Nothing was compared, so nothing was verified either.
        assert report.verified is False

    def test_a_genuine_engine_difference_is_caught_and_fails_the_database(
        self,
        loader_connection: Conn,
        make_sqlite_db: Callable[..., Path],
        settings: BenchmarkSettings,
    ) -> None:
        # `LIKE` is case-insensitive for ASCII in SQLite and case-sensitive in
        # PostgreSQL. Both queries run cleanly on both engines and return
        # different rows -- which is exactly the shape of defect this whole
        # module exists for: nothing raises, an accuracy number just drops.
        loaded = _load(loader_connection, make_sqlite_db, settings, "school", SCHOOL)
        questions = self._questions(
            [
                ("How many students?", "SELECT count(*) FROM Student"),
                ("Anyone called hopper?", "SELECT LName FROM Student WHERE LName LIKE 'hopper'"),
            ]
        )

        with open_database(loaded.path, db_id="school") as database:
            report = verify_database(
                loader_connection,
                database,
                questions,
                schema=loaded.schema,
                statement_timeout_ms=10_000,
            )

        assert report.comparable == 2
        assert report.matched == 1
        # One disagreement is enough. "Most queries reproduced" is not a
        # property anyone can act on: the affected questions are unknown until
        # someone looks at the one that did not.
        assert report.verified is False
        assert [check.outcome for check in report.checks] == [Outcome.MATCH, Outcome.MISMATCH]


class TestTranspile:
    def test_rewrites_sqlite_specific_syntax(self) -> None:
        assert "||" in transpile_to_postgres("SELECT LName || 'x' FROM Student")

    def test_an_unparseable_query_raises_rather_than_returning_something(self) -> None:
        # A transpile failure has to be a distinct outcome from a mismatch:
        # one says the benchmark holds a query this project cannot parse, the
        # other says the data moved. Returning the original string would
        # collapse them and send the investigation to the wrong component.
        with pytest.raises(ValueError, match=r".+"):
            transpile_to_postgres("SELECT FROM WHERE ((((")
