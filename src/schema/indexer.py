"""Writing the schema catalog: serialize, embed, upsert.

This is the only writer of ``agent_meta.schema_elements``. It runs as the
*owner* role -- the read-only role has no privileges on ``agent_meta`` at all,
which is what stops generated SQL from reading or rewriting the catalog that
steers it.

Two properties matter more than speed here:

**Idempotent.** Re-indexing an unchanged schema leaves the catalog byte-for-byte
as it was. That is what makes indexing safe to run on every deploy rather than
a step someone has to remember.

**All or nothing.** One transaction. A half-written catalog is worse than a
stale one: retrieval would return a mix of old and new elements with no way to
tell which is which.

See docs/architecture/DATABASE.md sections 3 and 8.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any

from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg import Connection

from core.exceptions import EmbeddingModelMismatchError
from core.ports.embedder import Embedder
from schema.models import SchemaSnapshot
from schema.sensitivity import DEFAULT_SENSITIVE_PATTERNS
from schema.serialization import serialize_column, serialize_table

logger = logging.getLogger(__name__)

_VECTOR_DIM_RE = re.compile(r"^vector\((\d+)\)$")

_COLUMN_WIDTH_SQL = """
    SELECT format_type(a.atttypid, a.atttypmod)
    FROM pg_attribute a
    WHERE a.attrelid = 'agent_meta.schema_elements'::regclass
      AND a.attname = 'embedding'
"""

_UPSERT_SQL = """
    INSERT INTO agent_meta.schema_elements
        (dataset, element_type, table_name, column_name,
         data_type, comment, serialized, embedding, model_version)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT ON CONSTRAINT schema_elements_unique DO UPDATE SET
        element_type = EXCLUDED.element_type,
        data_type    = EXCLUDED.data_type,
        comment      = EXCLUDED.comment,
        serialized   = EXCLUDED.serialized,
        embedding    = EXCLUDED.embedding,
        updated_at   = now()
    RETURNING id
"""

_DELETE_STALE_SQL = """
    DELETE FROM agent_meta.schema_elements
    WHERE dataset = %s AND model_version = %s AND id <> ALL(%s)
"""

_INSERT_FK_SQL = """
    INSERT INTO agent_meta.foreign_keys
        (dataset, from_table, from_column, to_table, to_column, constraint_name)
    VALUES (%s, %s, %s, %s, %s, %s)
