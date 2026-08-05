"""MCP server: `validate_sql`.

Contract: docs/architecture/MCP.md section 3.2.

The one tool that is safe to call in a loop, because it is side-effect-free by
construction: five stages of which the first four touch nothing, and the fifth
is `EXPLAIN` without `ANALYZE`. That property is the reason validation is a
separate server from execution -- merging them would force execution's cost
onto every self-correction attempt.

**A failed validation is a success at the protocol level.** ``valid: false`` is
the answer to the question that was asked, not a failure to answer it. The
agent reads ``stage_failed``, ``identifier`` and ``suggestion`` and rewrites the
query; returning ``isError`` for that would conflate "the SQL is wrong" with
"the tool is broken", and those need different responses.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.lowlevel import Server

from composition.resources import Resources
from core.settings import Settings
from mcp_servers.common import ToolSpec, build_server, succeeded
from validation.validator import DIALECT, MAX_SQL_CHARS, SQLValidator, ValidationResult

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Check that a SQL query parses, is a single read-only SELECT, and "
    "references only tables and columns that exist. Runs EXPLAIN without "
    "executing the query, so it is cheap and safe to call repeatedly -- call "
    "it on every generated query before execute_sql, every time. On failure "
    "it names the stage that rejected the query and, for an unknown "
    "identifier, the nearest real name: use that to fix the query rather than "
    "retrying it unchanged. It also returns the planner's estimated cost, so "
    "an implausibly expensive query can be abandoned before it is run."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_SQL_CHARS,
            "description": "The SQL to check. Exactly one statement",
        },
        "dialect": {
            "type": "string",
            "enum": [DIALECT],
            "default": DIALECT,
            "description": "Only postgres is supported",
        },
    },
    "required": ["sql"],
    "additionalProperties": False,
}


def render(result: ValidationResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "valid": result.valid,
        "stage_failed": result.stage_failed.value if result.stage_failed else None,
        "error_type": result.error_type,
        "message": result.message,
        "identifier": result.identifier,
        "suggestion": result.suggestion,
        "estimated_cost": result.estimated_cost,
        "plan_summary": result.plan_summary,
    }
    return {key: value for key, value in payload.items() if value is not None}


def build(resources: Resources) -> Server[Any, Any]:
    settings: Settings = resources.settings

    # The catalog comes from the owner connection; `EXPLAIN` runs on the
    # read-only one. Two roles for one component, and the split is deliberate:
    # agent_meta is unreadable to the read-only role, and the planner must be
    # asked as the role that would actually run the query -- an EXPLAIN that
    # succeeds as the owner and fails as the agent is worse than no check.
    validator = SQLValidator(resources.readonly, resources.catalog, settings.execution)

    async def validate_sql(arguments: dict[str, Any]) -> dict[str, Any]:
        return succeeded(render(validator.validate(arguments["sql"])))

    return build_server(
        "validate-sql",
        [
            ToolSpec(
                name="validate_sql",
                description=DESCRIPTION,
                input_schema=INPUT_SCHEMA,
                handler=validate_sql,
            )
        ],
    )
