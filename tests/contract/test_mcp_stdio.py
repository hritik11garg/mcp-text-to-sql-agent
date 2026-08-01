"""The four servers as real subprocesses, over real stdio, against real Postgres.

Everything else in the suite tests a component or an envelope. This tests the
thing a host actually does: launch `python -m mcp_servers.<name>`, negotiate,
call `tools/list`, and dispatch on the answer. Nothing here is mocked, because
every interesting failure in an MCP integration lives in the parts a mock
replaces -- process launch, the JSON-RPC framing, and whether stdout stayed
clean.

It is also the only place the *client* half is exercised. A contract tested
only from the side that implements it is tested against itself.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from adapters.embedding.hashing import HashingEmbedder
from agent.discovery import DEFAULT_SERVERS, ToolRegistry
from schema.indexer import SchemaIndexer
from schema.introspection import PostgresIntrospector

pytestmark = [pytest.mark.contract, pytest.mark.integration]

type Conn = psycopg.Connection[tuple[object, ...]]

DATASET = "mcp_contract"
RO_PASSWORD = "test-ro-password"  # matches tests/conftest.py; ephemeral container


@pytest.fixture(scope="session")
def ro_session_connection(postgres_url: str, target_table: None) -> Iterator[Conn]:
    """A session-scoped read-only connection, for session-scoped setup.

    The shared `ro_connection` is function-scoped, which is right for tests
    that assert a denial and want a clean session each time. Indexing happens
    once for the whole module, so it needs its own.

    Introspection runs as the **read-only role** deliberately, even here: it
    filters on `has_table_privilege`, so indexing as the owner would catalogue
    tables the agent cannot select from and produce retrieval hits that always
    end in a permission error.
    """
    conn = psycopg.connect(_readonly_url(postgres_url), autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="session")
def indexed_catalog(
    owner_connection: Conn, ro_session_connection: Conn, catalog_schema: None
) -> None:
    """A populated catalog, because every server refuses to start without one.

    That refusal is deliberate -- a server whose catalog is empty rejects every
    identifier it is ever asked about, and failing at launch is better than
    answering "no such table" to a schema that plainly has one.
    """
    owner_connection.execute(
        "DELETE FROM agent_meta.schema_elements WHERE dataset = %s", (DATASET,)
    )
    owner_connection.execute("DELETE FROM agent_meta.foreign_keys WHERE dataset = %s", (DATASET,))

    snapshot = PostgresIntrospector(ro_session_connection, schema="public").snapshot()
    SchemaIndexer(owner_connection, HashingEmbedder(), dataset=DATASET).index(snapshot)


def _readonly_url(postgres_url: str) -> str:
    parts = urlsplit(postgres_url.replace("postgresql+psycopg://", "postgresql://"))
    netloc = f"sql_agent_login:{RO_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@pytest.fixture(scope="session")
def server_env(postgres_url: str, indexed_catalog: None) -> dict[str, str]:
    """Environment for the server subprocesses.

    Every setting the servers read is set explicitly rather than left to
    inheritance. A developer's real `.env` is read by pydantic-settings from
    the working directory, and while environment variables win over it, a
    variable left unset here would silently pick up their configuration and
    make this suite pass or fail based on a file that is not in the repo.
    """
    libpq = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    return {
        **os.environ,
        "DATABASE_URL": libpq,
        "DATABASE_RO_URL": _readonly_url(postgres_url),
        "DATASET": DATASET,
        "EMBEDDER_PROVIDER": "hashing",  # no model download in CI
        "LLM_PROVIDER": "fake",  # servers never call a model; this keeps startup honest
        "LLM_MODEL": "",
        "SCHEMA_SAMPLE_VALUES": "false",
        "PROFILE_ALLOW_VALUE_SAMPLING": "false",
        "PYTHONPATH": "src",
    }


@asynccontextmanager
async def connected(env: dict[str, str], *modules: str) -> AsyncIterator[ToolRegistry]:
    """Open a registry for the length of one test, in one task.

    Not a fixture, deliberately. A pytest async fixture runs its setup and its
    teardown in *different tasks*, and the stdio transport's cancel scopes must
    be exited by the task that entered them -- so a `yield`-based fixture here
    passes every assertion and then raises at teardown. See `ToolRegistry`.

    Each test names only the servers it needs, which keeps the subprocess count
    down: the discovery tests want all four, everything else wants one.
    """
    async with ToolRegistry(modules or DEFAULT_SERVERS, python=sys.executable, env=env) as reg:
        yield reg


class TestDiscovery:
    async def test_every_server_starts(self, server_env: dict[str, str]) -> None:
        async with connected(server_env) as registry:
            assert registry.unavailable == {}

    async def test_all_four_tools_are_discovered(self, server_env: dict[str, str]) -> None:
        """The agent holds no hardcoded list -- this is where the list comes
        from. If a server stops advertising a tool, the capability disappears
        rather than failing at call time."""
        async with connected(server_env) as registry:
            assert set(registry.tools) == {
                "search_schema",
                "validate_sql",
                "execute_sql",
                "profile_table",
            }

    async def test_each_tool_knows_which_server_answered(self, server_env: dict[str, str]) -> None:
        """A trace has to record which contract ran -- MCP.md section 8."""
        async with connected(server_env) as registry:
            assert registry.tools["execute_sql"].server == "mcp_servers.execute_sql"

    async def test_discovered_schemas_survive_the_wire(self, server_env: dict[str, str]) -> None:
        """The schema the client sees is the schema the server published --
        the property the snapshot test assumes but cannot check by itself."""
        async with connected(server_env) as registry:
            from mcp_servers.schema_search.server import INPUT_SCHEMA

            assert registry.tools["search_schema"].input_schema == INPUT_SCHEMA

    async def test_descriptions_survive_the_wire(self, server_env: dict[str, str]) -> None:
        """They are the model's only selection signal, so a transport that
        dropped them would degrade tool choice silently."""
        async with connected(server_env) as registry:
            assert "foreign-key" in registry.tools["search_schema"].description


class TestSchemaSearch:
    async def test_it_returns_ranked_elements(self, server_env: dict[str, str]) -> None:
        async with connected(server_env, "mcp_servers.schema_search") as registry:
            result = await registry.call("search_schema", {"query": "customer country"})

            assert result.is_error is False
            assert result.payload["elements"]

    async def test_it_returns_join_paths(self, server_env: dict[str, str]) -> None:
        async with connected(server_env, "mcp_servers.schema_search") as registry:
            result = await registry.call(
                "search_schema", {"query": "orders and customers", "k": 20}
            )

            assert "foreign_keys" in result.payload

    async def test_it_never_returns_the_serialized_text(self, server_env: dict[str, str]) -> None:
        """`serialized` carries sampled row values when sampling is on, and
        this payload goes to a model. Same exclusion as the prompt path."""
        async with connected(server_env, "mcp_servers.schema_search") as registry:
            result = await registry.call("search_schema", {"query": "customer"})

            assert all("serialized" not in element for element in result.payload["elements"])


class TestValidateSql:
    async def test_valid_sql_validates(self, server_env: dict[str, str]) -> None:
        async with connected(server_env, "mcp_servers.validate_sql") as registry:
            result = await registry.call("validate_sql", {"sql": "SELECT id FROM customers"})

            assert result.payload["valid"] is True

    async def test_invalid_sql_is_a_successful_call(self, server_env: dict[str, str]) -> None:
        """`valid: false` is the answer to the question asked, not a failure to
        answer it. Flagging it `isError` would conflate "the SQL is wrong" with
        "the tool is broken", and those need different responses."""
        async with connected(server_env, "mcp_servers.validate_sql") as registry:
            result = await registry.call("validate_sql", {"sql": "SELECT nope FROM customers"})

            assert result.is_error is False
            assert result.payload["valid"] is False

    async def test_it_names_the_unknown_identifier(self, server_env: dict[str, str]) -> None:
        async with connected(server_env, "mcp_servers.validate_sql") as registry:
            result = await registry.call("validate_sql", {"sql": "SELECT nope FROM customers"})

            assert result.payload["identifier"] == "nope"

    async def test_a_write_is_rejected_before_the_database_sees_it(
        self, server_env: dict[str, str]
    ) -> None:
        async with connected(server_env, "mcp_servers.validate_sql") as registry:
            result = await registry.call("validate_sql", {"sql": "DELETE FROM customers"})

            assert result.payload["error_type"] == "not_read_only"

    async def test_it_returns_an_estimated_cost(self, server_env: dict[str, str]) -> None:
        """So an implausibly expensive query can be abandoned before it spends
        the execution budget."""
        async with connected(server_env, "mcp_servers.validate_sql") as registry:
            result = await registry.call("validate_sql", {"sql": "SELECT id FROM customers"})

            assert result.payload["estimated_cost"] >= 0


class TestExecuteSql:
    async def test_it_returns_rows(self, server_env: dict[str, str]) -> None:
        async with connected(server_env, "mcp_servers.execute_sql") as registry:
            result = await registry.call("execute_sql", {"sql": "SELECT name FROM customers"})

            assert result.is_error is False
            assert result.payload["columns"] == ["name"]

    async def test_it_reports_the_limit_it_applied(self, server_env: dict[str, str]) -> None:
        async with connected(server_env, "mcp_servers.execute_sql") as registry:
            result = await registry.call(
                "execute_sql", {"sql": "SELECT name FROM customers", "max_rows": 1}
            )

            assert result.payload["row_limit"] == 1

    async def test_it_revalidates_rather_than_trusting_the_caller(
        self, server_env: dict[str, str]
    ) -> None:
        """Nothing called validate_sql here. A separate host can connect to
        this server alone, and a tool that is only safe in the right order is
        not safe."""
        async with connected(server_env, "mcp_servers.execute_sql") as registry:
            result = await registry.call("execute_sql", {"sql": "DROP TABLE customers"})

            assert result.is_error is True
            assert result.payload["error_type"] == "not_read_only"

    async def test_the_table_survives_that_attempt(
        self, server_env: dict[str, str], owner_connection: Conn
    ) -> None:
        """The rejection above is the validator's. This is the role's.

        Checked separately and after the fact because they are independent
        layers: if the validator ever stopped catching `DROP`, the read-only
        role would still refuse it, and this assertion is the one that would
        still hold.
        """
        before = owner_connection.execute("SELECT count(*) FROM customers").fetchone()

        async with connected(server_env, "mcp_servers.execute_sql") as registry:
            await registry.call("execute_sql", {"sql": "DROP TABLE customers"})

        after = owner_connection.execute("SELECT count(*) FROM customers").fetchone()

        assert before == after


class TestProfileTable:
    async def test_it_profiles_a_known_table(self, server_env: dict[str, str]) -> None:
        async with connected(server_env, "mcp_servers.profile_table") as registry:
            result = await registry.call("profile_table", {"table": "customers"})

            assert result.is_error is False
            assert result.payload["table"] == "customers"

    async def test_an_unknown_table_is_a_readable_error(self, server_env: dict[str, str]) -> None:
        async with connected(server_env, "mcp_servers.profile_table") as registry:
            result = await registry.call("profile_table", {"table": "pg_authid"})

            assert result.is_error is True
            assert result.error_type == "table_not_found"

    async def test_the_scan_bound_travels_with_the_numbers(
        self, server_env: dict[str, str]
    ) -> None:
        async with connected(server_env, "mcp_servers.profile_table") as registry:
            result = await registry.call("profile_table", {"table": "customers"})

            assert result.payload["scanned_rows"] > 0

    async def test_a_sensitive_column_is_withheld_over_the_wire(
        self, server_env: dict[str, str]
    ) -> None:
        """The disclosure budget is a property of the profiler, and this
        confirms the server does not undo it on the way out."""
        async with connected(server_env, "mcp_servers.profile_table") as registry:
            result = await registry.call("profile_table", {"table": "customers"})
            email = next(c for c in result.payload["columns"] if c["column"] == "email")

            assert email["withheld"]
            assert email["frequent_values"] == []


class TestErrorsAcrossTheWire:
    async def test_a_schema_violation_is_readable_not_fatal(
        self, server_env: dict[str, str]
    ) -> None:
        async with connected(server_env, "mcp_servers.schema_search") as registry:
            result = await registry.call("search_schema", {"query": "x", "k": 9_999})

            assert result.is_error is True
            assert result.error_type == "invalid_arguments"

    async def test_the_session_survives_a_rejected_call(self, server_env: dict[str, str]) -> None:
        """The most important property here. If a bad argument killed the
        session, one malformed tool call would end the conversation -- and a
        model will produce malformed calls."""
        async with connected(server_env, "mcp_servers.schema_search") as registry:
            await registry.call("search_schema", {"query": "x", "k": 9_999})
            result = await registry.call("search_schema", {"query": "customer"})

            assert result.is_error is False

    async def test_an_unknown_tool_is_not_silently_ignored(
        self, server_env: dict[str, str]
    ) -> None:
        async with connected(server_env, "mcp_servers.schema_search") as registry:
            with pytest.raises(KeyError):
                await registry.call("definitely_not_a_tool", {})


class TestDegradation:
    """MCP.md section 7: a server that is down costs a capability, not a session."""

    async def test_a_missing_server_does_not_prevent_the_others(
        self, server_env: dict[str, str]
    ) -> None:
        modules = ("mcp_servers.schema_search", "mcp_servers.does_not_exist")

        async with ToolRegistry(modules, python=sys.executable, env=server_env) as registry:
            assert "search_schema" in registry.tools
            assert "mcp_servers.does_not_exist" in registry.unavailable

    async def test_the_failure_is_recorded_rather_than_raised(
        self, server_env: dict[str, str]
    ) -> None:
        modules = ("mcp_servers.does_not_exist",)

        async with ToolRegistry(modules, python=sys.executable, env=server_env) as registry:
            assert registry.tools == {}
            assert registry.unavailable
