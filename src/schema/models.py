"""Domain models for the schema catalog.

Plain frozen dataclasses with no database or vector concerns. Introspection
produces them, serialization consumes them, and both are testable without
either a connection or a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Column:
    """One column of one table, as the retriever will see it."""

    table: str
    name: str
    data_type: str
    is_nullable: bool
    comment: str | None = None
    sample_values: tuple[str, ...] = ()
    """Representative values, empty unless sampling is explicitly enabled.

    These are real rows. See :mod:`schema.serialization` for why that matters.
    """

    @property
    def qualified_name(self) -> str:
        return f"{self.table}.{self.name}"


@dataclass(frozen=True, slots=True)
class Table:
    """One table (or view) and its columns."""

    name: str
    comment: str | None = None
    columns: tuple[Column, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ForeignKey:
    """One column pair of one foreign-key constraint.

    Composite constraints expand to several instances sharing a
    ``constraint_name``. Retrieval needs join *paths*, and a path is expressed
    column by column.
    """

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    constraint_name: str | None = None


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    """Everything introspection found, at one point in time.

    A snapshot is the unit the indexer writes: partially applying one would
    leave the catalog describing a schema that never existed.
    """

    tables: tuple[Table, ...] = field(default_factory=tuple)
    foreign_keys: tuple[ForeignKey, ...] = field(default_factory=tuple)

    @property
    def column_count(self) -> int:
        return sum(len(table.columns) for table in self.tables)
