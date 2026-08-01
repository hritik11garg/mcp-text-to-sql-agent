"""SQLite to PostgreSQL, planned first and reported afterwards.

The conversion is split in two on purpose. :func:`plan_database` reads the
source and decides every target name and type without touching PostgreSQL;
:func:`convert_database` executes a plan. A plan is inspectable, diffable, and
testable without a server, and the decisions worth arguing about -- what type a
dynamically-typed column becomes, what happens to a name that will not fold
cleanly -- are all in the half that needs no database.

Three things this deliberately does not do.

**It does not rewrite data to make gold queries pass.** A column whose values
are a mix of numbers and text becomes ``text``, and a gold query comparing it to
a number then fails on PostgreSQL where it succeeded on SQLite. That failure is
real and is what :mod:`benchmark.verify` exists to find. Coercing the column to
numeric and dropping the rows that do not fit would make the query pass and the
answer wrong.

**It does not enforce constraints the source data violates.** Primary keys and
foreign keys are added *after* the data loads, and a constraint the data cannot
satisfy is skipped and recorded rather than failing the database. The
constraints exist so schema retrieval and join reasoning can see the
relationships; they are metadata here, not integrity enforcement, and benchmark
data is routinely inconsistent.

**It does not grant more than SELECT.** Migration 002 grants the read-only role
privileges on ``public`` only, so a converted schema is invisible to it until
granted explicitly. That grant is issued here, is USAGE plus SELECT, and is
never widened -- the containment boundary is the one thing a data-loading script
must not quietly relax to make something work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import psycopg
from psycopg import Connection, sql

from benchmark.identifiers import IdentifierMap, to_pg_identifier
from benchmark.sqlite_source import SqliteDatabase, SqliteTable
from core.exceptions import ConversionError, UnsafeIdentifierError
from core.settings import BenchmarkSettings

logger = logging.getLogger(__name__)

TEXT = "text"
BIGINT = "bigint"
DOUBLE = "double precision"
BYTEA = "bytea"

_AFFINITY_DEFAULT = {
    "INTEGER": BIGINT,
    "REAL": DOUBLE,
    "NUMERIC": DOUBLE,
    "TEXT": TEXT,
    "BLOB": BYTEA,
}


def sqlite_affinity(declared: str) -> str:
    """SQLite's own type-affinity rules, in their documented order.

    Order matters and is not alphabetical: ``VARCHAR`` contains neither ``INT``
    nor ``BLOB`` but does contain ``CHAR``, while ``POINT`` contains ``INT`` and
    is therefore INTEGER-affinity by the rules as written. Reimplementing this
    from intuition rather than from the rules is how a converter ends up
    disagreeing with the engine it is converting from.
    """
    upper = declared.upper()
    if "INT" in upper:
        return "INTEGER"
    if any(token in upper for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in upper or not upper.strip():
        return "BLOB"
    if any(token in upper for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def infer_pg_type(values: Iterable[Any], *, declared: str) -> str:
    """Decide a PostgreSQL type from the values a column actually holds.

    The declared type is a hint, not evidence. SQLite does not enforce it: a
    column declared ``INTEGER`` can hold ``'unknown'``, and benchmark databases
    do exactly that. Choosing ``bigint`` on the strength of the declaration
    would fail the load on one row in a hundred thousand, and choosing it and
    dropping the row would change the answers.

    Widening rules, from narrowest: every value ``int`` gives ``bigint``; a mix
    of ``int`` and ``float`` gives ``double precision``; anything else gives
    ``text``. ``bytes`` anywhere forces ``bytea`` unless text is also present,
    in which case there is no type that holds both and ``text`` wins.

    An all-NULL column has no evidence at all, so the declared affinity decides.
    """
    seen_int = seen_float = seen_str = seen_bytes = seen_other = False
    empty = True

    for value in values:
        if value is None:
            continue
        empty = False
        # `bool` needs no branch of its own: sqlite3 never returns one, and it
        # is an `int` subclass, so `bigint` is the correct target if it ever did.
        if isinstance(value, int):
            seen_int = True
        elif isinstance(value, float):
            seen_float = True
        elif isinstance(value, str):
            seen_str = True
        elif isinstance(value, bytes | bytearray | memoryview):
            seen_bytes = True
        else:
            seen_other = True

    if empty:
        return _AFFINITY_DEFAULT[sqlite_affinity(declared)]
    if seen_str or seen_other:
        return TEXT
    if seen_bytes:
        return TEXT if (seen_int or seen_float) else BYTEA
    if seen_float:
        return DOUBLE
    if seen_int:
        return BIGINT
    return TEXT


_CLASS_TO_TYPE = {"integer": BIGINT, "real": DOUBLE, "text": TEXT, "blob": BYTEA}


def pg_type_for_storage_classes(classes: set[str], *, declared: str) -> str:
    """Decide a PostgreSQL type from the storage classes a column actually holds.

    The same widening rules as :func:`infer_pg_type`, applied to an *exact*
    answer from SQLite rather than to a sample. ``'null'`` carries no
    information and is discarded; a column with nothing else has no evidence at
    all, so the declared affinity decides.
    """
    present = classes - {"null"}
    if not present:
        return _AFFINITY_DEFAULT[sqlite_affinity(declared)]
    if "text" in present:
        return TEXT
    if "blob" in present:
        return TEXT if present & {"integer", "real"} else BYTEA
    if "real" in present:
        return DOUBLE
    if "integer" in present:
        return BIGINT
    return TEXT


@dataclass(frozen=True, slots=True)
class ColumnPlan:
    source_name: str
    target_name: str
    pg_type: str
    declared_type: str

    @property
    def coerced(self) -> bool:
        """Whether the data forced a type the declaration did not imply.

        Reported per database. A conversion with no coercions is a clean one; a
        conversion with forty is a database whose gold queries are about to
        start comparing numbers to text, and knowing that before reading the
        verification failures saves the wrong investigation.
        """
        return self.pg_type != _AFFINITY_DEFAULT[sqlite_affinity(self.declared_type)]


@dataclass(frozen=True, slots=True)
class TablePlan:
    source_name: str
    target_name: str
    columns: tuple[ColumnPlan, ...]
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = ()
    """``(local columns, referenced table, referenced columns)``, already folded."""


@dataclass(slots=True)
class ConversionReport:
    """What the conversion did, in enough detail to explain a later surprise."""

    db_id: str
    schema: str
    tables: int = 0
    rows: int = 0
    coerced_columns: tuple[str, ...] = ()
    skipped_primary_keys: tuple[str, ...] = ()
    skipped_foreign_keys: tuple[str, ...] = ()
    text_replacements: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "db_id": self.db_id,
            "schema": self.schema,
            "tables": self.tables,
            "rows": self.rows,
            "coerced_columns": list(self.coerced_columns),
            "skipped_primary_keys": list(self.skipped_primary_keys),
            "skipped_foreign_keys": list(self.skipped_foreign_keys),
            "text_replacements": self.text_replacements,
            "notes": self.notes,
        }


def schema_name_for(db_id: str, *, prefix: str = "") -> str:
    """The PostgreSQL schema one benchmark database becomes.

    A prefix exists because ``db_id`` is unique within a benchmark and not
    across benchmarks -- Spider and BIRD both ship a ``movie`` database, and
    loading the second over the first would silently replace it. The combined
    name is validated as a whole, so a prefix that pushes a long ``db_id`` past
    the identifier limit is refused rather than truncated into a collision.
    """
    return to_pg_identifier(f"{prefix}{db_id}" if prefix else db_id, kind="schema")


def plan_database(
    database: SqliteDatabase,
    *,
    settings: BenchmarkSettings,
    prefix: str = "",
) -> tuple[str, list[TablePlan]]:
    """Read a source database and decide the whole target shape.

    Raises:
        ConversionError: The database has no convertible tables.
        UnsafeIdentifierError: A name cannot be represented unambiguously.
    """
    schema = schema_name_for(database.db_id, prefix=prefix)
    names = database.table_names()
    if not names:
        raise ConversionError(f"{database.db_id}: no ordinary tables to convert")

    table_map = IdentifierMap.build(names, kind="table")
    sources = {name: database.table(name) for name in names}
    column_maps = {
        name: IdentifierMap.build([c.name for c in source.columns], kind="column")
        for name, source in sources.items()
    }

    plans = [
        _plan_table(
            database,
            sources[name],
            target_name=table_map.safe(name),
            table_map=table_map,
            column_maps=column_maps,
            settings=settings,
        )
        for name in names
    ]
    return schema, unify_foreign_key_types(database, plans)


def unify_foreign_key_types(
    database: SqliteDatabase, plans: Sequence[TablePlan]
) -> list[TablePlan]:
    """Give both sides of a foreign key the same PostgreSQL type, where the data allows.

    **Why this is necessary, and why it is faithful rather than convenient.**
    Spider declares ``concert.Stadium_ID`` as ``TEXT`` and stores ``'1'``, while
    ``stadium.Stadium_ID`` is ``INT`` storing ``1``. SQLite joins them happily:
    comparing a TEXT-affinity column to an INTEGER-affinity one applies *numeric*
    affinity to the text operand, so ``'1' = 1`` is true there. PostgreSQL has no
    such rule and answers ``operator does not exist: text = bigint``.

    Left alone, per-column inference is locally correct and globally wrong: the
    foreign key cannot be created, retrieval loses the join edge, and every gold
    query crossing it fails. Measured on Spider: 35 of 769 foreign keys, across
    21 of 166 databases, and 19 of 45 dev questions on ``concert_singer`` alone.

    **The unification always runs toward the numeric type, never toward text.**
    That direction reproduces what SQLite actually computed. The other direction
    does not: ``'01' = 1`` is true under numeric affinity but ``'01' = '1'`` is
    false as text, so widening a numeric column to text would silently change
    which rows join.

    A column is only re-typed when *every* one of its values converts losslessly.
    Where they do not, the types stay as inferred and the constraint is dropped
    and reported -- the honest outcome, because SQLite is then doing a comparison
    this conversion cannot reproduce.
    """
    by_name = {plan.target_name: plan for plan in plans}
    retyped: dict[tuple[str, str], str] = {}

    for plan in plans:
        for local, referenced_table, remote in plan.foreign_keys:
            target = by_name.get(referenced_table)
            if target is None:
                continue
            for local_column, remote_column in zip(local, remote, strict=True):
                left = _column(plan, local_column)
                right = _column(target, remote_column)
                if left is None or right is None:
                    continue

                wanted = _unified_type(
                    retyped.get((plan.target_name, local_column), left.pg_type),
                    retyped.get((referenced_table, remote_column), right.pg_type),
                )
                if wanted is None:
                    continue

                for owner, column in ((plan, left), (target, right)):
                    current = retyped.get((owner.target_name, column.target_name), column.pg_type)
                    if current == wanted:
                        continue
                    if _values_fit(database, owner, column, wanted):
                        retyped[(owner.target_name, column.target_name)] = wanted
                    else:
                        logger.info(
                            "%s.%s cannot become %s without loss; foreign key will be dropped",
                            owner.target_name,
                            column.target_name,
                            wanted,
                        )

    if not retyped:
        return list(plans)

    return [_apply_retypes(plan, retyped) for plan in plans]


def _column(plan: TablePlan, target_name: str) -> ColumnPlan | None:
    return next((c for c in plan.columns if c.target_name == target_name), None)


_NUMERIC_RANK = {BIGINT: 1, DOUBLE: 2}


def _unified_type(left: str, right: str) -> str | None:
    """The type both sides should take, or ``None`` if they already agree or cannot."""
    if left == right:
        return None
    if BYTEA in (left, right):
        return None
    if left in _NUMERIC_RANK and right in _NUMERIC_RANK:
        return DOUBLE
    # One side numeric, one side text: adopt the numeric type. Never the reverse.
    numeric = left if left in _NUMERIC_RANK else right if right in _NUMERIC_RANK else None
    return numeric


def _values_fit(
    database: SqliteDatabase, plan: TablePlan, column: ColumnPlan, pg_type: str
) -> bool:
    """Whether every value in a column converts to ``pg_type`` without loss.

    Checked against the source rather than assumed from the inferred type,
    because the whole point is that the inferred type was too narrow a view of
    what the column means in a join.
    """
    caster: Callable[[Any], float] = int if pg_type == BIGINT else float
    for (value,) in database.rows(plan.source_name, [column.source_name]):
        if value is None:
            continue
        try:
            converted = caster(value)
        except (TypeError, ValueError):
            return False
        # `int('1.5')` raises, but `int(1.5)` silently truncates.
        if pg_type == BIGINT and isinstance(value, float) and converted != value:
            return False
    return True


def _apply_retypes(plan: TablePlan, retyped: dict[tuple[str, str], str]) -> TablePlan:
    columns = tuple(
        replace(column, pg_type=retyped[(plan.target_name, column.target_name)])
        if (plan.target_name, column.target_name) in retyped
        else column
        for column in plan.columns
    )
    return replace(plan, columns=columns)


def _plan_table(
    database: SqliteDatabase,
    source: SqliteTable,
    *,
    target_name: str,
    table_map: IdentifierMap,
    column_maps: dict[str, IdentifierMap],
    settings: BenchmarkSettings,
) -> TablePlan:
    columns = column_maps[source.name]
    names = [column.name for column in source.columns]

    # Exact, over the whole column, in one scan. See `storage_classes`.
    classes = database.storage_classes(source.name, names)

    plans = tuple(
        ColumnPlan(
            source_name=column.name,
            target_name=columns.safe(column.name),
            pg_type=pg_type_for_storage_classes(
                classes.get(column.name, set()), declared=column.declared_type
            ),
            declared_type=column.declared_type,
        )
        for column in source.columns
    )

    foreign_keys: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = []
    for key in source.foreign_keys:
        target = _resolve_fk_table(key.referenced_table, table_map)
        if target is None or key.referenced_table not in column_maps:
            logger.debug(
                "%s.%s: FK to unknown table %r skipped",
                database.db_id,
                source.name,
                key.referenced_table,
            )
            continue
        remote_columns = column_maps[key.referenced_table]
        try:
            local = tuple(columns.safe(column) for column in key.columns)
            remote = tuple(remote_columns.safe(column) for column in key.referenced_columns)
        except UnsafeIdentifierError:
            # A constraint naming a column that is not in the table it points at
            # is a defect in the source schema, not something to guess about.
            logger.debug("%s.%s: FK references an unknown column", database.db_id, source.name)
            continue
        foreign_keys.append((local, target, remote))

    return TablePlan(
        source_name=source.name,
        target_name=target_name,
        columns=plans,
        primary_key=tuple(columns.safe(name) for name in source.primary_key),
        foreign_keys=tuple(foreign_keys),
    )


def _resolve_fk_table(referenced: str, table_map: IdentifierMap) -> str | None:
    """Match a foreign key's target table name case-insensitively.

    SQLite resolves ``REFERENCES Stadium`` against a table created as
    ``stadium``, so a converter that matches the raw strings drops constraints
    that the source engine honours.
    """
    for raw, safe in table_map:
        if raw.lower() == referenced.lower():
            return safe
    return None


def convert_database(
    connection: Connection[Any],
    database: SqliteDatabase,
    *,
    schema: str,
    plans: Sequence[TablePlan],
    settings: BenchmarkSettings,
    readonly_role: str,
    replace: bool = False,
) -> ConversionReport:
    """Create the schema, load every table, then add constraints and grants.

    The whole database is one transaction. A conversion that fails halfway and
    leaves a schema holding four of nine tables is worse than one that fails
    cleanly: the next run finds a schema that exists, and every query against
    it returns plausible, wrong answers.

    Raises:
        ConversionError: The schema exists and ``replace`` was not requested,
            or the load failed. Nothing is left behind either way.
    """
    report = ConversionReport(db_id=database.db_id, schema=schema)
    schema_ident = sql.Identifier(schema)

    with connection.transaction():
        if _schema_exists(connection, schema):
            if not replace:
                raise ConversionError(
                    f"schema {schema!r} already exists. Pass --replace to drop and "
                    f"reload it; loading into it would leave a mix of two databases."
                )
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(schema_ident))

        # No REVOKE follows: PostgreSQL grants nothing to PUBLIC on a newly
        # created schema, unlike the pre-15 `public` schema. The grant below is
        # therefore the complete privilege set on this schema.
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(schema_ident))

        for plan in plans:
            _create_table(connection, schema_ident, plan)
            report.rows += _load_table(connection, database, schema_ident, plan, settings=settings)
            report.tables += 1

            report.coerced_columns = (
                *report.coerced_columns,
                *(
                    f"{plan.target_name}.{column.target_name}:{column.pg_type}"
                    for column in plan.columns
                    if column.coerced
                ),
            )

        for plan in plans:
            if not _add_primary_key(connection, schema_ident, plan):
                report.skipped_primary_keys = (*report.skipped_primary_keys, plan.target_name)

        for plan in plans:
            for index, key in enumerate(plan.foreign_keys):
                if not _add_foreign_key(connection, schema_ident, plan, key, index):
                    report.skipped_foreign_keys = (
                        *report.skipped_foreign_keys,
                        f"{plan.target_name}({','.join(key[0])})->{key[1]}",
                    )

        _grant_readonly(connection, schema_ident, readonly_role)

    report.text_replacements = database.text_replacements
    if report.text_replacements:
        report.notes.append(
            f"{report.text_replacements} value(s) contained bytes that are not valid "
            f"UTF-8 and were decoded with replacement characters"
        )
    return report


def _schema_exists(connection: Connection[Any], schema: str) -> bool:
    row = connection.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone()
    return row is not None


def _create_table(connection: Connection[Any], schema: sql.Identifier, plan: TablePlan) -> None:
    """``CREATE TABLE`` with no constraints. They are added after the data lands."""
    columns = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(column.target_name), sql.SQL(column.pg_type))
        for column in plan.columns
    )
    connection.execute(
        sql.SQL("CREATE TABLE {}.{} ({})").format(schema, sql.Identifier(plan.target_name), columns)
    )


def _load_table(
    connection: Connection[Any],
    database: SqliteDatabase,
    schema: sql.Identifier,
    plan: TablePlan,
    *,
    settings: BenchmarkSettings,
) -> int:
    """Stream one table in via ``COPY``.

    ``COPY`` rather than batched ``INSERT`` because the corpus is ~200
    databases and the difference is minutes against hours. Values still cross
    as typed parameters through the copy protocol -- nothing is formatted into
    a statement.
    """
    target = sql.SQL("{}.{} ({})").format(
        schema,
        sql.Identifier(plan.target_name),
        sql.SQL(", ").join(sql.Identifier(column.target_name) for column in plan.columns),
    )
    types = [column.pg_type for column in plan.columns]
    source_columns = [column.source_name for column in plan.columns]

    written = 0
    with (
        connection.cursor() as cursor,
        cursor.copy(sql.SQL("COPY {} FROM STDIN").format(target)) as copy,
    ):
        for row in database.rows(plan.source_name, source_columns):
            copy.write_row(
                tuple(adapt(value, pg_type) for value, pg_type in zip(row, types, strict=True))
            )
            written += 1
            if written % settings.benchmark_copy_batch_rows == 0:
                logger.debug("%s: %d rows", plan.target_name, written)
    return written


def adapt(value: Any, pg_type: str) -> Any:
    """Coerce one SQLite value to the type its target column was planned as.

    Type inference guarantees this is a no-op for almost every value; it exists
    for the cases inference resolved by widening. A blob landing in a ``text``
    column is the one that needs care -- passed through unchanged, psycopg would
    send it as a bytea hex literal and the column would hold ``\\x68690a``
    rather than the text, which no gold query would ever match.
    """
    if value is None:
        return None
    if pg_type == BYTEA:
        return value if isinstance(value, bytes) else str(value).encode("utf-8")
    if pg_type == TEXT:
        if isinstance(value, str):
            return value
        if isinstance(value, bytes | bytearray | memoryview):
            return bytes(value).decode("utf-8", "replace")
        return str(value)
    if pg_type == BIGINT:
        return int(value)
    if pg_type == DOUBLE:
        return float(value)
    return value


def _add_primary_key(connection: Connection[Any], schema: sql.Identifier, plan: TablePlan) -> bool:
    """Add the primary key, or report that the data does not support one."""
    if not plan.primary_key:
        return True
    statement = sql.SQL("ALTER TABLE {}.{} ADD PRIMARY KEY ({})").format(
        schema,
        sql.Identifier(plan.target_name),
        sql.SQL(", ").join(sql.Identifier(column) for column in plan.primary_key),
    )
    return _try_constraint(connection, statement, what=f"primary key on {plan.target_name}")


def _add_foreign_key(
    connection: Connection[Any],
    schema: sql.Identifier,
    plan: TablePlan,
    key: tuple[tuple[str, ...], str, tuple[str, ...]],
    index: int,
) -> bool:
    """Add one foreign key as ``NOT VALID``.

    ``NOT VALID`` skips the check against existing rows, which benchmark data
    frequently fails, while still recording the relationship in
    ``pg_constraint`` -- which is what schema introspection reads and what the
    generator needs in order to join two tables correctly. The alternative is a
    schema with no visible relationships at all, which degrades retrieval on
    exactly the databases that need it most.
    """
    local, referenced_table, remote = key
    name = f"{plan.target_name}_fk_{index}"
    statement = sql.SQL(
        "ALTER TABLE {}.{} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {}.{} ({}) NOT VALID"
    ).format(
        schema,
        sql.Identifier(plan.target_name),
        sql.Identifier(name[:63]),
        sql.SQL(", ").join(sql.Identifier(column) for column in local),
        schema,
        sql.Identifier(referenced_table),
        sql.SQL(", ").join(sql.Identifier(column) for column in remote),
    )
    return _try_constraint(connection, statement, what=f"foreign key {name}")


def _try_constraint(connection: Connection[Any], statement: sql.Composed, *, what: str) -> bool:
    """Run a constraint DDL in a savepoint so a failure does not kill the load.

    Without the nested transaction, one rejected constraint aborts the
    surrounding transaction and takes the whole database with it. Constraints
    are metadata here; the rows are the deliverable.
    """
    try:
        with connection.transaction():
            connection.execute(statement)
    except psycopg.Error as exc:
        logger.info("skipped %s: %s", what, str(exc).splitlines()[0])
        return False
    return True


def _grant_readonly(connection: Connection[Any], schema: sql.Identifier, role: str) -> None:
    """USAGE on the schema and SELECT on its tables. Nothing else, ever.

    The role name is validated as an identifier before composition even though
    ``sql.Identifier`` would quote it safely, because quoting answers "how is
    this written" and not "may this be named" -- and the thing being named here
    is the grantee of a privilege. See ADR-017.
    """
    grantee = sql.Identifier(to_pg_identifier(role, kind="role"))
    connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, grantee))
    connection.execute(
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(schema, grantee)
    )


__all__ = [
    "BIGINT",
    "BYTEA",
    "DOUBLE",
    "TEXT",
    "ColumnPlan",
    "ConversionReport",
    "TablePlan",
    "adapt",
    "convert_database",
    "infer_pg_type",
    "pg_type_for_storage_classes",
    "plan_database",
    "schema_name_for",
    "sqlite_affinity",
    "unify_foreign_key_types",
]
