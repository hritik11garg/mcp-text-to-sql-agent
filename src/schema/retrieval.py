"""Retrieval over the schema catalog: question in, ranked elements out.

This is the consumer of everything the indexer wrote, and the component whose
Recall@k puts a hard ceiling on execution accuracy -- the model cannot write
correct SQL against a column it was never shown. See docs/ml/EVALUATION.md
section 2.

Two properties are enforced here rather than trusted:

**Vector spaces never mix.** Every query filters on ``(dataset, model_version)``
before ranking. Comparing a question embedded by one model against vectors from
another produces distances that are meaningless but never *wrong-looking*, so
this failure is silent by construction.

**The caller never widens a limit.** ``k`` is clamped and ``table_filter`` is
bounded, because in the finished system both arrive from a language model.

Runs on the **owner** connection, not the read-only one: the read-only role has
no privileges on ``agent_meta`` at all, which is exactly what stops generated
SQL from reading or rewriting the catalog that steers it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg import Connection

from core.exceptions import RetrievalError
from core.ports.embedder import Embedder
from core.settings import RetrievalSettings
from schema.models import ForeignKey

logger = logging.getLogger(__name__)

DEFAULT_K = 10
MAX_K = 50
"""Ceiling on results per search, matching the ``schema_search`` tool schema in
docs/architecture/MCP.md section 3.1. A limit the code enforces is a limit; a
limit in a tool description is a suggestion."""

MAX_TABLE_FILTER = 50
"""Ceiling on ``table_filter`` entries. Bounds the array parameter so a caller
cannot turn one search into an arbitrarily large query."""

DEFAULT_EF_SEARCH = 40
MAX_EF_SEARCH = 1_000

# SET LOCAL cannot take a bound parameter; set_config can, and is equivalent.
_EF_SEARCH_SQL = "SELECT set_config('hnsw.ef_search', %s, true)"

_ITERATIVE_SCAN_SQL = "SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"
"""Keep filtering from starving the scan.

The ``(dataset, model_version)`` predicate reads like a pre-filter but the
planner applies it to rows the HNSW scan has *already* returned. With
``iterative_scan`` off -- pgvector's default -- the scan stops after its
candidate list is exhausted, so a selective filter silently yields fewer than
``k`` rows: measured at 6 rows for ``k=10`` over two datasets of 5,000 elements
each. Fewer candidates is lower Recall@k, and Recall@k is the ceiling on
execution accuracy.

``relaxed_order`` rather than ``strict_order`` because results are re-sorted by
score in :func:`_rank` anyway, and it is the cheaper of the two. The scan stays
bounded by pgvector's own ``hnsw.max_scan_tuples``.
"""

_ITERATIVE_SCAN_SUPPORTED_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_settings WHERE name = 'hnsw.iterative_scan')"
)

_SEARCH_TEMPLATE = """
    SELECT element_type, table_name, column_name, data_type, comment, serialized,
           1 - (embedding <=> %(vec)s) AS score
    FROM agent_meta.schema_elements
    WHERE dataset = %(dataset)s
      AND model_version = %(model_version)s
      AND embedding IS NOT NULL
      {table_clause}
    ORDER BY embedding <=> %(vec)s
    LIMIT %(k)s
"""

# Two statements rather than one with an "OR %(tables)s IS NULL" clause: that
# form is opaque to the planner, which then cannot tell a filtered search from
# an unfiltered one.
_SEARCH_SQL = _SEARCH_TEMPLATE.format(table_clause="")
_SEARCH_FILTERED_SQL = _SEARCH_TEMPLATE.format(table_clause="AND table_name = ANY(%(tables)s)")

_FOREIGN_KEYS_SQL = """
    SELECT from_table, from_column, to_table, to_column, constraint_name
    FROM agent_meta.foreign_keys
    WHERE dataset = %s
      AND from_table = ANY(%s)
      AND to_table = ANY(%s)
    ORDER BY from_table, from_column, to_table, to_column
