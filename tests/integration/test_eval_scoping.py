"""The seam against a real PostgreSQL, with two databases that share a name.

The unit tests prove the answerer consults the object it was given. Only a real
database can prove the *right* object was built: that a question about one
converted schema cannot be answered with another's columns, and that a bare
table name in generated SQL resolves where it should.

Two schemas here deliberately hold a table called ``singer`` with different
rows. Every assertion below would pass against a single shared catalog, or a
search_path set once at startup, if the two schemas did not collide -- so the
collision is the fixture's entire job.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from adapters.embedding.hashing import HashingEmbedder
from core.exceptions import RetrievalError
from core.settings import ExecutionSettings, RetrievalSettings
from evals.pipeline import Baseline, SchemaScopedQueryRunner, ScopeRegistry, read_full_schema
from schema.indexer import SchemaIndexer
from schema.introspection import PostgresIntrospector

type Conn = psycopg.Connection[tuple[object, ...]]

ALPHA = "bench_alpha"
BETA = "bench_beta"


@pytest.fixture(scope="module")
def two_databases(owner_connection: Conn) -> None:
    """Two converted schemas whose tables share a name, as Spider's do.

    ``singer`` exists in both with different rows, so a query answered against
    the wrong one returns data rather than an error -- which is the failure
    this whole module exists to rule out.
    """
    role = sql.Identifier("sql_agent_ro")

    for schema, names in ((ALPHA, ("Ada", "Grace")), (BETA, ("Linus", "Ken", "Dennis"))):
        name_of = sql.Identifier(schema)
        singer = sql.Identifier(schema, "singer")

        owner_connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(name_of))
        owner_connection.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} (id bigint PRIMARY KEY, name text NOT NULL)"
            ).format(singer)
        )
        owner_connection.execute(sql.SQL("TRUNCATE {}").format(singer))
        for row_id, name in enumerate(names, 1):
            owner_connection.execute(
                sql.SQL("INSERT INTO {} (id, name) VALUES (%s, %s)").format(singer),
                (row_id, name),
            )
        owner_connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(name_of, role))
        owner_connection.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(name_of, role)
        )

    # Only ALPHA carries a second table, so a full-schema read of one cannot be
    # mistaken for a read of the other by element count alone.
    concert = sql.Identifier(ALPHA, "concert")
    owner_connection.execute(
        sql.SQL("CREATE TABLE IF NOT EXISTS {} (id bigint PRIMARY KEY, year int)").format(concert)
    )
    owner_connection.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(concert, role))


@pytest.fixture
def indexed(owner_connection: Conn, ro_connection: Conn, two_databases: None) -> None:
    """Index both schemas, each under a dataset named for its schema.

    Function-scoped because ``ro_connection`` is, and that costs nothing: the
    indexer is idempotent by design, so re-running it over an unchanged schema
    leaves the catalog byte-for-byte as it was.

    The hashing embedder rather than a sentence-transformer: this asserts
    scoping, not retrieval quality, and a model download in an integration test
    is a minute nobody gets back.
    """
    embedder = HashingEmbedder()
    for schema in (ALPHA, BETA):
        snapshot = PostgresIntrospector(ro_connection, schema=schema).snapshot()
        SchemaIndexer(owner_connection, embedder, dataset=schema).index(snapshot)


@pytest.fixture
def registry(owner_connection: Conn, ro_connection: Conn, indexed: None) -> ScopeRegistry:
    return ScopeRegistry(
        owner_connection,
        ro_connection,
        settings=RetrievalSettings(),
        execution=ExecutionSettings(),
        embedder=HashingEmbedder(),
        needs_retrieval=True,
        needs_validation=True,
        prefix="bench_",
    )


class TestScoping:
    def test_retrieval_returns_only_the_named_databases_columns(
        self, registry: ScopeRegistry
    ) -> None:
        """The property that makes a 20-database benchmark measurable at all."""
        scope = registry.scope("alpha")

        assert scope.retriever is not None
        result = scope.retriever.search("who are the singers?", k=20)

        assert result.elements
        assert {element.table for element in result.elements} <= {"singer", "concert"}
        assert scope.dataset == ALPHA

    def test_two_databases_get_different_catalogs(self, registry: ScopeRegistry) -> None:
        alpha = registry.scope("alpha")
        beta = registry.scope("beta")

        assert "concert" in alpha.catalog.tables
        assert "concert" not in beta.catalog.tables

    def test_a_scope_is_built_once_and_reused(self, registry: ScopeRegistry) -> None:
        assert registry.scope("alpha") is registry.scope("alpha")

    def test_an_unindexed_database_is_refused_with_the_command_to_run(
        self, registry: ScopeRegistry
    ) -> None:
        """Failing here beats failing per question.

        An empty catalog makes the validator skip identifier checks entirely,
        so every question would fail later for a reason that names neither the
        cause nor the fix.
        """
        with pytest.raises(RetrievalError, match=r"benchmark\.load index"):
            registry.scope("never_converted")

    def test_a_question_naming_no_database_is_refused(self, registry: ScopeRegistry) -> None:
        with pytest.raises(ValueError, match="must name the database"):
            registry.scope("")


class TestFullSchema:
    def test_it_reads_every_element_of_one_dataset(
        self, owner_connection: Conn, indexed: None
    ) -> None:
        result = read_full_schema(owner_connection, ALPHA)

        assert {element.table for element in result.elements} == {"singer", "concert"}

    def test_it_does_not_reach_across_datasets(self, owner_connection: Conn, indexed: None) -> None:
        result = read_full_schema(owner_connection, BETA)

        assert {element.table for element in result.elements} == {"singer"}

    def test_the_full_schema_baseline_needs_no_embedder(
        self, owner_connection: Conn, ro_connection: Conn, indexed: None
    ) -> None:
        """Worth asserting because loading one costs a minute and 400 MB.

        A registry that built an embedder regardless would make the
        no-retrieval baseline pay for the machinery it exists to do without.
        """
        registry = ScopeRegistry(
            owner_connection,
            ro_connection,
            settings=RetrievalSettings(),
            execution=ExecutionSettings(),
            embedder=None,
            needs_retrieval=False,
            needs_validation=False,
            prefix="bench_",
        )

        scope = registry.scope("alpha")

        assert scope.retriever is None
        assert {element.table for element in scope.full_schema.elements} == {"singer", "concert"}


class TestQueryRunner:
    def test_a_bare_table_name_resolves_in_the_questions_own_schema(
        self, ro_connection: Conn, two_databases: None
    ) -> None:
        """Same statement, two databases, two answers -- and both are correct.

        This is the assertion that a single search_path, set once, would fail.
        """
        runner = SchemaScopedQueryRunner(
            ro_connection,
            schema_for={"alpha": ALPHA, "beta": BETA},
            statement_timeout_ms=10_000,
        )

        assert runner("SELECT count(*) FROM singer", db_id="alpha") == [[2]]
        assert runner("SELECT count(*) FROM singer", db_id="beta") == [[3]]

    def test_the_search_path_does_not_survive_the_question(
        self, ro_connection: Conn, two_databases: None
    ) -> None:
        """Transaction-scoped, so one question cannot leak into the next.

        After running against beta, a bare name with no schema set must not
        still resolve there.
        """
        runner = SchemaScopedQueryRunner(
            ro_connection, schema_for={"beta": BETA}, statement_timeout_ms=10_000
        )
        runner("SELECT count(*) FROM singer", db_id="beta")

        with pytest.raises(psycopg.errors.UndefinedTable):
            ro_connection.execute("SELECT count(*) FROM singer")

    def test_the_read_only_role_still_cannot_write(
        self, ro_connection: Conn, two_databases: None
    ) -> None:
        """A benchmark run is not a reason to relax the boundary.

        It is the run most likely to execute a statement nobody has read, and
        in two of three baselines nothing has validated what it is about to run.

        **Two refusals, and which one fires first is not this runner's
        business.** Migration 002 sets `default_transaction_read_only` on the
        role, so the transaction refuses the write (`25006`) before privileges
        are consulted at all; strip that and the missing `INSERT` grant refuses
        it (`42501`). Asserting one exact class would make this test fail when
        the *outer* of two controls is what caught it -- which is what happened
        the first time it ran. `SQLExecutor._translate` already maps both to
        `PermissionDeniedError` for exactly this reason.
        """
        runner = SchemaScopedQueryRunner(
            ro_connection, schema_for={"alpha": ALPHA}, statement_timeout_ms=10_000
        )

        with pytest.raises(
            (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)
        ):
            runner("INSERT INTO singer (id, name) VALUES (99, 'Mallory')", db_id="alpha")

    def test_an_oversized_result_is_refused(self, ro_connection: Conn, two_databases: None) -> None:
        runner = SchemaScopedQueryRunner(
            ro_connection,
            schema_for={"alpha": ALPHA},
            statement_timeout_ms=10_000,
            max_rows=1,
        )

        with pytest.raises(ValueError, match="Refused rather than truncated"):
            runner("SELECT name FROM singer", db_id="alpha")


class TestBaselineWiring:
    def test_validation_resolves_identifiers_against_the_right_database(
        self, registry: ScopeRegistry
    ) -> None:
        """``concert`` exists in alpha and not in beta.

        The catalog answers the first question and ``EXPLAIN`` -- which needs
        ``activate`` to have run -- answers the second.
        """
        alpha = registry.scope("alpha")
        beta = registry.scope("beta")
        assert alpha.validator is not None
        assert beta.validator is not None

        registry.activate(alpha)
        assert alpha.validator.validate("SELECT id FROM concert").valid is True

        registry.activate(beta)
        assert beta.validator.validate("SELECT id FROM concert").valid is False

    def test_without_activation_even_correct_sql_is_rejected(
        self, owner_connection: Conn, ro_connection: Conn, indexed: None
    ) -> None:
        """The reason ``activate`` exists, asserted rather than described.

        Skipping it makes the invalid-query rate the validation baseline
        measures an artifact of wiring: every query fails, correct or not.
        """
        ro_connection.execute("SELECT set_config('search_path', 'public', false)")
        registry = ScopeRegistry(
            owner_connection,
            ro_connection,
            settings=RetrievalSettings(),
            execution=ExecutionSettings(),
            embedder=HashingEmbedder(),
            needs_retrieval=True,
            needs_validation=True,
            prefix="bench_",
        )
        scope = registry.scope("alpha")
        assert scope.validator is not None

        assert scope.validator.validate("SELECT id FROM concert").valid is False

        registry.activate(scope)
        assert scope.validator.validate("SELECT id FROM concert").valid is True


def test_every_baseline_is_reachable_from_the_registry_flags() -> None:
    """Each baseline maps to a distinct combination of what gets built.

    A pure check, kept here so it sits beside the wiring it describes.
    """
    wiring: dict[Baseline, tuple[bool, bool]] = {
        Baseline.FULL_SCHEMA: (False, False),
        Baseline.RETRIEVAL_ONLY: (True, False),
        Baseline.WITH_VALIDATION: (True, True),
    }
    assert len(set(wiring.values())) == len(Baseline)
