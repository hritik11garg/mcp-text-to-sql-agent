"""Serialization decides retrieval quality, so its format is pinned by tests.

These are the cheapest tests in the project -- pure functions, no connection,
no model -- and they guard the input to everything downstream.
"""

from __future__ import annotations

import pytest

from schema.indexer import _distinct_edges
from schema.models import Column, ForeignKey, Table
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


class TestDistinctEdges:
    """A schema can declare the same join twice, and Spider's `dog_kennels` does.

    `dogs_fk_0` and `dogs_fk_1` are both `dogs.owner_id -> owners.owner_id`.
    The conversion reproduces both because they really are two constraints; the
    catalog stores join *paths*, which is why `foreign_keys_unique` is on the
    edge and not on the constraint name. Found by indexing the real corpus,
    where it aborted the run with a unique-violation on database six of twenty.
    """

    def edge(self, name: str, from_col: str = "owner_id") -> ForeignKey:
        return ForeignKey(
            from_table="dogs",
            from_column=from_col,
            to_table="owners",
            to_column="owner_id",
            constraint_name=name,
        )

    def test_one_edge_declared_twice_is_written_once(self) -> None:
        kept = _distinct_edges([self.edge("dogs_fk_0"), self.edge("dogs_fk_1")])

        assert len(kept) == 1

    def test_the_first_constraint_name_survives(self) -> None:
        # Introspection orders by constraint name, so first-seen is
        # deterministic rather than whatever the planner happened to return.
        kept = _distinct_edges([self.edge("dogs_fk_0"), self.edge("dogs_fk_1")])

        assert kept[0].constraint_name == "dogs_fk_0"

    def test_two_genuinely_different_edges_both_survive(self) -> None:
        kept = _distinct_edges([self.edge("dogs_fk_0"), self.edge("dogs_fk_2", "breed_code")])

        assert len(kept) == 2

    def test_order_is_preserved(self) -> None:
        # The catalog is read back in insertion order for the prompt, so a
        # reshuffle here would change what the model sees between runs.
        kept = _distinct_edges(
            [self.edge("b", "size_code"), self.edge("a"), self.edge("c", "size_code")]
        )

        assert [fk.from_column for fk in kept] == ["size_code", "owner_id"]

    def test_no_edges_is_not_an_error(self) -> None:
        assert _distinct_edges([]) == []
