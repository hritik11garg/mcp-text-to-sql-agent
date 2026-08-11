"""Seeding a real database, because the seeder's job is a first impression.

`python -m demo.seed` is what stands between a clean checkout and a working
demo. It runs inside the compose stack on every ``up``, before the API is
allowed to start, so a failure here is the first thing a stranger sees.

Two properties matter and neither is provable without a database. **The
read-only role must be able to read what was seeded** -- the catalog is
introspected as that role, so a missing GRANT produces a catalog whose every
retrieval hit generates SQL the database then refuses. And **seeding twice must
be the same as seeding once**, because compose runs it on every start.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from core.settings import RetrievalSettings
from demo import dataset, seed

# S608 is off for this file: every interpolated identifier is the module-level
# SCHEMA constant, and psycopg cannot parameterise an identifier. The values in
# these statements are bound.
# ruff: noqa: S608

pytestmark = [pytest.mark.integration]

type Conn = psycopg.Connection[tuple[Any, ...]]

SCHEMA = "demo_seed_test"


@pytest.fixture
def hashing() -> RetrievalSettings:
    """No model download in an integration test; this asserts wiring, not quality."""
    return RetrievalSettings(_env_file=None, embedder_provider="hashing")  # type: ignore[call-arg]


@pytest.fixture
def seeded(
    owner_connection: Conn, readonly_connection: Conn, hashing: RetrievalSettings
) -> Iterator[None]:
    seed.create_schema(owner_connection, SCHEMA)
    seed.load_rows(owner_connection, SCHEMA, dataset.build())
    seed.index(owner_connection, readonly_connection, SCHEMA, hashing)
    try:
        yield
    finally:
        owner_connection.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')


class TestWhatTheSeederProduces:
    def test_every_row_lands(self, owner_connection: Conn, seeded: None) -> None:
        rows = dataset.build()
        for table, expected in (
            ("venue", len(rows.venues)),
            ("artist", len(rows.artists)),
            ("event", len(rows.events)),
        ):
            got = owner_connection.execute(f'SELECT count(*) FROM "{SCHEMA}".{table}').fetchone()
            assert got is not None
            assert got[0] == expected

    def test_the_comments_reach_the_database(self, owner_connection: Conn, seeded: None) -> None:
        """They are indexed into the retrieval corpus, so they are data here."""
        got = owner_connection.execute(
            "SELECT col_description(%s::regclass, ordinal_position) "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'artist' AND column_name = 'genre'",
            (f"{SCHEMA}.artist", SCHEMA),
        ).fetchone()
        assert got is not None
        assert got[0] is not None
        assert "rock" in got[0]

    def test_the_catalog_is_populated_for_the_dataset(
        self, owner_connection: Conn, seeded: None
    ) -> None:
        got = owner_connection.execute(
            "SELECT count(*) FROM agent_meta.schema_elements WHERE dataset = %s", (SCHEMA,)
        ).fetchone()
        assert got is not None
        assert got[0] > 0

    def test_the_join_paths_are_indexed(self, owner_connection: Conn, seeded: None) -> None:
        """Two foreign keys, or the demo question cannot be answered with a join
        the model was told about rather than one it guessed."""
        got = owner_connection.execute(
            "SELECT count(*) FROM agent_meta.foreign_keys WHERE dataset = %s", (SCHEMA,)
        ).fetchone()
        assert got is not None
        assert got[0] == 2


class TestTheReadOnlyRoleCanUseIt:
    def test_it_can_read_every_seeded_table(self, readonly_connection: Conn, seeded: None) -> None:
        """The GRANT, asserted. Without it the catalog describes tables the
        agent cannot select from, and every answer ends in a permission error."""
        for table in ("venue", "artist", "event"):
            got = readonly_connection.execute(f'SELECT count(*) FROM "{SCHEMA}".{table}').fetchone()
            assert got is not None
            assert got[0] > 0

    def test_it_can_run_the_question_the_demo_asks(
        self, readonly_connection: Conn, seeded: None
    ) -> None:
        rows = readonly_connection.execute(
            f'SELECT a.genre, count(*) FROM "{SCHEMA}".event e '
            f'JOIN "{SCHEMA}".artist a ON a.id = e.artist_id '
            f"GROUP BY a.genre HAVING count(*) > 50"
        ).fetchall()
        assert len(rows) >= 3

    def test_it_still_cannot_write_to_the_seeded_schema(
        self, readonly_connection: Conn, seeded: None
    ) -> None:
        """A new schema is a new chance to grant too much.

        The loader grants USAGE and SELECT explicitly because migration 002
        only covered ``public`` -- so every schema created after it is a place
        the containment boundary has to be re-established rather than inherited.
        """
        with pytest.raises(psycopg.Error):
            readonly_connection.execute(
                f'INSERT INTO "{SCHEMA}".venue VALUES (9999, %s, %s, 1)', ("x", "y")
            )


class TestItIsIdempotent:
    def test_seeding_twice_leaves_one_copy(
        self,
        owner_connection: Conn,
        readonly_connection: Conn,
        hashing: RetrievalSettings,
        seeded: None,
    ) -> None:
        """Compose runs this on every ``up``.

        A seeder that appended would make the demo quietly different on the
        third start -- and the documents quote counts.
        """
        seed.create_schema(owner_connection, SCHEMA)
        seed.load_rows(owner_connection, SCHEMA, dataset.build())
        seed.index(owner_connection, readonly_connection, SCHEMA, hashing)

        got = owner_connection.execute(f'SELECT count(*) FROM "{SCHEMA}".event').fetchone()
        assert got is not None
        assert got[0] == len(dataset.build().events)

    def test_the_catalog_does_not_accumulate(
        self,
        owner_connection: Conn,
        readonly_connection: Conn,
        hashing: RetrievalSettings,
        seeded: None,
    ) -> None:
        before = owner_connection.execute(
            "SELECT count(*) FROM agent_meta.schema_elements WHERE dataset = %s", (SCHEMA,)
        ).fetchone()
        seed.index(owner_connection, readonly_connection, SCHEMA, hashing)
        after = owner_connection.execute(
            "SELECT count(*) FROM agent_meta.schema_elements WHERE dataset = %s", (SCHEMA,)
        ).fetchone()

        assert before is not None
        assert after is not None
        assert before[0] == after[0]


class TestTheSearchPathIsRestored:
    def test_creating_the_schema_does_not_leave_the_session_scoped(
        self, owner_connection: Conn
    ) -> None:
        """The defect this test exists for, found by running the seeder.

        ``create_schema`` sets ``search_path`` so the DDL can name tables
        unqualified. Leaving it set took the schema holding the ``vector`` type
        off the path, and the failure surfaced *later* -- inside the indexer, as
        "vector type not found in the database", on a connection that had been
        working a moment earlier.
        """
        before = owner_connection.execute("SHOW search_path").fetchone()
        try:
            seed.create_schema(owner_connection, SCHEMA)
            after = owner_connection.execute("SHOW search_path").fetchone()
        finally:
            owner_connection.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')

        assert before is not None
        assert after is not None
        assert after[0] == before[0]
