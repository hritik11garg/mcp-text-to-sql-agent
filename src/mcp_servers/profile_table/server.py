"""MCP server: `profile_table`.

Contract: docs/architecture/MCP.md section 3.4.

The only tool whose output is row-derived by design. `search_schema` returns
names and comments; `execute_sql` returns rows to the *caller*. A profile is
made in order to be shown to a model, so the whole disclosure budget in
SECURITY.md section 14.2.6 applies to every byte this server emits.

Two consequences visible in this file:

**The description tells the model the numbers are approximate.** They are
computed over a bounded scan in physical order, and an agent that reads a
sampled null fraction as the table's null fraction writes a confident, wrong
`WHERE` clause. A caveat that only exists in the docs is a caveat the model
never sees.

**`withheld` is rendered, not dropped.** Silence and suppression look identical
otherwise, and they call for different responses.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.lowlevel import Server

from composition.resources import Resources
from core.settings import ProfilingSettings
from mcp_servers.common import ToolSpec, build_server, succeeded
from profiling.profiler import TableProfile, TableProfiler

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Get column statistics for a table: null fraction, distinct count, "
    "extremes for numeric and date columns, and the most frequent values. "
    "Call this to choose between two similarly-named columns, or to see how "
    "values are actually stored before writing a WHERE clause against them -- "
    "a column may hold 'FI' rather than 'Finland', and a query filtering on "
    "the wrong form returns zero rows without any error. "
    "Numbers are computed over a bounded scan of the table, not the whole of "
    "it, so treat them as indicative rather than exact. "
    "Any field the server withheld is listed in 'withheld' with the reason: "
    "that means the value was suppressed, not that the column is empty."
)


def input_schema(settings: ProfilingSettings) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "minLength": 1,
                "description": "Table name, as returned by search_schema",
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": settings.profile_max_columns,
                "description": (
                    "Profile only these columns. Naming the two or three you are "
                    "choosing between is cheaper and clearer than profiling the table"
                ),
            },
            "sample_rows": {
                "type": "integer",
                "minimum": 0,
                "maximum": settings.profile_sample_rows,
                "description": (
                    "Raw example values per column. Returns none unless the server "
                    "is configured to allow value sampling"
                ),
            },
        },
        "required": ["table"],
        "additionalProperties": False,
    }


def render(profile: TableProfile) -> dict[str, Any]:
    return {
        "table": profile.table,
        "row_estimate": profile.row_estimate,
        "scanned_rows": profile.scanned_rows,
        "columns_omitted": profile.columns_omitted,
        "columns": [
            {
                "column": column.column,
                "type": column.data_type,
                "null_fraction": column.null_fraction,
                "distinct_count": column.distinct_count,
                "min": column.minimum,
                "max": column.maximum,
                "frequent_values": [
                    {"value": value.value, "count": value.count} for value in column.frequent_values
                ],
                "sample_values": list(column.sample_values),
                "withheld": list(column.withheld),
            }
            for column in profile.columns
        ],
    }


def build(resources: Resources) -> Server[Any, Any]:
    settings = resources.settings
    profiler = TableProfiler(
        resources.readonly,
        resources.catalog,
        settings.profiling,
        schema=settings.database.db_target_schema,
    )

    async def profile_table(arguments: dict[str, Any]) -> dict[str, Any]:
        profile = profiler.profile(
            arguments["table"],
            columns=arguments.get("columns"),
            sample_rows=arguments.get("sample_rows"),
        )
        return succeeded(render(profile))

    return build_server(
        "profile-table",
        [
            ToolSpec(
                name="profile_table",
                description=DESCRIPTION,
                input_schema=input_schema(settings.profiling),
                handler=profile_table,
            )
        ],
    )
