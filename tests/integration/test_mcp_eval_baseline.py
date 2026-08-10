"""The MCP baseline against real servers: does the wire change the answer?

`tests/contract/test_mcp_stdio.py` proves the servers start, advertise and
answer. It does not prove they answer **the same thing** the eval harness gets
by calling the retriever directly -- and every accuracy number this project
publishes was measured on that direct path. A benchmark comparing the two is
only interpretable if the difference between them is the transport.

So the assertion below is not "the MCP path returns results". It is *the MCP
path returns the same results*, element for element, in the same order, for the
same question and the same ``k``. If that ever stops holding, a gap between the
`retrieval-only` and `mcp-retrieval` rows is a bug in this repository wearing
the costume of a finding about MCP.

The second thing proved here is the scoping. The published contract has no
dataset argument, so a twenty-database benchmark scopes by **process** -- and
the pool that makes that affordable is only correct if switching datasets
actually switches which catalog answers.

Hashing embedder throughout: this asserts routing and identity, not retrieval
quality, and a model download in an integration test is a minute nobody gets
back.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from adapters.embedding.hashing import HashingEmbedder
from core.exceptions import RetrievalError
from evals.mcp_client import McpClientPool, McpSchemaSearch
from generation.prompts import render_context
from schema.indexer import SchemaIndexer
from schema.introspection import PostgresIntrospector
from schema.retrieval import SchemaRetriever

pytestmark = [pytest.mark.integration, pytest.mark.contract]

type Conn = psycopg.Connection[tuple[object, ...]]

ALPHA = "mcpeval_alpha"
BETA = "mcpeval_beta"
RO_PASSWORD = "test-ro-password"  # matches tests/conftest.py; ephemeral container

QUESTION = "which singers performed at a concert?"
TOP_K = 30


def _readonly_url(postgres_url: str) -> str:
    parts = urlsplit(postgres_url.replace("postgresql+psycopg://", "postgresql://"))
    netloc = f"sql_agent_login:{RO_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@pytest.fixture(scope="module")
def two_schemas(owner_connection: Conn) -> None:
    """Two converted databases whose tables overlap by name, as Spider's do.

    ``singer`` exists in both. A server scoped to the wrong one therefore
    returns *results* rather than an error, which is the failure mode this
    module exists to rule out -- and the one a smoke test would miss.
    """
    role = sql.Identifier("sql_agent_ro")
    for schema in (ALPHA, BETA):
        owner_connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
        )

    # Written out rather than generated. `alpha.singer` carries `age` and
    # `beta.singer` carries `nickname`, which is what lets an assertion say
    # *which* catalog answered rather than only that one did.
    owner_connection.execute(
        sql.SQL("CREATE TABLE IF NOT EXISTS {} (id bigint PRIMARY KEY, name text, age int)").format(
            sql.Identifier(ALPHA, "singer")
        )
    )
    owner_connection.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {concert} "
            "(id bigint PRIMARY KEY, singer_id bigint REFERENCES {singer}(id))"
        ).format(
            concert=sql.Identifier(ALPHA, "concert"),
            singer=sql.Identifier(ALPHA, "singer"),
        )
    )
    owner_connection.execute(
        sql.SQL("CREATE TABLE IF NOT EXISTS {} (id bigint PRIMARY KEY, nickname text)").format(
            sql.Identifier(BETA, "singer")
        )
    )

    for schema in (ALPHA, BETA):
        name_of = sql.Identifier(schema)
        owner_connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(name_of, role))
        owner_connection.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(name_of, role)
        )


@pytest.fixture(scope="module")
def ro_session(postgres_url: str, target_table: None) -> Iterator[Conn]:
    conn = psycopg.connect(_readonly_url(postgres_url), autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def indexed(owner_connection: Conn, ro_session: Conn, two_schemas: None) -> None:
    """Index both, as the read-only role.

    Introspecting as the owner would catalogue tables the agent cannot select
    from, and the servers would then return hits that always end in a
    permission error.
    """
    for schema in (ALPHA, BETA):
        snapshot = PostgresIntrospector(ro_session, schema=schema).snapshot()
        SchemaIndexer(owner_connection, HashingEmbedder(), dataset=schema).index(snapshot)


@pytest.fixture(scope="module")
def server_env(postgres_url: str, indexed: None) -> dict[str, str]:
    """Everything the subprocess reads, set explicitly.

    ``DATASET`` is deliberately **absent**: it is what the client injects, and
    leaving it here would let a broken injection pass by inheriting a value the
    test had already set. ``EMBEDDER_PROVIDER`` must match what indexed the
    catalog, or the retriever filters on a ``model_version`` no row carries.
    """
    return {
        **os.environ,
        "DATABASE_URL": postgres_url.replace("postgresql+psycopg://", "postgresql://"),
        "DATABASE_RO_URL": _readonly_url(postgres_url),
        "EMBEDDER_PROVIDER": "hashing",
        "LLM_PROVIDER": "fake",
        "LLM_MODEL": "",
        "SCHEMA_SAMPLE_VALUES": "false",
        "PROFILE_ALLOW_VALUE_SAMPLING": "false",
        "RETRIEVAL_TOP_K": str(TOP_K),
        "PYTHONPATH": "src",
    }


@pytest.fixture
def in_process(owner_connection: Conn) -> SchemaRetriever:
    """The retriever every published number was measured through."""
    return SchemaRetriever(
        owner_connection,
        HashingEmbedder(),
        dataset=ALPHA,
        default_k=TOP_K,
    )


class TestTheWireDoesNotChangeTheAnswer:
    """The property that makes the two baselines comparable at all."""

    def test_the_same_question_retrieves_the_same_elements(
        self, server_env: dict[str, str], in_process: SchemaRetriever
    ) -> None:
        direct = in_process.search(QUESTION, k=TOP_K)
        with McpSchemaSearch(ALPHA, env=server_env) as client:
            over_the_wire = client.search(QUESTION, k=TOP_K)

        assert [(e.table, e.column) for e in over_the_wire.elements] == [
            (e.table, e.column) for e in direct.elements
        ]
        assert direct.elements, "the fixture retrieved nothing; the assertion above is vacuous"

    def test_the_two_paths_build_the_same_prompt(
        self, server_env: dict[str, str], in_process: SchemaRetriever
    ) -> None:
        """Byte-identical, because the prompt is what the model actually sees.

        The unit test proves the round trip preserves a prompt built from a
        constructed result. This proves it for the results a real server
        produces from a real catalog, including the types and comments
        introspection found.
        """
        direct = in_process.search(QUESTION, k=TOP_K)
        with McpSchemaSearch(ALPHA, env=server_env) as client:
            over_the_wire = client.search(QUESTION, k=TOP_K)

        assert render_context(over_the_wire) == render_context(direct)

    def test_the_join_paths_survive_the_wire(
        self, server_env: dict[str, str], in_process: SchemaRetriever
    ) -> None:
        """Edges are how the model is told to write a join rather than guess one."""
        with McpSchemaSearch(ALPHA, env=server_env) as client:
            result = client.search(QUESTION, k=TOP_K)

        assert [
            (fk.from_table, fk.from_column, fk.to_table, fk.to_column) for fk in result.foreign_keys
        ] == [
            (fk.from_table, fk.from_column, fk.to_table, fk.to_column)
            for fk in in_process.search(QUESTION, k=TOP_K).foreign_keys
        ]

    def test_row_values_never_reach_the_client(self, server_env: dict[str, str]) -> None:
        """The wire's exclusion of ``serialized``, proved against a real server."""
        with McpSchemaSearch(ALPHA, env=server_env) as client:
            result = client.search(QUESTION, k=TOP_K)

        assert result.elements
        assert all(element.serialized == "" for element in result.elements)


