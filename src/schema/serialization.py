"""Turning schema elements into the text that actually gets embedded.

Retrieval quality is decided here. The vector never sees the database -- it
sees this string -- so what is included, and in what order, is the whole of the
schema linker's input. A column serialized as ``"total_amount"`` matches the
question "how much did we make?" far worse than one serialized with its table,
type and comment.

The format is fixed by docs/architecture/DATABASE.md section 3:

    {table}.{column} ({type}) -- {comment}. Examples: {v1}, {v2}, {v3}

Every function here is pure. No connection, no model, no configuration --
which is why the format can be changed and measured cheaply in Stage 5.
"""

from __future__ import annotations

from schema.models import Column, Table
from schema.sensitivity import DEFAULT_SENSITIVE_PATTERNS, is_sensitive

_DASH = " — "
"""Em dash with spaces, exactly as docs/architecture/DATABASE.md specifies.

Kept as a named constant rather than inlined: the separator is part of the
embedded text, so changing it changes every vector and requires a re-index
under a new model_version."""

MAX_SERIALIZED_CHARS = 512
"""Ceiling on one serialized element.

Embedding models truncate long input silently, so an unbounded comment would
push the column name out of the window and leave the vector describing prose
instead of a column. Truncating here makes the limit visible and identical
across models.
"""

MAX_COLUMNS_LISTED = 40
"""Columns named in a table's serialization before the rest are summarised."""


def serialize_column(
    column: Column,
    *,
    sensitive_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS,
) -> str:
    """Serialize one column for embedding.

    Sample values are dropped for columns whose name looks sensitive, even if
    they were somehow collected. Introspection is supposed to never fetch them
    in the first place; this is the second gate, because a single missed check
    on the first one writes real personal data into the catalog permanently.
    """
    parts = [f"{column.table}.{column.name} ({column.data_type})"]

    if column.comment:
        parts.append(f"{_DASH}{_flatten(column.comment)}")

    if column.sample_values and not is_sensitive(column.name, sensitive_patterns):
        examples = ", ".join(_flatten(value) for value in column.sample_values)
        parts.append(f". Examples: {examples}")

    return _truncate("".join(parts))


def serialize_table(table: Table) -> str:
    """Serialize one table for embedding.

    Column names are included because questions name attributes far more often
    than tables ("revenue by region" mentions neither ``orders`` nor
    ``customers``), so a table vector built from its name alone is nearly
    unretrievable.
    """
    parts = [f"{table.name} (table)"]

    if table.comment:
        parts.append(f"{_DASH}{_flatten(table.comment)}")

    if table.columns:
        listed = [column.name for column in table.columns[:MAX_COLUMNS_LISTED]]
        remainder = len(table.columns) - len(listed)
        suffix = f", and {remainder} more" if remainder > 0 else ""
        parts.append(f". Columns: {', '.join(listed)}{suffix}")

    return _truncate("".join(parts))


def _flatten(text: str) -> str:
    """Collapse whitespace so one element is always one line.

    Newlines in a comment would otherwise make the serialized text ambiguous to
    read in logs and in the prompt that eventually carries it.
    """
    return " ".join(text.split())


def _truncate(text: str) -> str:
    if len(text) <= MAX_SERIALIZED_CHARS:
        return text
    return text[: MAX_SERIALIZED_CHARS - 3].rstrip() + "..."
