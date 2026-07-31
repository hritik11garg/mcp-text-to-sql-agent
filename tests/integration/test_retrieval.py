"""Retrieval against a real catalog in a real PostgreSQL.

The properties under test are the ones a fake connection cannot show:
ANN ordering, the ``(dataset, model_version)`` pre-filter, and the join-path
expansion. All three are enforced by the database or by pgvector.

Relevance itself is *not* under test here. ``HashingEmbedder`` has no semantic
understanding, so these tests prove the plumbing carries scores in the right
direction -- quality is Recall@k on Spider/BIRD in Stage 5.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

import psycopg
import pytest
from pgvector import Vector

from adapters.embedding.hashing import HashingEmbedder
from core.exceptions import RetrievalError
from core.settings import RetrievalSettings
from schema.indexer import SchemaIndexer
from schema.introspection import PostgresIntrospector
from schema.models import SchemaSnapshot
from schema.retrieval import MAX_K, SchemaRetriever, build_retriever

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[tuple[object, ...]]

DATASET = "test_retrieval"
STRAY_DATASET = "test_retrieval_elsewhere"

STARVE_DATASET = "test_retrieval_starvation"
STARVE_ROWS = 2_400
"""Just past the point where the planner stops preferring a sequential scan.
Below it the query is exact and the behaviour under test cannot occur."""

NEAR_MISS_QUERY = "customer order revenue by country total"
"""Matches the vocabulary of the dataset that is filtered *out*, so every
nearby candidate is one the filter discards. That is the shape that starves."""


class ZeroEmbedder:
    """An embedder that returns a zero vector. A legal port implementation.

    Nothing in the ``Embedder`` contract forbids one, and cosine distance
    against it is undefined -- so the retriever has to refuse rather than trust.
    """

    @property
    def model_version(self) -> str:
        return "hashing-trigram-384"

    @property
    def dimensions(self) -> int:
        return 384

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


@pytest.fixture(scope="module")
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture
def indexed(
    owner_connection: Conn,
    ro_connection: Conn,
    embedder: HashingEmbedder,
    catalog_schema: None,
) -> SchemaSnapshot:
    """A freshly indexed catalog for this dataset only.

    ``STRAY_DATASET`` is cleaned too: the isolation tests move rows out of this
    dataset to prove the filter works, and rows moved out are no longer caught
    by a delete scoped to it.
    """
    owner_connection.execute(
        "DELETE FROM agent_meta.schema_elements WHERE dataset = ANY(%s)",
        ([DATASET, STRAY_DATASET],),
    )
    owner_connection.execute("DELETE FROM agent_meta.foreign_keys WHERE dataset = %s", (DATASET,))

    snapshot = PostgresIntrospector(ro_connection, schema="public").snapshot()
    SchemaIndexer(owner_connection, embedder, dataset=DATASET).index(snapshot)
    return snapshot


@pytest.fixture
def retriever(
    owner_connection: Conn, embedder: HashingEmbedder, indexed: SchemaSnapshot
) -> SchemaRetriever:
    return SchemaRetriever(owner_connection, embedder, dataset=DATASET)


@pytest.fixture(scope="module")
def starvation_corpus(owner_connection: Conn, embedder: HashingEmbedder) -> Iterator[None]:
    """A corpus big enough for the planner to choose the HNSW index.

    Two datasets with deliberately different vocabularies, so the half that
    survives the filter sits *away* from the query in vector space. Below
    roughly 2,000 rows Postgres prefers a sequential scan, which is exact and
    therefore hides the behaviour entirely -- the corpus has to be large enough
    for the index to be worth using.
    """
    texts = [
        f"warehouse{i}.sku_{i} (text) -- inventory shelf bin pallet {i}"
        if i % 2 == 0
        else f"ledger{i}.customer_order_revenue_{i} (numeric) -- country total {i}"
        for i in range(STARVE_ROWS)
    ]
    vectors = embedder.embed(texts)
    rows = [
        (
            STARVE_DATASET if i % 2 == 0 else STRAY_DATASET,
            f"warehouse{i}" if i % 2 == 0 else f"ledger{i}",
            f"col{i}",
            text,
            Vector(vector),
            embedder.model_version,
        )
        for i, (text, vector) in enumerate(zip(texts, vectors, strict=True))
    ]

    with owner_connection.cursor() as cur:
        cur.executemany(
            "INSERT INTO agent_meta.schema_elements "
            "(dataset, element_type, table_name, column_name, serialized, "
            " embedding, model_version) VALUES (%s, 'column', %s, %s, %s, %s, %s)",
            rows,
        )
    owner_connection.execute("ANALYZE agent_meta.schema_elements")
    try:
        yield
    finally:
        owner_connection.execute(
            "DELETE FROM agent_meta.schema_elements WHERE dataset = ANY(%s)",
            ([STARVE_DATASET, STRAY_DATASET],),
        )


class TestSearch:
    def test_returns_ranked_elements(self, retriever: SchemaRetriever) -> None:
        result = retriever.search("order total amount")

        assert result.elements
        scores = [element.score for element in result.elements]
        assert scores == sorted(scores, reverse=True)

    def test_exact_serialized_text_ranks_first(
        self, retriever: SchemaRetriever, owner_connection: Conn
    ) -> None:
        """Searching an element's own embedded text must return that element.

        A weak assertion about relevance but a strong one about wiring: it can
        only hold if the same text was embedded on both paths, the vector
        survived the round trip through pgvector, and cosine distance is being
        read in the right direction.
        """
        row = owner_connection.execute(
            "SELECT serialized FROM agent_meta.schema_elements "
            "WHERE dataset = %s AND table_name = 'orders' AND column_name = 'total_amount'",
            (DATASET,),
        ).fetchone()
        assert row is not None

        best = retriever.search(row[0]).elements[0]

        assert best.qualified_name == "orders.total_amount"
        assert best.score == pytest.approx(1.0)

    def test_carries_the_fields_the_prompt_needs(self, retriever: SchemaRetriever) -> None:
        result = retriever.search("customers.country (text)")
        match = next(e for e in result.elements if e.qualified_name == "customers.country")

        assert match.element_type == "column"
        assert match.data_type == "text"
        assert match.comment == "ISO 3166-1 alpha-2 country code"
        assert match.serialized.startswith("customers.country (text)")

    def test_scores_are_cosine_similarities(self, retriever: SchemaRetriever) -> None:
        for element in retriever.search("customer").elements:
            assert -1.0 <= element.score <= 1.0

    def test_unreadable_tables_are_not_retrievable(self, retriever: SchemaRetriever) -> None:
        """internal_payroll is revoked from the read-only role, so it was never
        indexed -- retrieval must not be able to surface it under any query."""
        result = retriever.search("internal payroll amount", k=MAX_K)

        assert "internal_payroll" not in result.tables


class TestLimits:
    def test_k_bounds_the_result_count(self, retriever: SchemaRetriever) -> None:
        assert len(retriever.search("customer", k=3).elements) == 3

    def test_k_is_clamped_to_the_ceiling(self, retriever: SchemaRetriever) -> None:
        """The caller is a language model in the finished system. A limit it
        can raise past the ceiling is not a limit."""
        result = retriever.search("customer", k=10_000)
        assert len(result.elements) <= MAX_K

    def test_k_below_one_is_clamped_up(self, retriever: SchemaRetriever) -> None:
        assert len(retriever.search("customer", k=0).elements) == 1

    def test_default_k_is_used_when_unspecified(
        self, owner_connection: Conn, embedder: HashingEmbedder, indexed: SchemaSnapshot
    ) -> None:
        retriever = SchemaRetriever(owner_connection, embedder, dataset=DATASET, default_k=2)
        assert len(retriever.search("customer").elements) == 2

    def test_settings_drive_the_composed_retriever(
        self, owner_connection: Conn, embedder: HashingEmbedder, indexed: SchemaSnapshot
    ) -> None:
        """Configuration that nothing reads is worse than no configuration."""
        settings = RetrievalSettings(dataset=DATASET, retrieval_top_k=4, hnsw_ef_search=64)
        retriever = build_retriever(owner_connection, embedder, settings)

        assert len(retriever.search("customer").elements) == 4


class TestTableFilter:
    def test_restricts_results_to_the_named_tables(self, retriever: SchemaRetriever) -> None:
        result = retriever.search("id", k=MAX_K, table_filter=["customers"])

        assert result.tables == ("customers",)
        assert result.elements

    def test_unknown_table_yields_no_elements(self, retriever: SchemaRetriever) -> None:
        result = retriever.search("id", table_filter=["no_such_table"])

        assert result.elements == ()
        assert result.foreign_keys == ()

    def test_filter_is_a_value_not_an_identifier(self, retriever: SchemaRetriever) -> None:
        """Table names are bound as an array parameter, never composed into SQL.

        This one would be an injection point if it were built by string
        formatting, because in the finished system the value is chosen by a
        language model reading user text.
        """
        hostile = "customers'; DROP TABLE agent_meta.schema_elements; --"
        result = retriever.search("id", table_filter=[hostile])

        assert result.elements == ()
        # The catalog is intact: a following search still works.
        assert retriever.search("customer").elements


class TestIsolation:
    def test_other_model_versions_are_invisible(
        self, retriever: SchemaRetriever, owner_connection: Conn
    ) -> None:
        """Vectors from two models are not comparable, so mixing them degrades
        retrieval without ever erroring. The filter is the only thing between
        this project and that failure."""
        owner_connection.execute(
            "UPDATE agent_meta.schema_elements SET model_version = 'pretend-other' "
            "WHERE dataset = %s",
            (DATASET,),
        )
        assert retriever.search("customer").elements == ()

    def test_other_datasets_are_invisible(
        self, retriever: SchemaRetriever, owner_connection: Conn
    ) -> None:
        owner_connection.execute(
            "UPDATE agent_meta.schema_elements SET dataset = %s WHERE dataset = %s",
            (STRAY_DATASET, DATASET),
        )
        assert retriever.search("customer").elements == ()

    def test_rows_without_a_vector_are_skipped(
        self, retriever: SchemaRetriever, owner_connection: Conn
    ) -> None:
        """The column is nullable, so a partially written row must not rank."""
        owner_connection.execute(
            "UPDATE agent_meta.schema_elements SET embedding = NULL "
            "WHERE dataset = %s AND table_name = 'customers'",
            (DATASET,),
        )
        assert "customers" not in retriever.search("customer", k=MAX_K).tables


class TestForeignKeys:
    def test_edges_between_retrieved_tables_are_returned(self, retriever: SchemaRetriever) -> None:
        """Two tables without the path between them leaves the model to invent
        the join condition."""
        result = retriever.search("customer", k=MAX_K)

        assert {"orders", "customers"} <= set(result.tables)
        edge = next(fk for fk in result.foreign_keys if fk.from_table == "orders")
        assert (edge.from_column, edge.to_table, edge.to_column) == (
            "customer_id",
            "customers",
            "id",
        )

    def test_one_ended_edges_are_omitted(self, retriever: SchemaRetriever) -> None:
        """An edge to a table whose columns were not retrieved is not a usable
        join path -- the model would have to invent the far side."""
        result = retriever.search("id", k=MAX_K, table_filter=["orders"])

        assert result.tables == ("orders",)
        assert result.foreign_keys == ()

    def test_no_elements_means_no_edges(self, retriever: SchemaRetriever) -> None:
        assert retriever.search("id", table_filter=["no_such_table"]).foreign_keys == ()


class TestFilterStarvation:
    """The ``(dataset, model_version)`` predicate is not really a pre-filter.

    ``EXPLAIN`` shows it as a ``Filter`` applied to rows the HNSW scan has
    already produced. With pgvector's default ``hnsw.iterative_scan = off`` the
    scan stops once its candidate list is exhausted, so a filter that removes
    most candidates leaves fewer than ``k`` rows -- with no error.

    It only bites when the filter *correlates with position in vector space*,
    which is why it needs a corpus shaped like this one rather than random
    vectors. That correlation is the normal case, not the exotic one: a second
    dataset has its own vocabulary, and a re-index under a new ``model_version``
    puts a whole second corpus in its own region -- exactly the state
    DATABASE.md section 10 asks for during a model rollout.
    """

    def test_a_correlated_filter_still_returns_k_elements(
        self, starvation_corpus: None, owner_connection: Conn, embedder: HashingEmbedder
    ) -> None:
        retriever = SchemaRetriever(owner_connection, embedder, dataset=STARVE_DATASET)

        result = retriever.search(NEAR_MISS_QUERY, k=10)

        assert len(result.elements) == 10
        assert set(result.tables) <= {f"warehouse{i}" for i in range(STARVE_ROWS)}

    def test_without_iterative_scan_the_same_search_starves(
        self, starvation_corpus: None, owner_connection: Conn, embedder: HashingEmbedder
    ) -> None:
        """Proves the test above is load-bearing rather than passing by luck.

        Measured on this corpus: the default returns *zero* of ten rows. A
        regression test that cannot fail is not a regression test.
        """
        retriever = SchemaRetriever(owner_connection, embedder, dataset=STARVE_DATASET)
        retriever._iterative_scan = False  # reaching in is the point of the test

        starved = retriever.search(NEAR_MISS_QUERY, k=10)

        assert len(starved.elements) < 10


class TestBadInput:
    @pytest.mark.parametrize("query", ["", "   ", "\n\t"])
    def test_blank_queries_are_refused(self, retriever: SchemaRetriever, query: str) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            retriever.search(query)

    def test_zero_vector_is_refused(self, owner_connection: Conn, indexed: SchemaSnapshot) -> None:
        """Ranking on NaN would return rows in scan order and call it relevance."""
        retriever = SchemaRetriever(owner_connection, ZeroEmbedder(), dataset=DATASET)

        with pytest.raises(RetrievalError, match="zero vector"):
            retriever.search("anything")

    def test_empty_table_filter_is_refused(self, retriever: SchemaRetriever) -> None:
        with pytest.raises(ValueError, match="non-empty table names"):
            retriever.search("customer", table_filter=[])


class TestLatency:
    def test_search_stays_well_inside_the_budget(self, retriever: SchemaRetriever) -> None:
        """Guards against an accidental N+1 or a full scan, not against the
        p95 target.

        The corpus here is a handful of vectors, so a passing number says
        nothing about behaviour at scale -- the < 100 ms p95 in
        docs/operations/PERFORMANCE.md is measured in Stage 6 against a real
        corpus. What this catches is a regression that adds a round trip per
        element, which would show up even at this size.
        """
        retriever.search("warm up the connection")

        started = time.perf_counter()
        for _ in range(5):
            retriever.search("customer order totals by country", k=MAX_K)
        average_ms = (time.perf_counter() - started) * 1000 / 5

        assert average_ms < 100
