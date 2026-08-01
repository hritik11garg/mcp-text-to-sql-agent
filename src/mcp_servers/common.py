"""What all four servers share: the error contract, and the stdout guard.

Four processes, one set of rules. Putting them here rather than in each server
means the contract in docs/architecture/MCP.md section 6 is enforced in one
place, and a fifth server inherits it by construction.

Three things this module exists to get right:

**stdout belongs to the protocol.** Over stdio transport, stdout *is* the
JSON-RPC channel. One stray ``print()`` -- in this code, in a dependency, in a
debugging session someone forgot to undo -- writes a line the host cannot parse
and the session dies with a decode error that names nothing useful. So
:func:`claim_stdout` hands the real stream to the transport and repoints
``sys.stdout`` at stderr, which turns that failure into a harmless log line.

**A tool failure is a normal outcome, not an exception.** The agent has to
*read* a failure to correct itself, so everything a tool can fail with comes
back as ``isError: true`` with structured content. An exception that kills the
call gives the agent nothing to work with.

**An unexpected exception must not narrate itself to the model.** The SDK's
own catch-all returns ``str(exc)``, which for a driver error can carry a
connection string, a role name, or a file path. So the dispatcher here catches
everything first: domain errors (whose messages this project wrote, and which
the agent needs) pass through, and anything else becomes a generic message with
the detail logged to stderr where the operator can see it and the model cannot.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from io import TextIOWrapper
from typing import Any

import anyio
import jsonschema
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from core.exceptions import (
    EmbeddingModelMismatchError,
    ExecutionError,
    PermissionDeniedError,
    ProfilingError,
    RetrievalError,
    SQLValidationError,
    StatementTimeoutError,
    TextToSQLError,
    UnknownTableError,
)

logger = logging.getLogger(__name__)

SERVER_VERSION = "0.3.0"
"""Reported in ``initialize`` so a trace records which contract ran.

Tracks the CHANGELOG release that introduces the MCP layer rather than the
package version, because what a client cares about is the shape of the tools.
"""

GENERIC_FAILURE = "the server could not complete this call"
"""What an unexpected exception says to the model.

Deliberately uninformative. The operator gets the real thing on stderr; the
model gets something it cannot mine for infrastructure detail.
"""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool: its published contract and the function behind it.

    The schema is written out as a literal dict rather than derived from type
    hints. It is a *published contract* -- docs/architecture/MCP.md quotes it,
    the snapshot test diffs it, and a host may have cached it -- so it should
    change only when somebody means to change it. Generating it from a
    signature makes an incidental refactor into a contract break.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

    def to_tool(self) -> types.Tool:
        return types.Tool(
            name=self.name, description=self.description, inputSchema=self.input_schema
        )


def error_payload(exc: Exception) -> dict[str, Any]:
    """A domain failure as content the agent can branch on.

    Shape is fixed by docs/architecture/MCP.md section 6: ``error_type`` is what
    the agent dispatches on, ``message`` is what it reads, and the optional
    ``identifier`` / ``suggestion`` are what let it fix the query rather than
    retry it unchanged.
    """
    payload: dict[str, Any] = {
        "error_type": _error_type(exc),
        "message": str(exc) if isinstance(exc, TextToSQLError) else GENERIC_FAILURE,
    }

    if isinstance(exc, SQLValidationError):
        payload["error_type"] = exc.error_type
        if exc.identifier:
            payload["identifier"] = exc.identifier
    elif isinstance(exc, UnknownTableError):
        payload["identifier"] = exc.table
        if exc.suggestion:
            payload["suggestion"] = exc.suggestion

    return payload


def _error_type(exc: Exception) -> str:
    """Map an exception to one of the published ``error_type`` values.

    Ordered most specific first, because the hierarchy is nested --
    ``StatementTimeoutError`` and ``PermissionDeniedError`` are both
    ``ExecutionError``, and matching the parent first would erase the
    distinction the agent needs. A ``dict`` keyed by type would silently do the
    wrong thing here for the same reason.
    """
    if isinstance(exc, StatementTimeoutError):
        return "statement_timeout"
    if isinstance(exc, PermissionDeniedError):
        return "permission_denied"
    if isinstance(exc, UnknownTableError):
        return "table_not_found"
    if isinstance(exc, SQLValidationError):
        return exc.error_type
    if isinstance(exc, EmbeddingModelMismatchError):
        return "connection_failed"
    if isinstance(exc, ProfilingError | RetrievalError):
        return "invalid_arguments"
    if isinstance(exc, ExecutionError):
        return "execution_failed"
    return "internal_error"


def build_server(name: str, specs: Sequence[ToolSpec]) -> Server[Any, Any]:
    """Wire tool definitions into a server that cannot leak or crash.

    Input validation runs here rather than via the SDK's ``validate_input``.
    Same library, same schema -- what changes is the *shape of the failure*.
    The SDK returns a bare text message, which would mean a schema violation
    reaches the agent in a different format from every other failure, and the
    agent's dispatch on ``error_type`` would have a hole in it exactly where a
    model most often gets things wrong.
    """
    server: Server[Any, Any] = Server(name, version=SERVER_VERSION)
    by_name = {spec.name: spec for spec in specs}

    # The SDK's registration decorators are untyped, so strict mode cannot see
    # through them. Ignored here at the two call sites rather than by relaxing
    # the setting for this module, which would also stop catching untyped
    # decorators this project wrote.
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return [spec.to_tool() for spec in specs]

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        # Returning a fully built result rather than a dict is what puts
        # `isError` under this function's control. Handing back a dict makes
        # the SDK construct the result itself and set `isError: false`
        # unconditionally -- so every tool failure would arrive at the agent
        # flagged as a success, and the flag the protocol defines for exactly
        # this purpose would be permanently wrong.
        return to_call_result(await _dispatch(by_name, tool_name, arguments))

    return server


async def _dispatch(
    by_name: dict[str, ToolSpec], tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Validate, run, and convert every failure into readable content."""
    spec = by_name.get(tool_name)
    if spec is None:
        # A genuinely unknown tool is a protocol-level bug, not something the
        # agent can correct, so this is the one path that raises.
        raise ValueError(f"unknown tool: {tool_name!r}")

    try:
        jsonschema.validate(instance=arguments, schema=spec.input_schema)
    except jsonschema.ValidationError as exc:
        logger.info("rejected %s arguments: %s", tool_name, exc.message)
        return _failed(
            {
                "error_type": "invalid_arguments",
                "message": exc.message,
                "identifier": ".".join(str(p) for p in exc.absolute_path) or None,
            }
        )

    try:
        return await spec.handler(arguments)
    except TextToSQLError as exc:
        # Expected. The message was written by this project for the agent to
        # read, so it passes through.
        logger.info("%s returned a tool error: %s", tool_name, type(exc).__name__)
        return _failed(error_payload(exc))
    except Exception:
        # Unexpected. `logger.exception` puts the traceback on stderr for the
        # operator; the model gets GENERIC_FAILURE. Catching here is what stops
        # the SDK's own handler from returning str(exc), which for a driver
        # error can carry a connection string or a role name.
        logger.exception("%s failed unexpectedly", tool_name)
        return _failed({"error_type": "internal_error", "message": GENERIC_FAILURE})


