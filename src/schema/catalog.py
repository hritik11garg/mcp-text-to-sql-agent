"""The catalog as an identifier index, rather than as a retrieval corpus.

Retrieval asks "what is *relevant* to this question?". Validation asks a
different question -- "does this name exist at all?" -- and answering it needs
the whole namespace, not a ranked slice of it.

Loaded once and held, because it is read on every validation attempt and the
self-correction loop may validate three or four candidates per question. It is
a point-in-time snapshot for the same reason the catalog itself is: a migration
to the target database invalidates it until the indexer runs again.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import Connection

_LOAD_SQL = """
    SELECT DISTINCT table_name, column_name
    FROM agent_meta.schema_elements
    WHERE dataset = %s
"""

SUGGESTION_CUTOFF = 0.6
"""How close a name must be before it is offered as "did you mean".

Below this the suggestion is noise, and a wrong suggestion is worse than none:
the agent will act on it, spend a retry, and fail again.
"""


@dataclass(frozen=True, slots=True)
class SchemaCatalog:
    """Every table and column name the agent is allowed to reference.

    Names are matched case-insensitively because PostgreSQL folds unquoted
    identifiers to lower case, so ``SELECT ID FROM Orders`` and
    ``select id from orders`` name the same thing. The original spelling is
    kept for error messages.

    The residual case -- an identifier created *quoted* as ``"MyTable"``, which
    genuinely is case-sensitive -- would be accepted here and then rejected by
    ``EXPLAIN``. Being permissive at this stage is the correct bias: a false
    rejection blocks a valid query, while a false acceptance is caught one
    stage later by the database itself.
    """

    columns_by_table: Mapping[str, frozenset[str]]

    @property
    def tables(self) -> frozenset[str]:
        return frozenset(self.columns_by_table)

    @property
    def is_empty(self) -> bool:
        return not self.columns_by_table

    def has_table(self, name: str) -> bool:
        return name.casefold() in self.columns_by_table

    def has_column(self, table: str, column: str) -> bool:
        return column.casefold() in self.columns_by_table.get(table.casefold(), frozenset())

    def columns_of(self, tables: Iterable[str]) -> frozenset[str]:
        """Every column reachable from a set of tables, for unqualified lookups."""
        found: set[str] = set()
        for table in tables:
            found |= self.columns_by_table.get(table.casefold(), frozenset())
        return frozenset(found)

    def suggest_table(self, name: str) -> str | None:
        return _nearest(name, self.columns_by_table)

    def suggest_column(self, name: str, tables: Iterable[str]) -> str | None:
        return _nearest(name, self.columns_of(tables))


def load_catalog(connection: Connection[Any], *, dataset: str) -> SchemaCatalog:
    """Read the namespace for one dataset.

    Runs on the **owner** connection: ``agent_meta`` is unreadable to the
    read-only role, which is what stops generated SQL from inspecting the
    catalog that judges it.
    """
    with connection.cursor() as cur:
        cur.execute(_LOAD_SQL, (dataset,))
        rows = cur.fetchall()

    columns: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        bucket = columns.setdefault(str(table_name).casefold(), set())
        if column_name is not None:
            bucket.add(str(column_name).casefold())

    return SchemaCatalog({table: frozenset(cols) for table, cols in columns.items()})


def _nearest(name: str, candidates: Iterable[str]) -> str | None:
    matches = difflib.get_close_matches(
        name.casefold(), list(candidates), n=1, cutoff=SUGGESTION_CUTOFF
    )
    return matches[0] if matches else None
