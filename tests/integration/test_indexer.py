"""The indexer against a real catalog.

Idempotency and atomicity are the properties under test. Both are the kind
that hold in a unit test with a fake connection and fail against Postgres,
because both are enforced by the database.
"""

from __future__ import annotations

import psycopg
import pytest

from adapters.embedding.hashing import HashingEmbedder
from core.exceptions import EmbeddingModelMismatchError
from schema.indexer import SchemaIndexer, assert_catalog_ready
from schema.introspection import PostgresIntrospector
from schema.models import Column, SchemaSnapshot, Table

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[tuple[object, ...]]

DATASET = "test_indexer"


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture
def indexer(
    owner_connection: Conn, embedder: HashingEmbedder, catalog_schema: None
) -> SchemaIndexer:
    owner_connection.execute(
        "DELETE FROM agent_meta.schema_elements WHERE dataset = %s", (DATASET,)
    )
    owner_connection.execute("DELETE FROM agent_meta.foreign_keys WHERE dataset = %s", (DATASET,))
    return SchemaIndexer(owner_connection, embedder, dataset=DATASET)


@pytest.fixture
def snapshot(ro_connection: Conn, catalog_schema: None) -> SchemaSnapshot:
    return PostgresIntrospector(ro_connection, schema="public").snapshot()


def count_elements(conn: Conn, model_version: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM agent_meta.schema_elements WHERE dataset = %s AND model_version = %s",
        (DATASET, model_version),
    ).fetchone()
    return int(row[0]) if row else 0


class TestIndexing:
    def test_writes_one_row_per_element(
        self, indexer: SchemaIndexer, snapshot: SchemaSnapshot, owner_connection: Conn
    ) -> None:
        report = indexer.index(snapshot)

        assert report.elements_written == len(snapshot.tables) + snapshot.column_count
        assert count_elements(owner_connection, report.model_version) == report.elements_written

    def test_stores_the_serialized_text_next_to_the_vector(
        self, indexer: SchemaIndexer, snapshot: SchemaSnapshot, owner_connection: Conn
    ) -> None:
        # Storing the text is what makes a bad retrieval result explainable
        # later without re-running introspection.
        indexer.index(snapshot)
        row = owner_connection.execute(
            "SELECT serialized, embedding FROM agent_meta.schema_elements "
            "WHERE dataset = %s AND table_name = 'orders' AND column_name = 'total_amount'",
            (DATASET,),
        ).fetchone()

        assert row is not None
        assert row[0].startswith("orders.total_amount (numeric(12,2))")
        assert row[1] is not None

    def test_table_elements_have_no_column_name(
        self, indexer: SchemaIndexer, snapshot: SchemaSnapshot, owner_connection: Conn
    ) -> None:
        indexer.index(snapshot)
        row = owner_connection.execute(
            "SELECT count(*) FROM agent_meta.schema_elements "
            "WHERE dataset = %s AND element_type = 'table' AND column_name IS NOT NULL",
            (DATASET,),
        ).fetchone()
        assert row is not None
        assert row[0] == 0

    def test_foreign_keys_are_stored(
        self, indexer: SchemaIndexer, snapshot: SchemaSnapshot, owner_connection: Conn
    ) -> None:
        indexer.index(snapshot)
        row = owner_connection.execute(
            "SELECT to_table, to_column FROM agent_meta.foreign_keys "
            "WHERE dataset = %s AND from_table = 'orders' AND from_column = 'customer_id'",
            (DATASET,),
        ).fetchone()
        assert row == ("customers", "id")