def _failed(payload: dict[str, Any]) -> dict[str, Any]:
    """Mark a payload as a tool error, dropping keys with nothing to say."""
    return {"ok": False, **{k: v for k, v in payload.items() if v is not None}}


def succeeded(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def to_call_result(payload: dict[str, Any]) -> types.CallToolResult:
    """Lift a handler payload into a result with ``isError`` set from ``ok``.

    The two carry the same fact deliberately, and deriving one from the other
    here is what stops them disagreeing. ``isError`` is the protocol's flag and
    is what a client branches on; ``ok`` lives inside the payload so that the
    **text** block is self-describing. That matters because structured content
    is optional in MCP -- against a host that does not support it, the model
    sees only the JSON text, and a failure that announces itself in the first
    line of that text is far harder to misread as a result.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
        structuredContent=payload,
        isError=not payload.get("ok", False),
    )


def configure_logging(level: int = logging.INFO) -> None:
    """Send every log record to stderr, and nothing to stdout.

    ``logging.basicConfig`` defaults to stderr already, but only when no
    handler exists yet -- a library that configured logging first would leave
    the root logger pointed wherever it chose. Forcing the handler makes this
    independent of import order, which is not a detail worth debugging through
    a corrupted JSON-RPC stream.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


def claim_stdout() -> anyio.AsyncFile[str]:
    """Take the real stdout for the protocol and repoint ``sys.stdout`` at stderr.

    Returns the stream to hand to ``stdio_server``. After this call, anything
    that writes to ``sys.stdout`` -- a stray ``print``, a dependency's progress
    bar, a debugger -- lands on stderr and is merely noise instead of a parse
    error that kills the session.

    The real handle is captured from ``sys.stdout.buffer`` *before* the
    rebinding, so the transport keeps the descriptor it needs.
    """
    protocol_stream = anyio.wrap_file(TextIOWrapper(sys.stdout.buffer, encoding="utf-8"))
    sys.stdout = sys.stderr
    return protocol_stream


async def serve_stdio(server: Server[Any, Any]) -> None:
    """Run a server over stdio, with stdout claimed for the protocol."""
    protocol_stream = claim_stdout()
    async with stdio_server(stdout=protocol_stream) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, initialization_options(server))


def initialization_options(server: Server[Any, Any]) -> InitializationOptions:
    """Declare capabilities honestly: no notifications, because none are sent.

    A tool set here is fixed for the life of the process, so advertising
    ``tools_changed`` would promise a notification that never arrives and leave
    a client waiting for one.
    """
    return InitializationOptions(
        server_name=server.name,
        server_version=SERVER_VERSION,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )


def main(server: Server[Any, Any]) -> None:
    """Entrypoint shared by all four ``__main__`` modules."""
    configure_logging()
    anyio.run(serve_stdio, server)


__all__ = [
    "GENERIC_FAILURE",
    "SERVER_VERSION",
    "ToolSpec",
    "build_server",
    "claim_stdout",
    "configure_logging",
    "error_payload",
    "main",
    "serve_stdio",
    "succeeded",
    "to_call_result",
]
