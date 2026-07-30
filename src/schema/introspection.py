"""Reading the target schema out of PostgreSQL's catalog.

Two decisions shape this module.

**It is meant to run as the read-only role.** Introspection reads
``pg_catalog`` (world-readable) and, when sampling is enabled, real rows. Doing
that as the owner would mean a bug here could write; doing it as the read-only
role means the worst case is a failed SELECT. The sampling queries also inherit
that role's ``statement_timeout``, so a pathological table cannot hang indexing.

**It only reports what that role can actually read.** Every query filters on
``has_table_privilege`` / ``has_column_privilege``. Indexing a table the agent
cannot SELECT would produce retrieval hits that always generate SQL the
database then refuses -- a confident answer followed by a permission error,
which is the worst of both.

See docs/architecture/DATABASE.md section 3.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import Connection, sql

from schema.models import Column, ForeignKey, SchemaSnapshot, Table
from schema.sensitivity import DEFAULT_SENSITIVE_PATTERNS, is_sensitive

logger = logging.getLogger(__name__)

_RELKINDS = ("r", "p", "v", "m", "f")
"""Ordinary and partitioned tables, views, materialised views, foreign tables.
Indexes, sequences and TOAST tables are not things a question can be about."""

_TABLES_SQL = """
    SELECT c.relname, obj_description(c.oid, 'pg_class')
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s
      AND c.relkind = ANY(%s)
      AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY c.relname
"""

_COLUMNS_SQL = """
    SELECT c.relname,
           a.attname,
           format_type(a.atttypid, a.atttypmod),
           NOT a.attnotnull,
           col_description(c.oid, a.attnum)
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s
      AND c.relkind = ANY(%s)
      AND a.attnum > 0
      AND NOT a.attisdropped
      AND has_table_privilege(c.oid, 'SELECT')
      AND has_column_privilege(c.oid, a.attnum, 'SELECT')
    ORDER BY c.relname, a.attnum
"""

# unnest(conkey, confkey) WITH ORDINALITY expands composite constraints one
# column pair at a time, in declaration order. A composite key flattened
# without the ordinality would pair the wrong columns together.
_FOREIGN_KEYS_SQL = """
    SELECT con.conname,
           src.relname, src_att.attname,
           tgt.relname, tgt_att.attname
    FROM pg_constraint con
    JOIN pg_class src ON src.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = src.relnamespace
    JOIN pg_class tgt ON tgt.oid = con.confrelid
    JOIN LATERAL unnest(con.conkey, con.confkey)
         WITH ORDINALITY AS k(src_attnum, tgt_attnum, ord) ON TRUE
    JOIN pg_attribute src_att
      ON src_att.attrelid = con.conrelid AND src_att.attnum = k.src_attnum
    JOIN pg_attribute tgt_att
      ON tgt_att.attrelid = con.confrelid AND tgt_att.attnum = k.tgt_attnum
    WHERE con.contype = 'f'
      AND n.nspname = %s
      AND has_table_privilege(src.oid, 'SELECT')
      AND has_table_privilege(tgt.oid, 'SELECT')
    ORDER BY con.conname, k.ord