"""


@dataclass(frozen=True, slots=True)
class RetrievedElement:
    """One catalog hit, with the score that put it there.

    ``serialized`` is carried through deliberately: it is the exact text that
    was embedded, so a bad result can be explained without re-running
    introspection or guessing what the vector saw.
    """

    element_type: str
    table: str
    column: str | None
    data_type: str | None
    comment: str | None
    serialized: str
    score: float

    @property
    def qualified_name(self) -> str:
        return self.table if self.column is None else f"{self.table}.{self.column}"


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Ranked elements plus the join paths between the tables they belong to."""

    elements: tuple[RetrievedElement, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()

    @property
    def tables(self) -> tuple[str, ...]:
        """Distinct table names, in rank order -- best-matching table first."""
        return tuple(dict.fromkeys(element.table for element in self.elements))


class SchemaRetriever:
    """Ranks catalog elements against a natural-language question.

    Args:
        connection: The **owner** connection. ``agent_meta`` is unreadable to
            the read-only role by design.
        embedder: Must be the same one that indexed the catalog. It supplies
            the ``model_version`` filter as well as the query vector, so the
            two cannot drift apart.
        dataset: Catalog namespace, from ``DATASET``.
        default_k: Used when a caller does not specify one.
        ef_search: HNSW search breadth. Higher is more accurate and slower.
    """

    def __init__(
        self,
        connection: Connection[Any],
        embedder: Embedder,
        *,
        dataset: str = "default",
        default_k: int = DEFAULT_K,
        ef_search: int = DEFAULT_EF_SEARCH,
    ) -> None:
        register_vector(connection)

        self._conn = connection
        self._embedder = embedder
        self._dataset = dataset
        self._default_k = _clamp(default_k, 1, MAX_K)
        self._ef_search = _clamp(ef_search, 1, MAX_EF_SEARCH)
        self._iterative_scan = self._supports_iterative_scan()

    @property
    def model_version(self) -> str:
        """The vector space this retriever searches. Worth logging on startup."""
        return self._embedder.model_version

    def search(
        self,
        query: str,
        *,
        k: int | None = None,
        table_filter: Sequence[str] | None = None,
    ) -> RetrievalResult:
        """Find the schema elements most relevant to ``query``.

        Args:
            query: A question or phrase. Must not be blank.
            k: Result count, clamped to ``MAX_K``. ``None`` uses ``default_k``.
            table_filter: Restrict results to these tables. ``None`` means no
                restriction; an empty sequence is rejected rather than treated
                as one, because silently widening a filter is the wrong way to
                be wrong.

        Raises:
            ValueError: Blank query, or an unusable ``table_filter``.
            RetrievalError: The query embedded to a zero vector.
        """
        text = query.strip()
        if not text:
            raise ValueError("query must not be blank")

        tables = _clean_table_filter(table_filter)
        limit = _clamp(self._default_k if k is None else k, 1, MAX_K)
        vector = self._embed_query(text)

        params: dict[str, Any] = {
            # Vector(...), not the bare list. A list is adapted as
            # double precision[], and while an INSERT coerces that to the
            # column's type, `<=>` resolves operators by argument type and
            # finds none -- so the write path works and the read path does not.
            "vec": Vector(vector),
            "dataset": self._dataset,
            "model_version": self._embedder.model_version,
            "k": limit,
        }
        statement = _SEARCH_SQL
        if tables is not None:
            statement = _SEARCH_FILTERED_SQL
            params["tables"] = list(tables)

        started = time.perf_counter()
        with self._conn.transaction(), self._conn.cursor() as cur:
            # ef_search must be at least k or HNSW silently returns fewer, and
            # worse, candidates than asked for.
            cur.execute(_EF_SEARCH_SQL, (str(max(self._ef_search, limit)),))
            if self._iterative_scan:
                cur.execute(_ITERATIVE_SCAN_SQL)
            cur.execute(statement, params)
            elements = _rank(cur.fetchall())

            edges = self._foreign_keys(cur, tuple(dict.fromkeys(e.table for e in elements)))
        duration_ms = (time.perf_counter() - started) * 1000

        logger.info(
            "schema search",
            extra={
                "dataset": self._dataset,
                "model_version": self._embedder.model_version,
                "k": limit,
                "elements": len(elements),
                "foreign_keys": len(edges),
                "filtered": tables is not None,
                "duration_ms": round(duration_ms, 2),
            },
        )
        return RetrievalResult(elements=elements, foreign_keys=edges)

    def _supports_iterative_scan(self) -> bool:
        """Ask the server rather than parsing an extension version string.

        ``hnsw.iterative_scan`` arrived in pgvector 0.8. On an older server the
        setting simply does not exist, and setting an unknown GUC under a
        registered prefix warns instead of failing -- which would hide the
        degradation. Checking the capability makes it visible once, at startup.
        """
        with self._conn.cursor() as cur:
            cur.execute(_ITERATIVE_SCAN_SUPPORTED_SQL)
            row = cur.fetchone()

        supported = bool(row and row[0])
        if not supported:
            logger.warning(
                "pgvector does not support hnsw.iterative_scan; filtered searches "
                "may return fewer than k elements, which lowers Recall@k silently. "
                "Upgrade to pgvector 0.8 or later.",
                extra={"dataset": self._dataset},
            )
        return supported

    def _embed_query(self, text: str) -> list[float]:
        vectors = self._embedder.embed([text])
        if len(vectors) != 1:
            raise RetrievalError(f"embedder returned {len(vectors)} vectors for one query")

        vector = vectors[0]
        if not any(vector):
            # Cosine distance is undefined against a zero vector: pgvector
            # returns NaN, every row ties, and the "ranking" is whatever order
            # the scan happened to produce. Refusing is the only honest answer.
            raise RetrievalError(
                f"query {text!r} produced a zero vector -- it is too short or "
                f"has no content the retriever recognises"
            )
        return vector

    def _foreign_keys(self, cur: Any, tables: tuple[str, ...]) -> tuple[ForeignKey, ...]:
        """Join paths where **both** ends were retrieved.

        A one-ended edge names a table whose columns are not in the result, so
        the model would have to invent them to use it -- which is the failure
        returning edges is meant to prevent. The cost is that a join through an
        un-retrieved bridge table is not offered; raising ``k`` is the lever,
        and whether it is needed is a Stage 5 measurement, not a guess.
        """
        if not tables:
            return ()

        names = list(tables)
        cur.execute(_FOREIGN_KEYS_SQL, (self._dataset, names, names))
        return tuple(
            ForeignKey(
                from_table=row[0],
                from_column=row[1],
                to_table=row[2],
                to_column=row[3],
                constraint_name=row[4],
            )
            for row in cur.fetchall()
        )


def build_retriever(
    connection: Connection[Any],
    embedder: Embedder,
    settings: RetrievalSettings,
) -> SchemaRetriever:
    """Compose a retriever from configuration.

    The composition point, so callers depend on the class and not on how it is
    configured. The embedder is passed in rather than built here because the
    *same instance* must have indexed the catalog -- see the module docstring
    of :mod:`schema`.
    """
    return SchemaRetriever(
        connection,
        embedder,
        dataset=settings.dataset,
        default_k=settings.retrieval_top_k,
        ef_search=settings.hnsw_ef_search,
    )


def _rank(rows: Sequence[tuple[Any, ...]]) -> tuple[RetrievedElement, ...]:
    """Order the returned rows deterministically.

    The tiebreak is applied here rather than in ``ORDER BY`` on purpose. A
    second sort key in SQL forces the planner to sort the whole filtered set
    before applying ``LIMIT``, which discards the ANN index scan and turns a
    bounded lookup into a full scan. Sorting k rows in Python costs nothing and
    keeps output -- and therefore the prompt built from it -- stable.
    """
    elements = [
        RetrievedElement(
            element_type=row[0],
            table=row[1],
            column=row[2],
            data_type=row[3],
            comment=row[4],
            serialized=row[5],
            score=float(row[6]),
        )
        for row in rows
    ]
    elements.sort(key=lambda e: (-e.score, e.table, e.column or ""))
    return tuple(elements)


def _clean_table_filter(table_filter: Sequence[str] | None) -> tuple[str, ...] | None:
    if table_filter is None:
        return None

    cleaned = tuple(dict.fromkeys(name.strip() for name in table_filter))
    if not cleaned or any(not name for name in cleaned):
        raise ValueError("table_filter must contain non-empty table names, or be None")
    if len(cleaned) > MAX_TABLE_FILTER:
        raise ValueError(f"table_filter accepts at most {MAX_TABLE_FILTER} tables")
    return cleaned


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))
