"""The error contract, tested without a database or a transport.

Every one of the four servers routes its failures through `build_server`, so
this is the single place the contract in docs/architecture/MCP.md section 6 is
either held or broken. Fake handlers keep it that way -- a test that needed
Postgres to check an error shape would be testing two things and reporting one.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types
import pytest

from core.exceptions import (
    ExecutionError,
    PermissionDeniedError,
    ProfilingError,
    SQLValidationError,
    StatementTimeoutError,
    UnknownTableError,
)
from mcp_servers.common import (
    GENERIC_FAILURE,
    ToolSpec,
    build_server,
    error_payload,
    succeeded,
    to_call_result,
)

pytestmark = pytest.mark.unit

STRICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    "required": ["text"],
    "additionalProperties": False,
}


def spec(name: str, handler: Any, schema: dict[str, Any] = STRICT_SCHEMA) -> ToolSpec:
    return ToolSpec(name=name, description="a tool", input_schema=schema, handler=handler)


async def echo(arguments: dict[str, Any]) -> dict[str, Any]:
    return succeeded({"echo": arguments})


def raises(exc: Exception) -> Any:
    async def handler(_: dict[str, Any]) -> dict[str, Any]:
        raise exc

    return handler


async def call(server: Any, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    handler = server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await handler(request)
    return result.root  # type: ignore[no-any-return]


class TestSuccess:
    async def test_a_successful_call_is_not_an_error(self) -> None:
        server = build_server("t", [spec("echo", echo)])

        result = await call(server, "echo", {"text": "hi"})

        assert result.isError is False

    async def test_the_payload_arrives_as_structured_content(self) -> None:
        server = build_server("t", [spec("echo", echo)])

        result = await call(server, "echo", {"text": "hi"})

        assert result.structuredContent == {"ok": True, "echo": {"text": "hi"}}

    async def test_the_payload_also_arrives_as_json_text(self) -> None:
        """Structured content is optional in MCP. Against a host without it the
        model sees only this block, so it has to carry the whole payload."""
        server = build_server("t", [spec("echo", echo)])

        result = await call(server, "echo", {"text": "hi"})
        block = result.content[0]
        assert isinstance(block, types.TextContent)

        assert json.loads(block.text) == {"ok": True, "echo": {"text": "hi"}}

    async def test_tools_are_listed_with_their_schemas(self) -> None:
        server = build_server("t", [spec("echo", echo)])

        handler = server.request_handlers[types.ListToolsRequest]
        listed = await handler(types.ListToolsRequest(method="tools/list"))

        assert listed.root.tools[0].inputSchema == STRICT_SCHEMA


class TestFailuresAreReadableNotFatal:
    """A tool error is a normal outcome. The agent has to receive it as content
    it can branch on -- an exception that kills the call gives it nothing."""

    async def test_a_domain_failure_sets_the_protocol_flag(self) -> None:
        server = build_server("t", [spec("boom", raises(UnknownTableError("orders")))])

        result = await call(server, "boom", {"text": "x"})

        assert result.isError is True

    async def test_the_flag_and_the_payload_cannot_disagree(self) -> None:
        """``isError`` is derived from ``ok`` rather than set alongside it.

        Two fields carrying one fact is a bug waiting to happen; deriving one
        from the other is what stops it. This is the assertion that would fail
        if someone set them independently.
        """
        server = build_server("t", [spec("boom", raises(UnknownTableError("orders")))])

        result = await call(server, "boom", {"text": "x"})
        assert result.structuredContent is not None

        assert result.isError is not result.structuredContent["ok"]

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (StatementTimeoutError("timed out"), "statement_timeout"),
            (PermissionDeniedError("denied"), "permission_denied"),
            (UnknownTableError("orders"), "table_not_found"),
            (SQLValidationError("syntax_error", "bad"), "syntax_error"),
            (SQLValidationError("unknown_identifier", "bad"), "unknown_identifier"),
            (ProfilingError("no such column"), "invalid_arguments"),
            (ExecutionError("something else"), "execution_failed"),
        ],
    )
    async def test_each_failure_maps_to_its_published_error_type(
        self, exc: Exception, expected: str
    ) -> None:
        """``error_type`` is what the agent dispatches on, so the mapping is the
        contract. Timeout and permission-denied are both ``ExecutionError``
        subclasses, and collapsing them to the parent would erase the
        distinction that decides whether retrying is worth anything."""
        server = build_server("t", [spec("boom", raises(exc))])

        result = await call(server, "boom", {"text": "x"})
        assert result.structuredContent is not None

        assert result.structuredContent["error_type"] == expected

    async def test_an_unknown_identifier_carries_the_name_and_suggestion(self) -> None:
        """Enough to fix the query rather than retry it unchanged."""
        server = build_server("t", [spec("boom", raises(UnknownTableError("ordrs", "orders")))])

        result = await call(server, "boom", {"text": "x"})
        assert result.structuredContent is not None

        assert result.structuredContent["identifier"] == "ordrs"
        assert result.structuredContent["suggestion"] == "orders"

    async def test_absent_fields_are_omitted_rather_than_null(self) -> None:
        server = build_server("t", [spec("boom", raises(UnknownTableError("orders")))])

        result = await call(server, "boom", {"text": "x"})
        assert result.structuredContent is not None

        assert "suggestion" not in result.structuredContent


class TestArgumentValidation:
    """Declared constraints are enforced here, in the same shape as every other
    failure -- so the agent's dispatch on ``error_type`` has no hole in it at
    exactly the point a model most often gets things wrong."""

    @pytest.mark.parametrize(
        "arguments",
        [
            {},  # missing required
            {"text": ""},  # violates minLength
            {"text": "x", "count": 0},  # below minimum
            {"text": "x", "count": 99},  # above maximum
            {"text": "x", "extra": 1},  # additionalProperties: false
            {"text": 42},  # wrong type
        ],
    )
    async def test_a_schema_violation_is_rejected(self, arguments: dict[str, Any]) -> None:
        server = build_server("t", [spec("echo", echo)])

        result = await call(server, "echo", arguments)

        assert result.isError is True

    async def test_a_schema_violation_uses_the_same_envelope(self) -> None:
        server = build_server("t", [spec("echo", echo)])

        result = await call(server, "echo", {"text": "x", "count": 99})
        assert result.structuredContent is not None

        assert result.structuredContent["error_type"] == "invalid_arguments"

    async def test_the_offending_field_is_named(self) -> None:
        server = build_server("t", [spec("echo", echo)])

        result = await call(server, "echo", {"text": "x", "count": 99})
        assert result.structuredContent is not None

        assert result.structuredContent["identifier"] == "count"

    async def test_the_handler_never_runs_on_invalid_arguments(self) -> None:
        """Validation is a gate, not a report. A handler that saw `count: 99`
        would have to defend against it itself, which is the duplication the
        schema exists to remove."""
        called = False

        async def handler(_: dict[str, Any]) -> dict[str, Any]:
            nonlocal called
            called = True
            return succeeded({})

        server = build_server("t", [spec("echo", handler)])
        await call(server, "echo", {"text": "x", "count": 99})

        assert called is False


class TestUnexpectedFailuresDoNotNarrateThemselves:
    """The SDK's own catch-all returns ``str(exc)``. For a driver error that can
    carry a connection string, a role name, or a file path -- so this dispatcher
    catches everything first."""

    async def test_an_unexpected_exception_is_still_a_tool_error(self) -> None:
        server = build_server("t", [spec("boom", raises(RuntimeError("kaboom")))])

        result = await call(server, "boom", {"text": "x"})

        assert result.isError is True

    async def test_its_message_is_generic(self) -> None:
        secret = "postgresql://agent:hunter2@10.0.0.7:5432/prod"
        server = build_server("t", [spec("boom", raises(RuntimeError(secret)))])

        result = await call(server, "boom", {"text": "x"})
        assert result.structuredContent is not None

        assert result.structuredContent["message"] == GENERIC_FAILURE

    async def test_nothing_from_the_exception_survives_anywhere_in_the_result(self) -> None:
        """Asserted against the whole serialized result, not one field, so a
        detail escaping through a field this test never considered still
        fails it."""
        secret = "postgresql://agent:hunter2@10.0.0.7:5432/prod"
        server = build_server("t", [spec("boom", raises(RuntimeError(secret)))])

        result = await call(server, "boom", {"text": "x"})
        serialized = result.model_dump_json()

        assert "hunter2" not in serialized
        assert "10.0.0.7" not in serialized

    async def test_it_maps_to_internal_error(self) -> None:
        server = build_server("t", [spec("boom", raises(RuntimeError("kaboom")))])

        result = await call(server, "boom", {"text": "x"})
        assert result.structuredContent is not None

        assert result.structuredContent["error_type"] == "internal_error"


class TestErrorPayloadDirectly:
    def test_a_non_domain_exception_never_contributes_its_message(self) -> None:
        assert error_payload(RuntimeError("secret")) == {
            "error_type": "internal_error",
            "message": GENERIC_FAILURE,
        }

    def test_a_domain_exception_does(self) -> None:
        """These messages were written by this project, for the agent to read."""
        payload = error_payload(StatementTimeoutError("canceling statement"))

        assert payload["message"] == "canceling statement"


class TestCallResultConversion:
    def test_ok_true_is_not_an_error(self) -> None:
        assert to_call_result({"ok": True}).isError is False

    def test_ok_false_is(self) -> None:
        assert to_call_result({"ok": False}).isError is True

    def test_a_payload_with_no_ok_field_fails_closed(self) -> None:
        """A payload that forgot to say it succeeded is treated as a failure.

        The alternative default would report an unknown state as success, which
        is the wrong way round for a flag the agent trusts.
        """
        assert to_call_result({"rows": []}).isError is True

    def test_non_serializable_values_do_not_break_the_text_block(self) -> None:
        """Postgres returns `Decimal`, `date` and `UUID`, none of them JSON
        types. A serialization failure here would turn a successful query into
        an unhandled exception at the boundary."""
        from datetime import date
        from decimal import Decimal

        result = to_call_result({"ok": True, "rows": [[Decimal("1.50"), date(2026, 8, 1)]]})
        block = result.content[0]
        assert isinstance(block, types.TextContent)

        assert "1.50" in block.text
        assert "2026-08-01" in block.text
