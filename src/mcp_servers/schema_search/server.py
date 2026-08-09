"""MCP server: `search_schema`.

Contract: docs/architecture/MCP.md section 3.1.

A thin adapter over :class:`schema.retrieval.SchemaRetriever`. Thin is the
design: every bound that matters -- `k` clamped to the published ceiling,
`table_filter` bound as a parameter rather than composed -- already lives in
the retriever, because another caller reaching it directly must get the same
treatment. A limit enforced only in the server would be a limit only over the
protocol.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.lowlevel import Server

from composition.resources import Resources
from mcp_servers.common import ToolSpec, build_server, succeeded
from schema.retrieval import MAX_K, MAX_TABLE_FILTER, RetrievalResult

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Find the tables and columns relevant to a natural-language question. "
    "Call this before writing SQL. Returns ranked schema elements with types "
    "and comments, plus the foreign-key edges connecting the tables that "
    "matched -- use those edges to write joins rather than guessing them. "
    "If a generated query referenced an identifier that turned out not to "
    "exist, call this again with a more specific phrasing rather than "
    "inventing a name."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "The question or phrase to find schema elements for",
        },
        "k": {
            "type": "integer",
            "default": 10,
            "minimum": 1,
            "maximum": MAX_K,
            "description": "How many elements to return",
        },
        "table_filter": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_TABLE_FILTER,
            "description": "Restrict results to these tables",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}
"""The published contract. ``MAX_K`` and ``MAX_TABLE_FILTER`` are imported from
the retriever rather than written twice, so the number a caller is *told* and
the number that is *enforced* cannot drift apart -- which is the failure mode
that turns a schema constraint into documentation."""


def render(result: RetrievalResult) -> dict[str, Any]:
    """The wire shape. Structured fields only.

    Deliberately not ``element.serialized``: that string carries sampled row
    values when ``SCHEMA_SAMPLE_VALUES`` is on, and this payload goes to a
    model. Same exclusion, and the same reason, as
    ``generation.prompts.render_context`` -- see SECURITY.md section 14.2.5.
    """
    return {
        "elements": [
            {
                "table": element.table,
                "column": element.column,
                "type": element.data_type,
                "comment": element.comment,
                "score": round(element.score, 4),
            }
            for element in result.elements
        ],
        "foreign_keys": [
            {
                "from_table": fk.from_table,
                "from_column": fk.from_column,
                "to_table": fk.to_table,
                "to_column": fk.to_column,
            }
            for fk in result.foreign_keys
        ],
        "tables": list(result.tables),
    }


def build(resources: Resources) -> Server[Any, Any]:
    # Resolved **here**, not inside the handler. `Resources` connects on first
    # use, so a handler that reached for `resources.retriever` would be the
    # first thing to open the database -- and this module's `__main__` promises
    # the opposite in as many words: a bad DATABASE_URL kills the process while
    # the host is starting it, rather than surfacing as a tool error on the
    # first call the agent will try, and fail, to correct its way out of.
    #
    # It did not. This server started cleanly against an unreachable database,
    # exited 0, and advertised a tool that could not work, while the other
    # three died at startup as documented -- because their `build` happens to
    # touch a connection and this one did not. Found by
    # tests/contract/test_mcp_process_death.py.
    #
    # Reading `dimensions` rather than merely holding the retriever, for the
    # same reason the API lifespan does: it is the property whose side effect
    # is the model load. `model_version` is a name and loads nothing, which is
    # how a ready-looking retriever once shipped over an unopened checkpoint
    # and charged the first caller twenty seconds for it.
    retriever = resources.retriever
    logger.info(
        "retriever ready: model=%s dimensions=%d",
        retriever.model_version,
        retriever.dimensions,
    )

    async def search_schema(arguments: dict[str, Any]) -> dict[str, Any]:
        table_filter = arguments.get("table_filter")
        result = retriever.search(
            arguments["query"],
            k=arguments.get("k"),
            table_filter=table_filter,
        )
        return succeeded(render(result))

    return build_server(
        "schema-search",
        [
            ToolSpec(
                name="search_schema",
                description=DESCRIPTION,
                input_schema=INPUT_SCHEMA,
                handler=search_schema,
            )
        ],
    )
