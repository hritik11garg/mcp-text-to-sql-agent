"""Reading a benchmark's SQLite database, treated as untrusted input.

A ``.sqlite`` file that arrived over the internet is not a data structure, it is
a program's input format parsed by a C library, and its schema can contain
expressions. Four things follow, and each is a line of code below rather than a
note in a document.

**Opened read-only.** ``mode=ro`` via a URI, so nothing here can write to,
migrate, or journal the source. Conversion reads; anything that wants to change
the file is a bug that should fail rather than succeed quietly.

**``trusted_schema`` off.** SQLite will evaluate expressions found in a
database's own schema -- in views, triggers, generated columns and indexes on
expressions -- when it opens or queries it. That is an arbitrary-expression sink
whose input is the file. Turning it off is one pragma and it costs nothing here,
because benchmark tables have none of those.

**Identifiers reach queries through parameters where SQLite allows it.**
``PRAGMA table_info(x)`` cannot take a bind parameter, so the obvious
implementation formats a table name into a statement. The table-valued form,
``SELECT * FROM pragma_table_info(?)``, can, and is what this module uses. The
only place a name is still interpolated is ``SELECT * FROM <table>``, where no
driver binds identifiers -- and that name has already been through
:func:`benchmark.identifiers.to_pg_identifier`, which excludes quotes entirely.

**Only ordinary tables.** Views and virtual tables are skipped. A view is a
stored query, and a virtual table can be backed by a module that reads the
filesystem (``fts``, ``csv``, ``zipfile``); neither is something a benchmark
question needs and both are more than a converter should evaluate.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from benchmark.identifiers import quote_sqlite_identifier
from core.exceptions import ConversionError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SqliteColumn:
    """One source column, as SQLite declares it."""

    name: str
    declared_type: str
    not_null: bool
    primary_key_position: int
    """1-based position in the primary key, or 0 if the column is not part of one."""


@dataclass(frozen=True, slots=True)
class SqliteForeignKey:
    """One foreign key, flattened to parallel column lists in declaration order."""

    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SqliteTable:
    name: str
    columns: tuple[SqliteColumn, ...]
    foreign_keys: tuple[SqliteForeignKey, ...] = ()

    @property
    def primary_key(self) -> tuple[str, ...]:
        keyed = [c for c in self.columns if c.primary_key_position > 0]
        keyed.sort(key=lambda c: c.primary_key_position)
        return tuple(c.name for c in keyed)


@dataclass(slots=True)
class SqliteDatabase:
    """An open, read-only handle on one benchmark database.

    Mutable in one respect only: :attr:`text_replacements` counts values whose
    bytes were not valid UTF-8. That count is reported rather than logged and
    forgotten -- a conversion that silently substituted replacement characters
    into text a gold query filters on would produce a wrong answer that looks
    like a model failure.
    """

    db_id: str
    path: Path
    connection: sqlite3.Connection
    text_replacements: int = field(default=0)

    def __enter__(self) -> SqliteDatabase:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    # --- schema ------------------------------------------------------------

    def table_names(self) -> list[str]:
        """Ordinary tables only, in a stable order.

        ``type = 'table'`` excludes views. ``sql IS NOT NULL`` excludes the
        internal tables SQLite creates for ``AUTOINCREMENT`` and full-text
        indexes, and ``sql NOT LIKE 'CREATE VIRTUAL%'`` excludes virtual tables,
        whose backing module is arbitrary.
        """
        rows = self.connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
              AND sql IS NOT NULL
              AND upper(sql) NOT LIKE 'CREATE VIRTUAL%'
            ORDER BY name
            """
        ).fetchall()
        return [str(row[0]) for row in rows]

    def table(self, name: str) -> SqliteTable:
        """Columns and foreign keys for one table."""
        info = self.connection.execute(
            'SELECT name, type, "notnull", pk FROM pragma_table_info(?)', (name,)
        ).fetchall()
        if not info:
            raise ConversionError(f"{self.db_id}: table {name!r} has no columns, or does not exist")

        columns = tuple(
            SqliteColumn(
                name=str(row[0]),
                declared_type=str(row[1] or ""),
                not_null=bool(row[2]),
                primary_key_position=int(row[3]),
            )
            for row in info
        )
        return SqliteTable(name=name, columns=columns, foreign_keys=self._foreign_keys(name))

    def _foreign_keys(self, name: str) -> tuple[SqliteForeignKey, ...]:
        """Group ``pragma_foreign_key_list`` rows back into whole constraints.

        The pragma returns one row per column pair with an ``id`` shared across
        a composite key and a ``seq`` giving the position. Ignoring either would
        pair the wrong columns together on a composite key -- the same trap the
        PostgreSQL introspection avoids with ``WITH ORDINALITY``.
        """
        rows = self.connection.execute(
            """
            SELECT id, seq, "table", "from", "to"
            FROM pragma_foreign_key_list(?)
            ORDER BY id, seq
            """,
            (name,),
        ).fetchall()

        grouped: dict[int, list[tuple[int, str, str, str | None]]] = {}
        for row in rows:
            grouped.setdefault(int(row[0]), []).append(
                (int(row[1]), str(row[2]), str(row[3]), None if row[4] is None else str(row[4]))
            )

        keys: list[SqliteForeignKey] = []
        for pairs in grouped.values():
            referenced_table = pairs[0][1]
            local = tuple(pair[2] for pair in pairs)
            # A NULL "to" means the constraint targets the referenced table's
            # primary key implicitly. Resolving it needs that table's own
            # pragma, and if it is missing the constraint is unusable.
            if any(pair[3] is None for pair in pairs):
                target = self._primary_key_of(referenced_table)
                if len(target) != len(local):
                    logger.debug(
                        "%s.%s: implicit FK target could not be resolved", self.db_id, name
                    )
                    continue
                remote = target
            else:
                remote = tuple(str(pair[3]) for pair in pairs)
            keys.append(
                SqliteForeignKey(
                    columns=local,
                    referenced_table=referenced_table,
                    referenced_columns=remote,
                )
            )
        return tuple(keys)

    def _primary_key_of(self, table: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT name, pk FROM pragma_table_info(?) WHERE pk > 0 ORDER BY pk", (table,)
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    # --- data --------------------------------------------------------------

    def rows(
        self, table: str, columns: Sequence[str], *, limit: int | None = None
    ) -> Iterator[tuple[Any, ...]]:
        """Stream a table's rows, columns named explicitly.

        Naming the columns rather than using ``SELECT *`` fixes the order to the
        one the conversion planned against. ``SELECT *`` would depend on the
        source's column order matching what the plan recorded, which is true
        until it is not.
        """
        projection = ", ".join(quote_sqlite_identifier(column) for column in columns)
        statement = f"SELECT {projection} FROM {quote_sqlite_identifier(table)}"  # noqa: S608
        if limit is not None:
            statement += f" LIMIT {int(limit)}"

        cursor = self.connection.execute(statement)
        try:
            while batch := cursor.fetchmany(1000):
                yield from (tuple(row) for row in batch)
        finally:
            cursor.close()

    def storage_classes(self, table: str, columns: Sequence[str]) -> dict[str, set[str]]:
        """The exact set of SQLite storage classes present in each column.

        ``typeof()`` reports what a value *is* -- ``'integer'``, ``'real'``,
        ``'text'``, ``'blob'``, ``'null'`` -- which is precisely the question
        type inference needs to answer, and SQLite can answer it over the whole
        column without materialising a single row in Python.

        **This replaced sampling rows into Python, and the reason is worth
        keeping.** Sampling was capped at ``BENCHMARK_TYPE_SCAN_ROWS``, and
        Spider's ``wta_1.rankings`` has 510,437 rows with exactly one
        empty-string ``player_id`` sitting at rowid 1,593,272 -- far past a
        200,000-row cap. The column was inferred ``bigint``, the load ran, and
        it died on that one value with ``invalid literal for int()``. A partial
        scan can only ever be wrong in one direction, and being wrong in that
        direction costs a whole database.

        One query per table rather than one per column: ``group_concat`` with
        ``DISTINCT`` aggregates each column's classes in a single pass, so this
        is one full scan regardless of width.
        """
        projection = ", ".join(
            f"group_concat(DISTINCT typeof({quote_sqlite_identifier(column)}))"
            for column in columns
        )
        statement = f"SELECT {projection} FROM {quote_sqlite_identifier(table)}"  # noqa: S608
        row = self.connection.execute(statement).fetchone()

        found: dict[str, set[str]] = {}
        for index, column in enumerate(columns):
            raw = row[index] if row else None
            found[column] = set(str(raw).split(",")) if raw else set()
        return found

    def execute(self, statement: str) -> list[tuple[Any, ...]]:
        """Run one gold query against the source, for conversion verification.

        Raises:
            sqlite3.Error: Left to the caller. A gold query that does not run
                on its *own* database is a benchmark defect and gets counted as
                one; swallowing it here would turn it into a conversion defect.
        """
        cursor = self.connection.execute(statement)
        try:
            return [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()


def open_database(path: Path, *, db_id: str = "") -> SqliteDatabase:
    """Open a benchmark database read-only, with schema evaluation disabled.

    Raises:
        ConversionError: The file is missing or is not a SQLite database. The
            check is an actual query rather than a magic-byte peek, because a
            file that opens and then fails on first use produces the error
            somewhere far from the cause.
    """
    if not path.is_file():
        raise ConversionError(f"no SQLite database at {path}")

    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise ConversionError(f"cannot open {path}: {exc}") from exc

    database = SqliteDatabase(db_id=db_id or path.stem, path=path, connection=connection)

    def decode(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            database.text_replacements += 1
            return raw.decode("utf-8", "replace")

    # Counted, not silent. The default factory raises on invalid UTF-8, which
    # would refuse a whole database for one bad byte; decoding with replacement
    # and reporting the count keeps the database usable and keeps the fact that
    # its text was altered attached to the conversion report.
    connection.text_factory = decode

    try:
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise ConversionError(f"{path} is not a readable SQLite database: {exc}") from exc

    return database


__all__ = [
    "SqliteColumn",
    "SqliteDatabase",
    "SqliteForeignKey",
    "SqliteTable",
    "open_database",
]
