"""Column statistics for disambiguation, under a disclosure budget.

Retrieval answers *which columns might be relevant*. It cannot answer the
question that actually blocks a correct query: given two plausible columns, or
a column whose values are stored as ``'FI'`` rather than ``'Finland'``, what is
really in there? Profiling answers that.

**This is the one component whose output is row data by design.** Everywhere
else in the system, real values either stay in the database (execution results,
the audit log) or stay in a store the operator controls (the catalog's sampled
values, which are persisted but never rendered into a prompt -- see
docs/operations/SECURITY.md section 14.2.5). A profile exists in order to be
shown to a language model. So the design question is not "can we avoid sending
values?" -- sometimes we cannot and still be useful -- but **which values carry
the disambiguation signal without carrying a record with them.**

The answer used here has three parts:

*Derived statistics are free.* Null fraction, distinct count and row estimate
describe the column without quoting it. They are always returned.

*A value is reportable when it is a category, not a record.* A value occurring
once identifies whoever it belongs to. A value occurring five hundred times is
a label. That threshold -- ``profile_min_value_frequency`` -- is the small-cell
rule from statistical disclosure control, and it is what makes "top-k frequent
values" safe enough to be on by default while raw sampling is not.

*Extremes are only returned for types where they are bounds.* ``max(salary)``
and ``max(order_date)`` are both single values, but the first is a person's
pay and the second is a fact about the table. The line drawn here is
type-based and blunt: numeric and temporal columns get min/max, text columns
never do, because the lexicographic extreme of a text column is a verbatim
cell and calling that a "statistic" would be a category error.

Anything withheld is *reported as withheld*, with the reason. An agent told
only that a field is missing will ask again; an agent told "every value occurs
fewer than 5 times" can pick a different strategy.

See docs/architecture/MCP.md section 3.4 and docs/operations/SECURITY.md
section 14.2.6.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import Connection, sql

from core.exceptions import ProfilingError, UnknownTableError
from core.settings import ProfilingSettings
from schema.catalog import SchemaCatalog
from schema.sensitivity import DEFAULT_SENSITIVE_PATTERNS, is_sensitive

logger = logging.getLogger(__name__)

_ROW_ESTIMATE_SQL = """
    SELECT c.reltuples::bigint
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s AND c.relname = %s
"""
"""The planner's own estimate. Free -- it reads a catalog row rather than the
table -- and clearly labelled as an estimate, which is the honest thing to
show for a number nobody should filter on."""

_TIMEOUT_SQL = "SELECT set_config('statement_timeout', %s, true)"

_ORDERED_TYPE_PREFIXES = (
    "smallint",
    "integer",
    "bigint",
    "decimal",
    "numeric",
    "real",
    "double precision",
    "money",
    "date",
    "timestamp",
    "time",
    "interval",
)
"""Types whose extremes are bounds rather than cell contents.

