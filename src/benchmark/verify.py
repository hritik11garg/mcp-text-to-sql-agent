"""Proving the conversion preserved every answer, rather than assuming it did.

Every gold query is executed against both the SQLite original and the converted
PostgreSQL schema, and the two result sets are compared. DATASETS.md section 3
required this before any of it was built, and the reason is that a conversion
defect is invisible from downstream: a column that silently became text, a
foreign key that changed a join's row count, a date format that reorders a
``MAX`` -- each produces a *lower accuracy number*, not an error, and the
investigation that follows looks at the model.

**The comparator is the eval harness's own.** Not a stricter one written for
this purpose. The question being answered is not "are the two databases
identical" -- they are not, one is SQLite -- but "will the eval score a correct
answer as correct on the converted copy". Only :func:`evals.comparison.compare`
can answer that, because it is the thing that will do the scoring. Using a
stricter comparison here would fail conversions that the eval would have been
perfectly happy with; using a looser one would pass conversions the eval will
mark wrong.

**Gold queries are transpiled, not rewritten by hand.** sqlglot reads SQLite and
writes PostgreSQL. A transpile failure is recorded as such and is a distinct
finding from a mismatch: one says the benchmark contains a query this project
cannot parse, the other says the data moved. Collapsing them would send the
investigation to the wrong place.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import psycopg
import sqlglot
from psycopg import Connection, sql

from benchmark.sqlite_source import SqliteDatabase
from evals.comparison import Verdict, compare
from evals.dataset import Question

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    """Why one gold query did or did not confirm the conversion."""

    MATCH = "match"
    MISMATCH = "mismatch"
    GOLD_ERROR = "gold_error"
    """The reference query failed on its *own* SQLite database. Nothing about
    the conversion can be concluded from it, and it is not counted against it --
    the same rule EVALUATION.md section 5 applies to eval scoring."""

    TRANSPILE_ERROR = "transpile_error"
    POSTGRES_ERROR = "postgres_error"


@dataclass(frozen=True, slots=True)
class QueryCheck:
    question_id: str
    outcome: Outcome
    verdict: Verdict | None = None
    detail: str = ""


@dataclass(slots=True)
class VerificationReport:
    """Per-database verification, with the counts an operator has to act on."""

    db_id: str
    schema: str
    checks: list[QueryCheck] = field(default_factory=list)

    @property
    def comparable(self) -> int:
        """Queries that ran on both sides. The denominator that means anything."""
        return sum(1 for check in self.checks if check.outcome in (Outcome.MATCH, Outcome.MISMATCH))

    @property
    def matched(self) -> int:
        return sum(1 for check in self.checks if check.outcome is Outcome.MATCH)

    @property
    def verified(self) -> bool:
        """A conversion is verified only if every comparable query agreed.

        Not "most of them". A single mismatch is a class of data that moved, and
        the questions it affects are unknown until someone looks at it.
        """
        return self.comparable > 0 and self.matched == self.comparable

    def counts(self) -> dict[str, int]:
        tally = dict.fromkeys((outcome.value for outcome in Outcome), 0)
        for check in self.checks:
            tally[check.outcome.value] += 1
        return tally

    def as_dict(self) -> dict[str, Any]:
        return {
            "db_id": self.db_id,
            "schema": self.schema,
            "verified": self.verified,
            "comparable": self.comparable,
            "matched": self.matched,
            "counts": self.counts(),
            "failures": [
                {
                    "question_id": check.question_id,
                    "outcome": check.outcome.value,
                    "verdict": check.verdict.value if check.verdict else None,
                    "detail": check.detail,
                }
                for check in self.checks
                if check.outcome not in (Outcome.MATCH, Outcome.GOLD_ERROR)
            ],
        }


def transpile_to_postgres(gold_sql: str) -> str:
    """Rewrite one SQLite query for PostgreSQL.

    Raises:
        ValueError: The query could not be parsed or rendered, with the
            original message. Callers record this as its own outcome rather
            than as a mismatch.
    """
    try:
        rendered = sqlglot.transpile(gold_sql, read="sqlite", write="postgres")
    except Exception as exc:  # sqlglot raises several unrelated types
        raise ValueError(str(exc)) from exc
    if not rendered:
        raise ValueError("transpiled to an empty statement")
    return rendered[0]


def verify_database(
    connection: Connection[Any],
    database: SqliteDatabase,
    questions: Sequence[Question],
    *,
    schema: str,
    statement_timeout_ms: int,
) -> VerificationReport:
    """Run every gold query on both engines and compare.

    ``search_path`` is set to the converted schema for the session, so gold SQL
    referring to bare table names resolves without being rewritten. It is set
    with a bound parameter through ``set_config`` rather than composed into a
    ``SET``, because the value derives from a benchmark-supplied ``db_id``.

    Args:
        connection: An owner or read-only connection to the converted database.
        database: The SQLite original, still open.
        questions: Gold questions for this ``db_id`` only. Passing questions for
            another database would compare against the wrong schema and report
            the conversion as broken.
    """
    report = VerificationReport(db_id=database.db_id, schema=schema)

    connection.execute("SELECT set_config('search_path', %s, false)", (schema,))
    connection.execute(
        "SELECT set_config('statement_timeout', %s, false)", (str(statement_timeout_ms),)
    )

    for question in questions:
        report.checks.append(
            _check_one(connection, database, question, statement_timeout_ms=statement_timeout_ms)
        )

    logger.info(
        "%s: %d/%d gold queries reproduced", database.db_id, report.matched, report.comparable
    )
    return report


def _check_one(
    connection: Connection[Any],
    database: SqliteDatabase,
    question: Question,
    *,
    statement_timeout_ms: int,
) -> QueryCheck:
    try:
        expected = database.execute(question.gold_sql)
    except sqlite3.Error as exc:
        # The reference query does not run on the database it shipped with.
        # That is a benchmark defect and says nothing about the conversion.
        return QueryCheck(question.question_id, Outcome.GOLD_ERROR, detail=str(exc))

    try:
        translated = transpile_to_postgres(question.gold_sql)
    except ValueError as exc:
        return QueryCheck(question.question_id, Outcome.TRANSPILE_ERROR, detail=str(exc))

    try:
        with connection.transaction():
            actual = _run(connection, translated)
    except psycopg.Error as exc:
        return QueryCheck(
            question.question_id,
            Outcome.POSTGRES_ERROR,
            detail=str(exc).splitlines()[0],
        )

    # Argument order is load-bearing: the SQLite result is the reference, so it
    # is `gold`. Swapping them would still compare equal sets, but every
    # asymmetric diagnostic in the Comparison would describe the wrong side.
    result = compare(actual, expected, gold_sql=question.gold_sql)
    if result.matched:
        return QueryCheck(question.question_id, Outcome.MATCH, verdict=result.verdict)
    return QueryCheck(
        question.question_id,
        Outcome.MISMATCH,
        verdict=result.verdict,
        detail=result.detail,
    )


def _run(connection: Connection[Any], statement: str) -> list[tuple[Any, ...]]:
    """Execute transpiled gold SQL.

    ``sql.SQL(statement)`` wraps a string that came from a benchmark file. That
    is safe *here* and nowhere else in this project: verification runs offline,
    against a database whose entire contents are the benchmark itself, and the
    thing being verified is precisely whether these statements behave the same
    on both engines -- so they cannot be parameterised, validated, or rewritten
    without changing what is under test. The generated-SQL path, which does face
    untrusted input, goes through validation and the read-only role instead.
    """
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL(statement))
        if cursor.description is None:
            return []
        return [tuple(row) for row in cursor.fetchall()]


__all__ = [
    "Outcome",
    "QueryCheck",
    "VerificationReport",
    "transpile_to_postgres",
    "verify_database",
]
