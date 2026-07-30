"""Serialization decides retrieval quality, so its format is pinned by tests.

These are the cheapest tests in the project -- pure functions, no connection,
no model -- and they guard the input to everything downstream.
"""

from __future__ import annotations

import pytest

from schema.models import Column, Table
from schema.serialization import MAX_SERIALIZED_CHARS, serialize_column, serialize_table


def make_column(**overrides: object) -> Column:
    defaults: dict[str, object] = {
        "table": "orders",
        "name": "total_amount",
        "data_type": "numeric(12,2)",
        "is_nullable": False,
    }
    return Column(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestSerializeColumn:
    def test_includes_table_name_type(self) -> None:
        assert serialize_column(make_column()) == "orders.total_amount (numeric(12,2))"

    def test_comment_is_appended(self) -> None:
        text = serialize_column(make_column(comment="Order total including tax"))
        assert text.endswith("Order total including tax")
        assert "orders.total_amount" in text

    def test_samples_appear_as_examples(self) -> None:
        text = serialize_column(make_column(sample_values=("10.00", "25.50")))
        assert text.endswith("Examples: 10.00, 25.50")

    def test_multiline_comment_is_flattened(self) -> None:
        # A newline would make the element ambiguous in logs and in the prompt
        # that eventually carries it.
        text = serialize_column(make_column(comment="line one\n  line two\ttabbed"))
        assert "\n" not in text
        assert "line one line two tabbed" in text

    def test_long_element_is_truncated(self) -> None:
        text = serialize_column(make_column(comment="x" * 5_000))
        assert len(text) == MAX_SERIALIZED_CHARS
        assert text.endswith("...")

    def test_truncation_keeps_the_identifier(self) -> None:
        # The point of the ceiling: the column name must survive, because a
        # vector built from prose alone cannot be retrieved by column name.
        text = serialize_column(make_column(comment="x" * 5_000))
        assert text.startswith("orders.total_amount (numeric(12,2))")


class TestSensitiveColumnsAreNeverSampled:
    """The second gate. Introspection should never fetch these values at all,
    but a single missed check there would otherwise write real personal data
    into the catalog permanently."""

    @pytest.mark.parametrize(
        "name",
        ["email", "customer_email", "EmailAddress", "ssn", "password_hash", "home_address"],
    )
    def test_samples_are_dropped(self, name: str) -> None:
        text = serialize_column(make_column(name=name, sample_values=("ada@example.com",)))
        assert "ada@example.com" not in text
        assert "Examples" not in text

    def test_extra_patterns_are_honoured(self) -> None:
        column = make_column(name="internal_code", sample_values=("ACME-1",))
        assert "ACME-1" in serialize_column(column)
        assert "ACME-1" not in serialize_column(column, sensitive_patterns=("internal_",))

    def test_non_sensitive_column_still_samples(self) -> None:
        # A denylist that swallowed everything would be trivially "secure" and
        # useless, so assert the negative case too.
        text = serialize_column(make_column(name="country", sample_values=("GB", "FI")))
        assert "Examples: GB, FI" in text


class TestSerializeTable:
    def test_lists_columns(self) -> None:
        table = Table(
            name="orders",
            comment="One row per order",
            columns=(make_column(name="id", data_type="bigint"), make_column()),
        )
        text = serialize_table(table)
        assert text == "orders (table) — One row per order. Columns: id, total_amount"

    def test_wide_table_summarises_the_tail(self) -> None:
        columns = tuple(make_column(name=f"c{i}", data_type="text") for i in range(60))
        text = serialize_table(Table(name="wide", columns=columns))
        assert "c0" in text
        assert "and 20 more" in text

    def test_table_without_comment_or_columns(self) -> None:
        assert serialize_table(Table(name="empty")) == "empty (table)"