class TestIdempotency:
    """Re-indexing an unchanged schema must be a no-op.

    This is what makes indexing safe to run on every deploy instead of being a
    step someone has to remember.
    """

    def test_second_run_writes_no_duplicates(
        self, indexer: SchemaIndexer, snapshot: SchemaSnapshot, owner_connection: Conn
    ) -> None:
        first = indexer.index(snapshot)
        second = indexer.index(snapshot)

        assert second.elements_written == first.elements_written
        assert second.elements_removed == 0
        assert count_elements(owner_connection, first.model_version) == first.elements_written

    def test_table_rows_do_not_accumulate(
        self, indexer: SchemaIndexer, snapshot: SchemaSnapshot, owner_connection: Conn
    ) -> None:
        """The bug migration 003 exists to fix.

        ``column_name`` is NULL for table elements, and under the default
        NULLS DISTINCT two such rows never conflict -- so ON CONFLICT never
        fired and every re-index appended another copy of every table.
        """
        indexer.index(snapshot)
        indexer.index(snapshot)

        row = owner_connection.execute(
            "SELECT count(*) FROM agent_meta.schema_elements "
            "WHERE dataset = %s AND element_type = 'table' AND table_name = 'orders'",
            (DATASET,),
        ).fetchone()
        assert row is not None
        assert row[0] == 1

    def test_foreign_keys_do_not_accumulate(
        self, indexer: SchemaIndexer, snapshot: SchemaSnapshot, owner_connection: Conn
    ) -> None:
        indexer.index(snapshot)
        indexer.index(snapshot)

        row = owner_connection.execute(
            "SELECT count(*) FROM agent_meta.foreign_keys WHERE dataset = %s",
            (DATASET,),
        ).fetchone()
        assert row is not None
        assert row[0] == len(snapshot.foreign_keys)


class TestStaleElements:
    def test_dropped_elements_are_removed(
        self, indexer: SchemaIndexer, snapshot: SchemaSnapshot, owner_connection: Conn
    ) -> None:
        """A column that no longer exists must not stay retrievable.

        Retrieval would otherwise hand the model a column it cannot use, and
        the generated SQL would fail at execution with an unhelpful error.
        """
        indexer.index(snapshot)

        smaller = SchemaSnapshot(
            tables=(Table(name="orders", columns=(Column("orders", "id", "bigint", False),)),),
            foreign_keys=(),
        )
        report = indexer.index(smaller)

        assert report.elements_removed > 0
        assert count_elements(owner_connection, report.model_version) == 2

    def test_other_model_versions_are_untouched(
        self, owner_connection: Conn, snapshot: SchemaSnapshot, indexer: SchemaIndexer
    ) -> None:
        """Re-embedding under a new model must leave the old vectors queryable
        until the new set is verified -- see DATABASE.md section 10."""
        baseline = indexer.index(snapshot)

        # Relabel the indexed rows as if they came from a different model,
        # then index again. The second run must not see them at all.
        owner_connection.execute(
            "UPDATE agent_meta.schema_elements SET model_version = 'pretend-older' "
            "WHERE dataset = %s",
            (DATASET,),
        )
        reindexed = indexer.index(snapshot)

        assert count_elements(owner_connection, "pretend-older") == baseline.elements_written
        assert count_elements(owner_connection, reindexed.model_version) == (
            reindexed.elements_written
        )


class TestFailFast:
    def test_width_mismatch_is_refused_before_writing(
        self, owner_connection: Conn, snapshot: SchemaSnapshot
    ) -> None:
        narrow = HashingEmbedder(dimensions=8)
        indexer = SchemaIndexer(owner_connection, narrow, dataset=DATASET)

        with pytest.raises(EmbeddingModelMismatchError, match="vector\\(384\\)"):
            indexer.index(snapshot)

        assert count_elements(owner_connection, narrow.model_version) == 0


class TestStartupCheck:
    def test_passes_once_indexed(
        self, indexer: SchemaIndexer, snapshot: SchemaSnapshot, owner_connection: Conn
    ) -> None:
        report = indexer.index(snapshot)
        assert (
            assert_catalog_ready(
                owner_connection, dataset=DATASET, model_version=report.model_version
            )
            == report.elements_written
        )

    def test_refuses_an_unindexed_model(
        self, indexer: SchemaIndexer, snapshot: SchemaSnapshot, owner_connection: Conn
    ) -> None:
        """The failure this prevents is silent, not loud.

        Questions embedded by one model and compared against vectors from
        another land in different vector spaces: every distance is meaningless,
        no query errors, and results still look plausible.
        """
        indexer.index(snapshot)

        with pytest.raises(EmbeddingModelMismatchError, match="no schema elements indexed"):
            assert_catalog_ready(
                owner_connection, dataset=DATASET, model_version="some-other-model"
            )
