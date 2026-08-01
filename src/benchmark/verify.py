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
from sqlglot import expressions as exp

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

    DIALECT_ERROR = "dialect_error"
    """The gold query is not expressible in PostgreSQL, against any conversion.

    Overwhelmingly SQLite's type affinity. ``concert.Year`` is a TEXT column
    holding ``'2014'``, and the gold query says ``WHERE Year = 2014``; SQLite
    applies numeric affinity and matches, PostgreSQL answers ``operator does
    not exist: text = integer``. Storing the column as text is the *faithful*
    conversion -- the value really is text, and ``SELECT Year`` really does
    return a string -- so there is nothing here for a converter to fix.

    Same category: ``SELECT name, count(*) ... GROUP BY id``, which SQLite
    permits and PostgreSQL rejects.

    Counted and excluded from the verified denominator, exactly as
    :attr:`GOLD_ERROR` is. These questions have no PostgreSQL answer at all, so
    they cannot be scored later either -- and an eval that silently dropped
    them would report a denominator it did not earn.
    """

    AMBIGUOUS_ORDER = "ambiguous_order"
    """Same rows, different order, and the gold query never determined the order.

    ``SELECT name FROM employee ORDER BY age`` where three employees are aged
    29. Both engines sort the ages identically; neither guarantees anything
    about the *names* within a tie, and they break the tie differently.

    Inferred, not guessed: both sides ran the **same** gold query, and the row
    multisets are equal. When the same query over the same data returns the
    same rows in a different order, the ordering was never determined -- that
    is a property of the benchmark query, not of the conversion.

    The inference is only valid here. In the eval, predicted and gold are
    *different* queries, so equal multisets in a different order can genuinely
    mean a wrong answer; `evals.comparison` therefore keeps the strict rule and
    is deliberately not touched by this.
    """

    POSTGRES_ERROR = "postgres_error"
    """PostgreSQL rejected the query for a reason the conversion is responsible
    for -- a missing table or column. Unlike a dialect error, this one means
    something did not get created."""


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
        return sum(
            1
            for check in self.checks
            if check.outcome in (Outcome.MATCH, Outcome.MISMATCH, Outcome.AMBIGUOUS_ORDER)
        )

    @property
    def matched(self) -> int:
        return sum(
            1 for check in self.checks if check.outcome in (Outcome.MATCH, Outcome.AMBIGUOUS_ORDER)
        )

    @property
    def unscoreable(self) -> int:
        """Queries with no PostgreSQL answer: benchmark defects and dialect gaps.

        Surfaced because these questions cannot be scored *later* either. An
        eval that quietly dropped them would divide by a denominator it did not
        earn -- the same reason EVALUATION.md section 5 counts gold errors.
        """
        return sum(
            1
            for check in self.checks
            if check.outcome in (Outcome.GOLD_ERROR, Outcome.DIALECT_ERROR)
        )

    @property
    def ambiguous(self) -> int:
        """Queries whose gold ORDER BY does not determine the row order."""
        return sum(1 for check in self.checks if check.outcome is Outcome.AMBIGUOUS_ORDER)

    @property
    def verified(self) -> bool:
        """A conversion is verified only if every comparable query agreed.

        Not "most of them". A single mismatch is a class of data that moved, and
        the questions it affects are unknown until someone looks at it.

        A dialect error is not counted against it. Those queries fail against a
        *perfect* conversion too, so treating them as conversion defects would
        make the flag unachievable and meaningless.
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
            "unscoreable": self.unscoreable,
            "ambiguous_order": self.ambiguous,
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


def transpile_to_postgres(gold_sql: str, *, known_names: frozenset[str] = frozenset()) -> str:
    """Rewrite one SQLite query for PostgreSQL.

    ``known_names`` is every table and column name in the source database,
    lower-cased. Supplying it enables the double-quote repair below; without it
    the query is transpiled as written.

    Raises:
        ValueError: The query could not be parsed or rendered, with the
            original message. Callers record this as its own outcome rather
            than as a mismatch.
    """
    try:
        parsed = sqlglot.parse(gold_sql, read="sqlite")
        # Narrowed rather than cast. sqlglot's `parse` is typed as returning
        # `Expr | None`, and anything that is not an `Expression` is not
        # something this can rewrite -- refusing says so instead of asserting
        # it away and failing somewhere less obvious.
        statements = [item for item in parsed if isinstance(item, exp.Expression)]
        rendered = [
            _repair_quoted_literals(statement, known_names).sql(dialect="postgres")
            for statement in statements
        ]
    except ValueError:
        raise
    except Exception as exc:  # sqlglot raises several unrelated types
        raise ValueError(str(exc)) from exc
    if not rendered:
        raise ValueError("transpiled to an empty statement")
    return rendered[0]


def _repair_quoted_literals(
    statement: exp.Expression, known_names: frozenset[str]
) -> exp.Expression:
    """Turn double-quoted tokens that name nothing back into string literals.

    **SQLite's own rule, applied deliberately rather than worked around.** The
    SQL standard says a double-quoted token is an identifier; SQLite falls back
    to treating it as a *string literal* when it matches no column or table in
    scope. Spider's gold SQL leans on that constantly --
    ``WHERE course = "Math"``, ``WHERE name LIKE "%w%"``, ``WHERE title =
    "Robbin CV"``.

    PostgreSQL has no such fallback and answers ``column "Math" does not
    exist``. Measured on Spider dev: **213 of 1034 questions**, and every single
    ``undefined_column`` failure in the run was this and nothing else.

    This is not a conversion defect and it is not a guess. The schema says
    exactly which names exist, so the same test SQLite applies can be applied
    here: quoted, and not a known name, therefore a string. A quoted token that
    *is* a real column is left alone -- which matters, because Spider genuinely
    has columns like ``Official_ratings_(millions)`` that must stay identifiers.
    """
    if not known_names:
        return statement

    for column in list(statement.find_all(exp.Column)):
        identifier = column.this
        if not isinstance(identifier, exp.Identifier) or not identifier.quoted:
            continue
        if column.args.get("table") is not None:
            # Qualified -- `t."x"` is unambiguously an identifier.
            continue
        if identifier.name.lower() in known_names:
            continue
        column.replace(exp.Literal.string(identifier.name))

    return statement


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
    known_names = schema_names(database)

    connection.execute("SELECT set_config('search_path', %s, false)", (schema,))
    connection.execute(
        "SELECT set_config('statement_timeout', %s, false)", (str(statement_timeout_ms),)
    )

    for question in questions:
        report.checks.append(
            _check_one(
                connection,
                database,
                question,
                statement_timeout_ms=statement_timeout_ms,
                known_names=known_names,
            )
        )

    logger.info(
        "%s: %d/%d gold queries reproduced", database.db_id, report.matched, report.comparable
    )
    return report


def schema_names(database: SqliteDatabase) -> frozenset[str]:
    """Every table and column name in the source, lower-cased.

    The evidence for :func:`_repair_quoted_literals`. Built from the source
    rather than the converted schema so it is available before anything is
    loaded, and so the names match what the gold SQL was written against.
    """
    names: set[str] = set()
    for table_name in database.table_names():
        names.add(table_name.lower())
        names.update(column.name.lower() for column in database.table(table_name).columns)
    return frozenset(names)


def _check_one(
    connection: Connection[Any],
    database: SqliteDatabase,
    question: Question,
    *,
    statement_timeout_ms: int,
    known_names: frozenset[str] = frozenset(),
) -> QueryCheck:
    try:
        expected = database.execute(question.gold_sql)
    except sqlite3.Error as exc:
        # The reference query does not run on the database it shipped with.
        # That is a benchmark defect and says nothing about the conversion.
        return QueryCheck(question.question_id, Outcome.GOLD_ERROR, detail=str(exc))

    try:
        translated = transpile_to_postgres(question.gold_sql, known_names=known_names)
    except ValueError as exc:
        return QueryCheck(question.question_id, Outcome.TRANSPILE_ERROR, detail=str(exc))

    try:
        with connection.transaction():
            actual = _run(connection, translated)
    except psycopg.Error as exc:
        return QueryCheck(
            question.question_id,
            _classify_pg_error(exc),
            detail=str(exc).splitlines()[0],
        )

    # Argument order is load-bearing: the SQLite result is the reference, so it
    # is `gold`. Swapping them would still compare equal sets, but every
    # asymmetric diagnostic in the Comparison would describe the wrong side.
    result = compare(actual, expected, gold_sql=question.gold_sql)
    if result.matched:
        return QueryCheck(question.question_id, Outcome.MATCH, verdict=result.verdict)

    if result.order_enforced:
        unordered = compare(actual, expected, order_matters=False)
        if unordered.matched:
            return QueryCheck(
                question.question_id,
                Outcome.AMBIGUOUS_ORDER,
                verdict=result.verdict,
                detail="same rows; the gold ORDER BY does not determine their order",
            )

    return QueryCheck(
        question.question_id,
        Outcome.MISMATCH,
        verdict=result.verdict,
        detail=result.detail,
    )


_CONVERSION_FAULTS = frozenset(
    {
        "42P01",  # undefined_table
        "42703",  # undefined_column
        "3F000",  # invalid_schema_name
    }
)
"""SQLSTATEs that mean the conversion did not build what the query needs.

Everything else PostgreSQL rejects at parse or plan time -- undefined operator
(``42883``), grouping error (``42803``), datatype mismatch (``42804``) -- is the
gold query asking for something the dialect does not offer, and would fail
identically against a perfect conversion.

Splitting on SQLSTATE rather than on message text because the messages are
localised and reworded between major versions, and this decides whether a
database is reported as broken.
"""


def _classify_pg_error(exc: psycopg.Error) -> Outcome:
    """Whether a PostgreSQL rejection blames the conversion or the gold query."""
    state = getattr(exc, "sqlstate", None)
    if state in _CONVERSION_FAULTS:
        return Outcome.POSTGRES_ERROR
    return Outcome.DIALECT_ERROR


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