"""


class PostgresIntrospector:
    """Builds a :class:`~schema.models.SchemaSnapshot` from a live connection.

    Args:
        connection: Should be the read-only role. See the module docstring.
        schema: Target schema name, from ``DB_TARGET_SCHEMA``.
        sample_values: Whether to read representative values. Off by default
            everywhere; see :class:`~core.settings.RetrievalSettings`.
        sample_count: Distinct values kept per column.
        sample_max_chars: Each value is truncated to this many characters,
            in SQL, so a megabyte-long text field never crosses the wire.
        sample_scan_limit: Rows examined per column before de-duplicating.
        sensitive_patterns: Column-name substrings that are never sampled.
    """

    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str = "public",
        sample_values: bool = False,
        sample_count: int = 3,
        sample_max_chars: int = 40,
        sample_scan_limit: int = 1_000,
        sensitive_patterns: Sequence[str] = DEFAULT_SENSITIVE_PATTERNS,
    ) -> None:
        if sample_values and not connection.autocommit:
            # A sampling query that errors aborts the surrounding transaction,
            # so every later statement would fail with "current transaction is
            # aborted" and the real cause would be buried. Autocommit keeps a
            # single failed sample local to that column.
            raise ValueError("sampling requires an autocommit connection")

        self._conn = connection
        self._schema = schema
        self._sample_values = sample_values and sample_count > 0
        self._sample_count = sample_count
        self._sample_max_chars = sample_max_chars
        self._sample_scan_limit = sample_scan_limit
        self._sensitive_patterns = tuple(sensitive_patterns)

    def snapshot(self) -> SchemaSnapshot:
        """Read the whole schema. The unit the indexer writes."""
        columns_by_table = self._columns()
        tables = tuple(
            Table(
                name=name,
                comment=comment,
                columns=tuple(columns_by_table.get(name, ())),
            )
            for name, comment in self._tables()
        )
        snapshot = SchemaSnapshot(tables=tables, foreign_keys=self._foreign_keys())
        logger.info(
            "schema introspected",
            extra={
                "schema": self._schema,
                "tables": len(snapshot.tables),
                "columns": snapshot.column_count,
                "foreign_keys": len(snapshot.foreign_keys),
                "sampled": self._sample_values,
            },
        )
        return snapshot

    def _tables(self) -> list[tuple[str, str | None]]:
        with self._conn.cursor() as cur:
            cur.execute(_TABLES_SQL, (self._schema, list(_RELKINDS)))
            return [(row[0], row[1]) for row in cur.fetchall()]

    def _columns(self) -> dict[str, list[Column]]:
        with self._conn.cursor() as cur:
            cur.execute(_COLUMNS_SQL, (self._schema, list(_RELKINDS)))
            rows = cur.fetchall()

        by_table: dict[str, list[Column]] = {}
        for table, name, data_type, is_nullable, comment in rows:
            by_table.setdefault(table, []).append(
                Column(
                    table=table,
                    name=name,
                    data_type=data_type,
                    is_nullable=is_nullable,
                    comment=comment,
                    sample_values=self._samples(table, name),
                )
            )
        return by_table

    def _foreign_keys(self) -> tuple[ForeignKey, ...]:
        with self._conn.cursor() as cur:
            cur.execute(_FOREIGN_KEYS_SQL, (self._schema,))
            return tuple(
                ForeignKey(
                    constraint_name=name,
                    from_table=from_table,
                    from_column=from_column,
                    to_table=to_table,
                    to_column=to_column,
                )
                for name, from_table, from_column, to_table, to_column in cur.fetchall()
            )

    def _samples(self, table: str, column: str) -> tuple[str, ...]:
        """Read a few representative values, or nothing at all.

        Two gates before any row is touched: sampling must be enabled, and the
        column name must not look sensitive. Refusing here rather than
        filtering afterwards means the data never enters the process.
        """
        if not self._sample_values:
            return ()
        if is_sensitive(column, self._sensitive_patterns):
            logger.debug("skipping sample of sensitive column %s.%s", table, column)
            return ()

        # Identifiers cannot be bound as parameters, so they are composed with
        # sql.Identifier, which quotes and escapes them. Interpolating a
        # catalog-supplied name into the string would be injectable by anyone
        # who can create a table.
        query = sql.SQL(
            "SELECT DISTINCT left({col}::text, %s) AS value "
            "FROM (SELECT {col} FROM {rel} WHERE {col} IS NOT NULL LIMIT %s) AS scan "
            "ORDER BY value "
            "LIMIT %s"
        ).format(col=sql.Identifier(column), rel=sql.Identifier(self._schema, table))

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    query,
                    (self._sample_max_chars, self._sample_scan_limit, self._sample_count),
                )
                return tuple(row[0] for row in cur.fetchall())
        except psycopg.Error as exc:
            # One unsampleable column (an exotic type with no text cast, a
            # statement timeout on a huge table) must not stop the schema from
            # being indexed. The element is still catalogued, just without
            # examples -- degraded retrieval, not a failed startup.
            logger.warning("could not sample %s.%s: %s", table, column, exc)
            return ()
