"""The published contracts, checked as contracts rather than as code.

A tool schema is not an implementation detail. It is quoted in
docs/architecture/MCP.md, it is the model's only signal at selection time, and
a host may have cached it. So these tests assert the two things that make it a
contract: that its *properties* hold, and that it has not changed without
somebody meaning to change it.

Needs no database and no transport -- a schema that requires Postgres to
inspect is a schema nobody will inspect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from core.settings import ExecutionSettings, ProfilingSettings
from mcp_servers.execute_sql.server import DESCRIPTION as EXECUTE_DESCRIPTION
from mcp_servers.execute_sql.server import input_schema as execute_schema
from mcp_servers.profile_table.server import DESCRIPTION as PROFILE_DESCRIPTION
from mcp_servers.profile_table.server import input_schema as profile_schema
from mcp_servers.schema_search.server import DESCRIPTION as SEARCH_DESCRIPTION
from mcp_servers.schema_search.server import INPUT_SCHEMA as SEARCH_SCHEMA
from mcp_servers.validate_sql.server import DESCRIPTION as VALIDATE_DESCRIPTION
from mcp_servers.validate_sql.server import INPUT_SCHEMA as VALIDATE_SCHEMA

pytestmark = pytest.mark.contract

SNAPSHOT = Path(__file__).parent / "tool_schemas.json"


def published() -> dict[str, dict[str, Any]]:
    """Every tool contract, built from default configuration.

    Two of the four derive bounds from settings, so the snapshot is taken at
    defaults. That is the point rather than a limitation: it means the
    committed file is what a fresh deployment publishes.
    """
    return {
        "search_schema": {"description": SEARCH_DESCRIPTION, "inputSchema": SEARCH_SCHEMA},
        "validate_sql": {"description": VALIDATE_DESCRIPTION, "inputSchema": VALIDATE_SCHEMA},
        "execute_sql": {
            "description": EXECUTE_DESCRIPTION,
            "inputSchema": execute_schema(
                ExecutionSettings().max_rows_ceiling,
                ExecutionSettings().statement_timeout_ceiling_ms,
            ),
        },
        "profile_table": {
            "description": PROFILE_DESCRIPTION,
            "inputSchema": profile_schema(ProfilingSettings()),
        },
    }


ALL_TOOLS = sorted(published())


class TestSchemasAreValidJsonSchema:
    @pytest.mark.parametrize("tool", ALL_TOOLS)
    def test_a_host_can_compile_it(self, tool: str) -> None:
        """A schema the host cannot compile makes the tool uncallable, and the
        failure surfaces at connect time as something opaque."""
        jsonschema.Draft202012Validator.check_schema(published()[tool]["inputSchema"])


class TestContractProperties:
    """MCP.md section 3 states four rules for every tool. These are them."""

    @pytest.mark.parametrize("tool", ALL_TOOLS)
    def test_unknown_arguments_are_refused(self, tool: str) -> None:
        """``additionalProperties: false``.

        Without it a typo'd argument is silently ignored and the tool runs with
        defaults -- which the agent reads as "my argument had no effect" and
        cannot distinguish from "the server disagreed with me".
        """
        assert published()[tool]["inputSchema"]["additionalProperties"] is False

    @pytest.mark.parametrize("tool", ALL_TOOLS)
    def test_it_requires_something(self, tool: str) -> None:
        """Every tool here needs at least one argument to mean anything. A
        schema with no `required` invites a call with an empty object."""
        assert published()[tool]["inputSchema"]["required"]

    @pytest.mark.parametrize("tool", ALL_TOOLS)
    def test_every_bounded_number_has_both_ends(self, tool: str) -> None:
        """A `minimum` without a `maximum` is a limit in one direction only,
        and the unbounded direction is always the expensive one."""
        for name, prop in published()[tool]["inputSchema"]["properties"].items():
            if prop.get("type") == "integer":
                assert "minimum" in prop, f"{tool}.{name}"
                assert "maximum" in prop, f"{tool}.{name}"

    @pytest.mark.parametrize("tool", ALL_TOOLS)
    def test_every_array_is_length_bounded(self, tool: str) -> None:
        for name, prop in published()[tool]["inputSchema"]["properties"].items():
            if prop.get("type") == "array":
                assert "maxItems" in prop, f"{tool}.{name}"

    @pytest.mark.parametrize("tool", ALL_TOOLS)
    def test_every_string_input_rejects_the_empty_string(self, tool: str) -> None:
        """An empty query or an empty SQL string is never a real request, and
        letting one through spends a round trip to say so."""
        for name, prop in published()[tool]["inputSchema"]["properties"].items():
            if prop.get("type") == "string" and "enum" not in prop:
                assert prop.get("minLength", 0) >= 1, f"{tool}.{name}"


class TestDescriptionsAreToolSelectionPrompts:
    """The description is the only thing the model sees when choosing a tool,
    so MCP.md treats it as a prompt and requires it to say *when* to call."""

    @pytest.mark.parametrize("tool", ALL_TOOLS)
    def test_it_says_when_to_call(self, tool: str) -> None:
        """Not just what the tool does -- *when* to reach for it.

        Checked as "mentions calling, next to a word that places it in time",
        because the alternative is asserting an exact phrase, which tests the
        wording rather than the property and breaks on a capital letter.
        """
        description = published()[tool]["description"].lower()

        assert "call" in description
        assert any(
            marker in description
            for marker in ("before", "after", "when", "every time", "only call")
        )

    @pytest.mark.parametrize("tool", ALL_TOOLS)
    def test_it_is_substantial_enough_to_choose_on(self, tool: str) -> None:
        """A one-line description makes two tools look interchangeable when
        the difference between them is the whole design."""
        assert len(published()[tool]["description"]) > 200

    def test_execute_sql_says_what_a_timeout_means(self) -> None:
        """The correct response to a timeout differs from every other failure:
        narrow the query, do not retry it. A model that has not been told
        retries verbatim and times out again."""
        description = published()["execute_sql"]["description"]

        assert "narrow" in description.lower()

    def test_execute_sql_says_validation_comes_first(self) -> None:
        assert "validate_sql" in published()["execute_sql"]["description"]

    def test_profile_table_says_the_numbers_are_approximate(self) -> None:
        """Computed over a bounded scan. An agent that reads a sampled null
        fraction as the table's null fraction writes a confident wrong filter,
        and a caveat that lives only in the docs is one the model never sees."""
        description = published()["profile_table"]["description"]

        assert "bounded scan" in description

    def test_profile_table_explains_withheld(self) -> None:
        """Otherwise a suppressed field reads as an empty column."""
        assert "withheld" in published()["profile_table"]["description"]

    def test_search_schema_says_to_use_the_foreign_keys(self) -> None:
        """Returning two tables without the path between them leaves the model
        to invent a join condition, which it will do."""
        assert "foreign-key" in published()["search_schema"]["description"]


class TestPublishedBoundsMatchEnforcedOnes:
    """A schema constraint the server does not enforce is documentation. These
    check the reverse risk: a schema that advertises a *different* number from
    the one the component clamps to."""

    def test_search_k_ceiling_matches_the_retriever(self) -> None:
        from schema.retrieval import MAX_K

        assert published()["search_schema"]["inputSchema"]["properties"]["k"]["maximum"] == MAX_K

    def test_search_table_filter_ceiling_matches_the_retriever(self) -> None:
        from schema.retrieval import MAX_TABLE_FILTER

        prop = published()["search_schema"]["inputSchema"]["properties"]["table_filter"]
        assert prop["maxItems"] == MAX_TABLE_FILTER

    def test_execute_row_ceiling_follows_configuration(self) -> None:
        """A deployment that lowers the ceiling also lowers what callers are
        told they may ask for."""
        schema = execute_schema(50, 5_000)

        assert schema["properties"]["max_rows"]["maximum"] == 50

    def test_profile_sample_ceiling_follows_configuration(self) -> None:
        schema = profile_schema(ProfilingSettings(profile_sample_rows=3))

        assert schema["properties"]["sample_rows"]["maximum"] == 3

    def test_validate_sql_only_advertises_the_dialect_it_supports(self) -> None:
        """An `enum` of one is the honest way to say "this is not a choice"."""
        prop = published()["validate_sql"]["inputSchema"]["properties"]["dialect"]

        assert prop["enum"] == ["postgres"]


class TestSchemaSnapshot:
    """Breaking-change detection, per MCP.md section 8.

    The snapshot is committed and only ever updated deliberately. It does not
    regenerate itself on mismatch -- a test that writes its own expectation
    passes forever and tells you nothing.
    """

    def test_the_published_contracts_have_not_changed(self) -> None:
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        actual = published()

        assert actual == expected, (
            "A published tool contract changed.\n\n"
            "If this is deliberate: additive changes (a new optional field) may "
            "update the snapshot. Breaking changes -- removing a field, changing "
            "a type, tightening an enum, or altering a description -- need a new "
            "tool name per MCP.md section 8, because a host may have cached the "
            "old one and a description change is a prompt change.\n\n"
            "Update with: python -m tests.contract.snapshot"
        )

    def test_the_snapshot_covers_every_tool(self) -> None:
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

        assert sorted(expected) == ALL_TOOLS