"""


@dataclass(frozen=True, slots=True)
class IndexReport:
    """What one indexing run did. Logged, and asserted on in tests."""

    model_version: str
    tables: int
    columns: int
    elements_written: int
    elements_removed: int
    foreign_keys: int


class SchemaIndexer:
    """Serializes, embeds and stores a schema snapshot.

    Args:
        connection: The **owner** connection. Writing to ``agent_meta``
            requires it, and nothing else in the request path has it.
        embedder: Supplies both the vectors and the ``model_version`` recorded
            beside them, so the label can never disagree with the model.
        dataset: Catalog namespace, from ``DATASET``.
        sensitive_patterns: Passed through to serialization as the second gate
            on sample values; introspection is the first.
    """

    def __init__(
        self,
        connection: Connection[Any],
        embedder: Embedder,
        *,
        dataset: str = "default",
        sensitive_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS,
    ) -> None:
        # Teaches psycopg how to adapt Python sequences to the vector type.
        # Without it every embedding would be sent as a text array and
        # rejected. Safe to call repeatedly.
        register_vector(connection)

        self._conn = connection
        self._embedder = embedder
        self._dataset = dataset
        self._sensitive_patterns = sensitive_patterns

    def index(self, snapshot: SchemaSnapshot) -> IndexReport:
        """Replace this dataset's catalog for this model version.

        Raises:
            EmbeddingModelMismatchError: the embedder's width does not match
                the catalog column.
        """
        self._assert_dimensions()

        rows = self._build_rows(snapshot)
        if not rows:
            logger.warning(
                "schema snapshot contained no readable elements; catalog left untouched",
                extra={"dataset": self._dataset},
            )
            return IndexReport(self._embedder.model_version, 0, 0, 0, 0, 0)

        vectors = self._embedder.embed([row[6] for row in rows])
        if len(vectors) != len(rows):
            raise EmbeddingModelMismatchError(
                f"embedder returned {len(vectors)} vectors for {len(rows)} elements"
            )

        with self._conn.transaction(), self._conn.cursor() as cur:
            written_ids: list[int] = []
            for row, vector in zip(rows, vectors, strict=True):
                # Vector(...), not the bare list: a list is adapted as
                # double precision[], which an INSERT happens to coerce to the
                # column type. Relying on that is how the read path ended up
                # unable to use the same value in an operator expression.
                cur.execute(_UPSERT_SQL, (*row, Vector(vector), self._embedder.model_version))
                result = cur.fetchone()
                if result is None:  # pragma: no cover - RETURNING always yields here
                    raise EmbeddingModelMismatchError("upsert returned no id")
                written_ids.append(int(result[0]))

            cur.execute(
                _DELETE_STALE_SQL,
                (self._dataset, self._embedder.model_version, written_ids),
            )
            removed = cur.rowcount

            # Join paths are not model-specific, so they are replaced wholesale
            # rather than upserted per model version.
            cur.execute("DELETE FROM agent_meta.foreign_keys WHERE dataset = %s", (self._dataset,))
            for fk in snapshot.foreign_keys:
                cur.execute(
                    _INSERT_FK_SQL,
                    (
                        self._dataset,
                        fk.from_table,
                        fk.from_column,
                        fk.to_table,
                        fk.to_column,
                        fk.constraint_name,
                    ),
                )

        report = IndexReport(
            model_version=self._embedder.model_version,
            tables=len(snapshot.tables),
            columns=snapshot.column_count,
            elements_written=len(written_ids),
            elements_removed=removed,
            foreign_keys=len(snapshot.foreign_keys),
        )
        # asdict, not vars: IndexReport uses slots=True and so has no __dict__.
        logger.info("schema catalog indexed", extra={"dataset": self._dataset, **asdict(report)})
        return report

    def _build_rows(self, snapshot: SchemaSnapshot) -> list[tuple[Any, ...]]:
        """One row per element, in the order the upsert expects.

        Order matters here and is easy to get wrong silently, which is why the
        serialized text is read back out by index as ``row[6]`` in exactly one
        place rather than rebuilt.
        """
        rows: list[tuple[Any, ...]] = []

        for table in snapshot.tables:
            rows.append(
                (
                    self._dataset,
                    "table",
                    table.name,
                    None,
                    None,
                    table.comment,
                    serialize_table(table),
                )
            )
            for column in table.columns:
                rows.append(
                    (
                        self._dataset,
                        "column",
                        column.table,
                        column.name,
                        column.data_type,
                        column.comment,
                        serialize_column(column, sensitive_patterns=self._sensitive_patterns),
                    )
                )

        return rows

    def _assert_dimensions(self) -> None:
        """Fail before writing, not during.

        pgvector rejects a wrong-width vector per row. Discovering the mismatch
        on row 200 of 800 inside a transaction would roll back cleanly but
        report a confusing per-row error instead of the real cause.
        """
        with self._conn.cursor() as cur:
            cur.execute(_COLUMN_WIDTH_SQL)
            row = cur.fetchone()

        if row is None:
            raise EmbeddingModelMismatchError(
                "agent_meta.schema_elements.embedding not found -- run migrations first"
            )

        match = _VECTOR_DIM_RE.match(row[0])
        if match is None:  # pragma: no cover - only if the column type changes
            raise EmbeddingModelMismatchError(f"embedding column has unexpected type {row[0]!r}")

        expected = int(match.group(1))
        actual = self._embedder.dimensions
        if actual != expected:
            raise EmbeddingModelMismatchError(
                f"embedder {self._embedder.model_version!r} produces {actual}-dimensional "
                f"vectors but agent_meta.schema_elements.embedding is vector({expected}). "
                f"Changing the retriever's width is a migration, not a config change."
            )


def assert_catalog_ready(
    connection: Connection[Any],
    *,
    dataset: str,
    model_version: str,
) -> int:
    """Fail at startup if the configured retriever has no vectors indexed.

    Without this the service starts happily and returns *plausible* results:
    the question is embedded by one model and compared against vectors from
    another, so every distance is meaningless but no query errors. Silent
    quality loss is far more expensive to diagnose than a refused startup.

    Returns:
        The number of indexed elements.

    Raises:
        EmbeddingModelMismatchError: nothing is indexed for this combination.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM agent_meta.schema_elements "
            "WHERE dataset = %s AND model_version = %s",
            (dataset, model_version),
        )
        row = cur.fetchone()

    count = int(row[0]) if row else 0
    if count == 0:
        raise EmbeddingModelMismatchError(
            f"no schema elements indexed for dataset {dataset!r} with model "
            f"{model_version!r}. Run the indexer, or point RETRIEVER_MODEL at "
            f"a model that has been indexed."
        )
    return count
