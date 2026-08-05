"""MCP server: `execute_sql`.

Contract: docs/architecture/MCP.md section 3.3.

The only tool that is not freely retryable, and the only one that spends a real
query against a real database. Everything it enforces, it enforces itself:

**It re-validates.** It does not trust that `validate_sql` was called first. A
different MCP host can connect to this server alone, and a tool that is only
safe when invoked in the right order is not safe.

**It bounds the result at the AST level**, clamped to a ceiling the caller
cannot raise. `max_rows` in the schema is a way to ask for *less*.

**It runs as the read-only role.** That is the boundary that holds when
everything above it has failed.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.lowlevel import Server

from composition.resources import Resources
from execution.executor import AuditLog, QueryResult, SingleConnectionSource, SQLExecutor
from mcp_servers.common import ToolSpec, build_server, succeeded
from validation.validator import MAX_SQL_CHARS, SQLValidator

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Execute a validated read-only SELECT and return the rows. Only call this "
    "after validate_sql returned valid: true. The query runs as a SELECT-only "
    "role under a row limit and a statement timeout, and the limit is applied "
    "server-side whether or not the query has its own -- if truncated is true, "
    "there were more rows than were returned and the answer must say so. "
    "A statement_timeout error means the query was too expensive: narrow it, "
    "add a filter, or aggregate more aggressively. Do not retry it unchanged."
)


def input_schema(max_rows_ceiling: int, timeout_ceiling_ms: int) -> dict[str, Any]:
    """Build the published schema from the configured ceilings.

    The advertised `maximum` is the *server's* ceiling, read from settings
    rather than hardcoded, so a deployment that lowers `MAX_ROWS_CEILING` also
    lowers what callers are told they may ask for. The clamp in
    ``ExecutionSettings`` still applies regardless -- this keeps the contract
    honest, it is not what makes it safe.
    """
    return {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "minLength": 1, "maxLength": MAX_SQL_CHARS},
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": max_rows_ceiling,
                "description": "Fewer rows than the server default, if you need fewer",
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 100,
                "maximum": timeout_ceiling_ms,
                "description": "A shorter timeout than the server default",
            },
        },
        "required": ["sql"],
        "additionalProperties": False,
    }


def render(result: QueryResult) -> dict[str, Any]:
    return {
        "columns": list(result.columns),
        "rows": [list(row) for row in result.rows],
        "row_count": result.row_count,
        "truncated": result.truncated,
        "row_limit": result.row_limit,
        "duration_ms": round(result.duration_ms, 2),
        "executed_sql": result.executed_sql,
    }


def build(resources: Resources) -> Server[Any, Any]:
    settings = resources.settings
    validator = SQLValidator(resources.readonly, resources.catalog, settings.execution)

    # The audit write goes over the *owner* connection, separate from the one
    # running the query, so generated SQL cannot reach the trail that records
    # it and a rolled-back query still leaves a row behind.
    executor = SQLExecutor(
        SingleConnectionSource(resources.readonly),
        validator,
        settings.execution,
        audit=AuditLog(resources.owner, db_role="sql_agent_ro"),
    )

    async def execute_sql(arguments: dict[str, Any]) -> dict[str, Any]:
        result = executor.execute(
            arguments["sql"],
            max_rows=arguments.get("max_rows"),
            timeout_ms=arguments.get("timeout_ms"),
        )
        return succeeded(render(result))

    return build_server(
        "execute-sql",
        [
            ToolSpec(
                name="execute_sql",
                description=DESCRIPTION,
                input_schema=input_schema(
                    settings.execution.max_rows_ceiling,
                    settings.execution.statement_timeout_ceiling_ms,
                ),
                handler=execute_sql,
            )
        ],
    )
