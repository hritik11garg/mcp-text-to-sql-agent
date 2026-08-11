"""``python -m demo.seed`` -- load the demo schema and index it.

Idempotent by construction: the schema is dropped and rebuilt, and the indexer
already upserts on ``(dataset, table_name, column_name, model_version)``. Running
it twice is the same as running it once, which matters because the compose stack
runs it on every ``up`` and a seeder that accumulated rows would quietly make the
demo different on the third start.

**Two roles, as everywhere else in this project.** Creating the schema is an
owner operation. Introspection runs as the *read-only* role, because the catalog
must describe what the agent can actually reach -- a table indexed as the owner
and unreadable by the agent produces retrieval hits whose SQL is always refused.
That is the same reasoning as ``benchmark.load index``, and it is repeated here
rather than shared because the two commands have different inputs and the shared
part is three lines.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

import psycopg
from psycopg import sql

from adapters.embedding.factory import build_embedder
from core.dsn import libpq_dsn, redact_dsn
from core.exceptions import ConfigurationError
from core.settings import DatabaseSettings, RetrievalSettings
from demo import dataset
from schema.indexer import SchemaIndexer
from schema.introspection import PostgresIntrospector

logger = logging.getLogger("demo.seed")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DATABASE = 3

READONLY_ROLE = "sql_agent_ro"


def _connect(url: object, variable: str) -> psycopg.Connection[Any]:
    if url is None:
        raise ConfigurationError(f"{variable} is required to seed the demo dataset")
    try:
        # `libpq_dsn` unwraps the SecretStr itself. Calling `str()` on one first
        # yields `**********`, which libpq then rejects as a malformed DSN --
        # a redaction working exactly as designed, one layer too early.
        return psycopg.connect(libpq_dsn(url), autocommit=True)
    except psycopg.Error as exc:
        # psycopg quotes the DSN it was handed in parse errors, password
        # included. SECURITY.md section 14.2.10.
        raise ConfigurationError(f"{variable}: {redact_dsn(str(exc))}") from None


def create_schema(owner: psycopg.Connection[Any], schema: str) -> None:
    """Drop and rebuild, then grant the read-only role exactly SELECT."""
    name = sql.Identifier(schema)
    owner.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(name))
    owner.execute(sql.SQL("CREATE SCHEMA {}").format(name))

    # The DDL is written unqualified so that `REFERENCES venue(id)` reads as it
    # would in a migration, which means `search_path` has to carry the new
    # schema while it runs -- and then be put back.
    #
    # Putting it back is the part worth the comment. `search_path` is *session*
    # state on a connection this function does not own: leaving it set took the
    # schema holding the `vector` type off the path, and the failure surfaced
    # later, in the indexer, as "vector type not found in the database" on a
    # connection that had been working a moment earlier.
    try:
        owner.execute(sql.SQL("SET search_path TO {}").format(name))
        for statement in dataset.DDL:
            owner.execute(sql.SQL(statement))
    finally:
        owner.execute(sql.SQL("RESET search_path"))

    for table, column, comment in dataset.COMMENTS:
        target = (
            sql.SQL("TABLE {}").format(sql.Identifier(schema, table))
            if column is None
            else sql.SQL("COLUMN {}").format(sql.Identifier(schema, table, column))
        )
        owner.execute(sql.SQL("COMMENT ON {} IS {}").format(target, sql.Literal(comment)))

    owner.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(name, sql.Identifier(READONLY_ROLE))
    )
    owner.execute(
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
            name, sql.Identifier(READONLY_ROLE)
        )
    )


def load_rows(owner: psycopg.Connection[Any], schema: str, rows: dataset.Rows) -> dict[str, int]:
    """Insert every row. ``COPY`` would be faster and this is 400 rows."""
    counts: dict[str, int] = {}
    for table, records in (
        ("venue", rows.venues),
        ("artist", rows.artists),
        ("event", rows.events),
    ):
        if not records:  # pragma: no cover - the generator always produces rows
            continue
        placeholders = sql.SQL(", ").join(sql.Placeholder() * len(records[0]))
        statement = sql.SQL("INSERT INTO {} VALUES ({})").format(
            sql.Identifier(schema, table), placeholders
        )
        with owner.cursor() as cur:
            cur.executemany(statement, records)
        counts[table] = len(records)
    return counts


def index(
    owner: psycopg.Connection[Any],
    readonly: psycopg.Connection[Any],
    schema: str,
    settings: RetrievalSettings,
) -> int:
    """Build the retrieval catalog, introspecting as the role that will query."""
    introspector = PostgresIntrospector(
        readonly,
        schema=schema,
        sample_values=settings.schema_sample_values,
        sample_count=settings.schema_sample_count,
        sample_max_chars=settings.schema_sample_max_chars,
        sample_scan_limit=settings.schema_sample_scan_limit,
    )
    report = SchemaIndexer(owner, build_embedder(settings), dataset=schema).index(
        introspector.snapshot()
    )
    return report.elements_written


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)-8s %(message)s")

    parser = argparse.ArgumentParser(
        prog="python -m demo.seed",
        description="Load the demo dataset and index it, so the API has something to answer from.",
    )
    parser.add_argument(
        "--schema",
        default=dataset.SCHEMA,
        help=(
            "Schema to create, and the catalog `dataset` namespace. Must match "
            "DATASET, or the retriever filters on a namespace with no rows"
        ),
    )
    args = parser.parse_args(argv)

    try:
        database = DatabaseSettings()
        retrieval = RetrievalSettings()
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return EXIT_USAGE

    try:
        owner = _connect(database.database_url, "DATABASE_URL")
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return EXIT_DATABASE

    try:
        try:
            readonly = _connect(database.database_ro_url, "DATABASE_RO_URL")
        except ConfigurationError as exc:
            logger.error("%s", exc)
            return EXIT_DATABASE

        try:
            rows = dataset.build()
            create_schema(owner, args.schema)
            counts = load_rows(owner, args.schema, rows)
            logger.info("loaded %s into schema %s", counts, args.schema)

            written = index(owner, readonly, args.schema, retrieval)
            logger.info("indexed %d catalog element(s) for dataset=%s", written, args.schema)
        finally:
            readonly.close()
    finally:
        owner.close()

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