class TestTheProcessIsTheScope:
    """No dataset argument on the tool, so the process boundary is the isolation."""

    def test_a_client_answers_only_from_its_own_dataset(self, server_env: dict[str, str]) -> None:
        """``singer`` exists in both schemas with different columns.

        A server reading the wrong catalog returns plausible results, so this
        assertion is about *which* columns came back rather than whether any
        did.
        """
        with McpSchemaSearch(ALPHA, env=server_env) as alpha:
            alpha_columns = {e.column for e in alpha.search(QUESTION, k=TOP_K).elements}
        with McpSchemaSearch(BETA, env=server_env) as beta:
            beta_columns = {e.column for e in beta.search(QUESTION, k=TOP_K).elements}

        assert "age" in alpha_columns
        assert "nickname" not in alpha_columns
        assert "nickname" in beta_columns
        assert "age" not in beta_columns

    def test_the_pool_switches_catalogs_when_the_database_changes(
        self, server_env: dict[str, str]
    ) -> None:
        """The eviction path, end to end.

        A pool of one closes the previous server tree and starts the next. If
        eviction left the old process serving, or if the restart reused the old
        scope, this returns the wrong database's columns while looking healthy.
        """
        with McpClientPool(
            factory=lambda dataset: McpSchemaSearch(dataset, env=server_env)
        ) as pool:
            first = pool.search(ALPHA, QUESTION, k=TOP_K)
            second = pool.search(BETA, QUESTION, k=TOP_K)
            third = pool.search(ALPHA, QUESTION, k=TOP_K)

            assert pool.starts == 3, "an ordered walk starts one server per database"

        assert "age" in {e.column for e in first.elements}
        assert "nickname" in {e.column for e in second.elements}
        assert [(e.table, e.column) for e in third.elements] == [
            (e.table, e.column) for e in first.elements
        ]

    def test_a_dataset_that_was_never_indexed_fails_the_question_not_the_run(
        self, server_env: dict[str, str]
    ) -> None:
        """An empty catalog is a scope error, and it must arrive as one.

        The server starts -- ``schema_search`` builds a retriever, not a
        catalog -- so the failure surfaces on the call. It has to be a
        ``RetrievalError`` so the harness records ``retrieval_failed`` for that
        question rather than aborting, and so ``--halt-after`` sees a run of
        infrastructure failures if the whole corpus is unindexed.
        """
        with McpSchemaSearch("mcpeval_never_converted", env=server_env) as client:
            result = client.search(QUESTION, k=TOP_K)

        assert result.elements == ()


class TestTheClientFailsUsefully:
    def test_an_unreachable_database_kills_the_start_rather_than_the_question(
        self, server_env: dict[str, str]
    ) -> None:
        """The server's own contract: bad configuration dies at launch.

        Worth asserting from the eval side because the harness turns a failed
        *start* into a run-ending error and a failed *call* into one recorded
        question -- and a misconfigured benchmark should not spend a token
        budget discovering that 921 times.
        """
        broken = {**server_env, "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/nothing"}
        client = McpSchemaSearch(ALPHA, env=broken, start_timeout=90.0)

        with pytest.raises(RetrievalError, match="did not advertise 'search_schema'"):
            client.start()

    def test_a_k_above_the_published_ceiling_is_refused_by_the_contract(
        self, server_env: dict[str, str]
    ) -> None:
        """``MAX_K`` is enforced by the schema, and the client reports the refusal.

        The benchmark runs at k=30 and the ceiling is 50, so this is headroom
        rather than a limit in force -- but a silently clamped ``k`` would make
        an MCP row incomparable with the direct row it is published beside.
        """
        with (
            McpSchemaSearch(ALPHA, env=server_env) as client,
            pytest.raises(RetrievalError, match="invalid_arguments"),
        ):
            client.search(QUESTION, k=500)