Deliberately excludes every string type, ``uuid``, ``bytea``, ``json`` and the
enum/array/composite cases. ``min(name)`` is somebody's name; ``min(price)`` is
a fact about prices. Being wrong in the permissive direction here would leak a
verbatim value under the label "statistic", so the list is an allowlist and
anything unrecognised gets no extremes.
"""

WITHHELD_SENSITIVE_NAME = "column name matches the sensitive-column denylist"
WITHHELD_RARE = "every value occurs fewer than {threshold} times in the scanned rows"
WITHHELD_UNORDERED_TYPE = "extremes are not reported for type {data_type}"
WITHHELD_SAMPLING_DISABLED = "raw values require PROFILE_ALLOW_VALUE_SAMPLING"
WITHHELD_TOP_K_DISABLED = "frequent values are disabled (PROFILE_TOP_K=0)"


@dataclass(frozen=True, slots=True)
class FrequentValue:
    """One value and how often it was seen in the scanned rows.

    The count travels with the value on purpose. It is what lets a reader check
    the small-cell rule for themselves rather than trust that it was applied,
    and it is genuinely useful to the agent: a value covering 90% of a column
    is a different signal from one covering 3%.
    """

    value: str
    count: int


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """What one column looks like, and what was deliberately not said about it."""

    column: str
    data_type: str
    null_fraction: float | None = None
    distinct_count: int | None = None
    minimum: str | None = None
    maximum: str | None = None
    frequent_values: tuple[FrequentValue, ...] = ()
    sample_values: tuple[str, ...] = ()
    withheld: tuple[str, ...] = ()
    """Why each absent field is absent.

    Silence and suppression look identical to a caller, and they call for
    different responses -- the same reason validation returns an ``error_type``
    rather than just ``valid: false``.
    """

    @property
    def is_fully_suppressed(self) -> bool:
        return WITHHELD_SENSITIVE_NAME in self.withheld


@dataclass(frozen=True, slots=True)
class TableProfile:
    """A bounded, explicitly approximate description of one table.

    **Every number here is computed over at most ``scanned_rows`` rows**, taken
    in physical order without an ``ORDER BY``. That is not a random sample, and
    a null fraction from it is not the table's null fraction. The alternative,
    ``ORDER BY random()``, reads the whole table -- the exact cost profiling
    exists to avoid -- and ``TABLESAMPLE`` does not work on views, which are
    catalogued and profiled like any other relation.

    The field is named ``scanned_rows`` rather than ``sample_size`` so that the
    approximation is visible at the point of use, and it is stated in the tool
    description the model reads.
    """

    table: str
    columns: tuple[ColumnProfile, ...] = field(default_factory=tuple)
    row_estimate: int | None = None
    scanned_rows: int = 0
    columns_omitted: int = 0
    """Columns dropped by the width cap. Non-zero means this profile is partial
    and a follow-up call with an explicit ``columns`` list will see the rest."""


class TableProfiler:
    """Profiles a table over the read-only connection, within fixed bounds.

    Args:
        connection: A **read-only** connection. Profiling reads real rows from
            target tables, which is precisely the privilege the read-only role
            is scoped to; running it as the owner would put a component that
            composes identifiers into SQL on a connection that can write.
        catalog: The identifier allowlist. Names are resolved against it
            *before* any statement is built -- see :meth:`profile`.
        settings: Every bound. A caller may ask for less, never for more.
        schema: The target schema, from ``DB_TARGET_SCHEMA``.
        sensitive_patterns: Column names that are never read.
    """

    def __init__(
        self,
        connection: Connection[Any],
        catalog: SchemaCatalog,
        settings: ProfilingSettings,
        *,
        schema: str = "public",
        sensitive_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS,
    ) -> None:
        self._conn = connection
        self._catalog = catalog
        self._settings = settings
        self._schema = schema
        self._sensitive_patterns = sensitive_patterns

    def profile(
        self,
        table: str,
        *,
        columns: list[str] | tuple[str, ...] | None = None,
        sample_rows: int | None = None,
    ) -> TableProfile:
        """Describe a table, or a named subset of its columns.

        Raises:
            UnknownTableError: the table is not in the catalog.
            ProfilingError: a named column is not in the catalog, or the
                database refused the profile outright.

        Resolution against the catalog happens first and is not only a
        usability check. ``table`` and ``columns`` originate as free text chosen
        by a language model reading a user's question, and they end up inside a
        composed statement. ``sql.Identifier`` quotes them correctly, but
        "correctly quoted" and "allowed to be named at all" are different
        properties, and only the second one bounds which relations a caller can
        reach. Rejecting an unknown name here means the composition layer only
        ever sees identifiers that were already in the catalog.
        """
        resolved_table = self._resolve_table(table)
        selected, omitted = self._select_columns(resolved_table, columns)
        wanted_samples = self._settings.clamp_sample_rows(sample_rows)

        profiles: list[ColumnProfile] = []
        for name, data_type in selected:
            profiles.append(self._profile_column(resolved_table, name, data_type, wanted_samples))

        return TableProfile(
            table=resolved_table,
            columns=tuple(profiles),
            row_estimate=self._row_estimate(resolved_table),
            scanned_rows=self._settings.profile_scan_limit,
            columns_omitted=omitted,
        )

    # --- resolution --------------------------------------------------------

    def _resolve_table(self, table: str) -> str:
        if not self._catalog.has_table(table):
            raise UnknownTableError(table, self._catalog.suggest_table(table))
        return table.casefold()

    def _select_columns(
        self, table: str, requested: list[str] | tuple[str, ...] | None
    ) -> tuple[tuple[tuple[str, str], ...], int]:
        """Resolve the column list and apply the width cap.

        Returns the selected ``(name, data_type)`` pairs and how many were
        dropped. An explicit request is honoured in full up to the cap, because
        a caller naming three columns of a 300-column table is doing exactly
        what the cap exists to encourage.
        """
        known = self._catalog.columns_by_table[table]

        if requested is not None:
            unknown = [c for c in requested if c.casefold() not in known]
            if unknown:
                suggestion = self._catalog.suggest_column(unknown[0], [table])
                hint = f"; did you mean {suggestion!r}?" if suggestion else ""
                raise ProfilingError(f"column {unknown[0]!r} is not in {table!r}{hint}")
            names = [c.casefold() for c in requested]
        else:
            names = sorted(known)

        cap = self._settings.profile_max_columns
        omitted = max(0, len(names) - cap)
        types = self._column_types(table)
        return tuple((name, types.get(name, "unknown")) for name in names[:cap]), omitted

    def _column_types(self, table: str) -> dict[str, str]:
        """Types straight from ``pg_catalog``.

        Not from the schema catalog: ``SchemaCatalog`` is deliberately a
        name-only index, and the type decides whether extremes may be reported,
        which is a security-relevant branch. Reading it from the live database
        means a re-typed column cannot leave a stale allowlist entry behind.
        """
        query = """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND a.attnum > 0 AND NOT a.attisdropped
              AND has_column_privilege(c.oid, a.attnum, 'SELECT')
        """
        with self._conn.cursor() as cur:
            cur.execute(query, (self._schema, table))
            return {str(name).casefold(): str(data_type) for name, data_type in cur.fetchall()}

    def _row_estimate(self, table: str) -> int | None:
        with self._conn.cursor() as cur:
            cur.execute(_ROW_ESTIMATE_SQL, (self._schema, table))
            row = cur.fetchone()
        if row is None or row[0] is None or row[0] < 0:
            # reltuples is -1 on a table that has never been analysed.
            return None
        return int(row[0])

    # --- per-column --------------------------------------------------------

    def _profile_column(
        self, table: str, column: str, data_type: str, sample_rows: int
    ) -> ColumnProfile:
        """One column, with every gate applied before any value is read.

        The sensitivity check comes first and returns immediately. Reading the
        values and filtering them afterwards would put them in this process's
        memory, in the driver's buffers, and in any exception that quoted the
        query -- refusing up front means they are never read at all, which is
        the same ordering :mod:`schema.introspection` uses.
        """
        if is_sensitive(column, self._sensitive_patterns):
            logger.debug("withholding profile of sensitive column %s.%s", table, column)
            return ColumnProfile(
                column=column, data_type=data_type, withheld=(WITHHELD_SENSITIVE_NAME,)
            )

        withheld: list[str] = []
        supports_extremes = _supports_extremes(data_type)
        if not supports_extremes:
            withheld.append(WITHHELD_UNORDERED_TYPE.format(data_type=data_type))

        top_k = self._settings.profile_top_k
        if top_k == 0:
            withheld.append(WITHHELD_TOP_K_DISABLED)

        try:
            stats, frequent = self._run_stats(table, column, supports_extremes, top_k)
        except psycopg.Error as exc:
            # One unprofileable column -- an exotic type with no text cast, a
            # timeout on a huge column -- must not fail the whole profile. The
            # agent gets a partial answer with the reason attached, which is
            # more useful than an error for a question it was only asking to
            # disambiguate. Driver text is logged, never returned: MCP.md
            # section 6 forbids raw driver output crossing a tool boundary.
            logger.warning("could not profile %s.%s: %s", table, column, exc)
            return ColumnProfile(
                column=column,
                data_type=data_type,
                withheld=(*withheld, "the database refused to compute statistics for this column"),
            )

        if top_k > 0 and not frequent:
            withheld.append(
                WITHHELD_RARE.format(threshold=self._settings.profile_min_value_frequency)
            )

        samples: tuple[str, ...] = ()
        if sample_rows > 0:
            samples = self._sample(table, column, sample_rows)
        else:
            withheld.append(WITHHELD_SAMPLING_DISABLED)

        return ColumnProfile(
            column=column,
            data_type=data_type,
            null_fraction=stats.null_fraction,
            distinct_count=stats.distinct_count,
            minimum=stats.minimum,
            maximum=stats.maximum,
            frequent_values=frequent,
            sample_values=samples,
            withheld=tuple(withheld),
        )

    def _run_stats(
        self, table: str, column: str, supports_extremes: bool, top_k: int
    ) -> tuple[_Stats, tuple[FrequentValue, ...]]:
        """Statistics and frequent values in one round trip.

        The ``LEFT JOIN ... ON true`` is load-bearing: ``stats`` is always one
        row and ``freq`` is often zero, and an inner join would discard the
        statistics of exactly the columns whose values were all too rare to
        report -- the case where the statistics are the only thing left.

        Every caller-influenced value is a bound parameter. The two identifiers
        are composed with :class:`psycopg.sql.Identifier`, and both were
        resolved against the catalog before reaching this method.
        """
        extremes = (
            sql.SQL(", left(min(v)::text, {n}) AS lo, left(max(v)::text, {n}) AS hi").format(
                n=sql.Literal(self._settings.profile_max_value_chars)
            )
            if supports_extremes
            else sql.SQL(", NULL::text AS lo, NULL::text AS hi")
        )

        query = sql.SQL(
            "WITH scan AS (SELECT {col} AS v FROM {rel} LIMIT %s), "
            "stats AS (SELECT count(*) AS n, count(v) AS non_null, "
            "                 count(DISTINCT v) AS distinct_count{extremes} FROM scan), "
            "freq AS (SELECT left(v::text, %s) AS value, count(*) AS freq FROM scan "
            "         WHERE v IS NOT NULL GROUP BY v HAVING count(*) >= %s "
            "         ORDER BY count(*) DESC, 1 LIMIT %s) "
            "SELECT s.n, s.non_null, s.distinct_count, s.lo, s.hi, f.value, f.freq "
            "FROM stats s LEFT JOIN freq f ON true"
        ).format(
            col=sql.Identifier(column),
            rel=sql.Identifier(self._schema, table),
            extremes=extremes,
        )

        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_TIMEOUT_SQL, (str(self._settings.profile_timeout_ms),))
            cur.execute(
                query,
                (
                    self._settings.profile_scan_limit,
                    self._settings.profile_max_value_chars,
                    self._settings.profile_min_value_frequency,
                    max(top_k, 1),  # LIMIT 0 would drop the stats row with it.
                ),
            )
            rows = cur.fetchall()

        if not rows:  # pragma: no cover - the stats CTE always yields one row
            raise ProfilingError(f"no statistics returned for {table}.{column}")

        n, non_null, distinct_count, lo, hi, _, _ = rows[0]
        stats = _Stats(
            null_fraction=round((n - non_null) / n, 4) if n else None,
            distinct_count=int(distinct_count),
            minimum=None if lo is None else str(lo),
            maximum=None if hi is None else str(hi),
        )

        frequent = (
            ()
            if top_k == 0
            else tuple(
                FrequentValue(value=str(value), count=int(freq))
                for *_, value, freq in rows
                if value is not None
            )
        )
        return stats, frequent

    def _sample(self, table: str, column: str, limit: int) -> tuple[str, ...]:
        """Raw values, only ever reached when sampling is explicitly allowed.

        ``clamp_sample_rows`` returns 0 unless ``PROFILE_ALLOW_VALUE_SAMPLING``
        is set, so this method is unreachable by default. It is written as a
        separate query rather than folded into the statistics so that the
        disclosing statement is one distinct, greppable thing.
        """
        query = sql.SQL(
            "SELECT left({col}::text, %s) FROM {rel} WHERE {col} IS NOT NULL LIMIT %s"
        ).format(col=sql.Identifier(column), rel=sql.Identifier(self._schema, table))

        try:
            with self._conn.transaction(), self._conn.cursor() as cur:
                cur.execute(_TIMEOUT_SQL, (str(self._settings.profile_timeout_ms),))
                cur.execute(query, (self._settings.profile_max_value_chars, limit))
                return tuple(str(row[0]) for row in cur.fetchall())
        except psycopg.Error as exc:
            logger.warning("could not sample %s.%s: %s", table, column, exc)
            return ()


@dataclass(frozen=True, slots=True)
class _Stats:
    null_fraction: float | None
    distinct_count: int | None
    minimum: str | None
    maximum: str | None


def _supports_extremes(data_type: str) -> bool:
    """Whether ``min``/``max`` on this type is a bound rather than a cell.

    Matched on a prefix because ``format_type`` returns parameterised names
    (``numeric(10,2)``, ``timestamp without time zone``). Unrecognised types
    get no extremes, so a type this list has never heard of fails closed.
    """
    lowered = data_type.strip().casefold()
    if lowered.endswith("[]"):
        # An array's extreme is an entire array of values, not a bound.
        return False
    return lowered.startswith(_ORDERED_TYPE_PREFIXES)


__all__ = [
    "ColumnProfile",
    "FrequentValue",
    "ProfilingError",
    "TableProfile",
    "TableProfiler",
    "UnknownTableError",
]
