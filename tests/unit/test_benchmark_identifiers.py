"""The identifier boundary: what folds, what collides, and what is refused.

These are the tests that decide whether a converted database means what the
benchmark meant. Everything here is decidable without a database, and a failure
in any of them would surface downstream as a plausible, wrong accuracy number.
"""

from __future__ import annotations

import pytest

from benchmark.identifiers import (
    MAX_IDENTIFIER_BYTES,
    IdentifierMap,
    quote_sqlite_identifier,
    to_pg_identifier,
)
from core.exceptions import UnsafeIdentifierError


class TestFolding:
    def test_mixed_case_folds_to_lower(self) -> None:
        assert to_pg_identifier("Stadium", kind="table") == "stadium"

    def test_already_lower_is_unchanged(self) -> None:
        assert to_pg_identifier("singer_in_concert", kind="table") == "singer_in_concert"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert to_pg_identifier("  concert  ", kind="table") == "concert"

    def test_spaces_and_hyphens_survive(self) -> None:
        # Real benchmark schemas contain them, and psycopg quotes them; the
        # point of the allowlist is not to be tidy but to exclude the
        # characters that make a name unusable or dangerous.
        assert to_pg_identifier("Order Details", kind="table") == "order details"
        assert to_pg_identifier("first-name", kind="column") == "first-name"

    def test_a_leading_digit_is_allowed(self) -> None:
        assert to_pg_identifier("2010_census", kind="table") == "2010_census"


class TestRefusal:
    @pytest.mark.parametrize(
        "raw",
        [
            'weird"name',
            "drop;table",
            "schema.table",
            "back\\slash",
            "new\nline",
            "tab\there",
            "unicode_ñame",
        ],
    )
    def test_dangerous_or_unusable_characters_are_refused(self, raw: str) -> None:
        with pytest.raises(UnsafeIdentifierError):
            to_pg_identifier(raw, kind="column")

    @pytest.mark.parametrize("raw", ["", "   ", "\t"])
    def test_empty_names_are_refused(self, raw: str) -> None:
        with pytest.raises(UnsafeIdentifierError):
            to_pg_identifier(raw, kind="table")

    def test_names_over_the_postgres_limit_are_refused(self) -> None:
        # PostgreSQL truncates rather than erroring, so two long names sharing
        # a prefix would silently become one identifier. Refusing is the only
        # behaviour that cannot lose a table.
        too_long = "a" * (MAX_IDENTIFIER_BYTES + 1)
        with pytest.raises(UnsafeIdentifierError, match="truncate"):
            to_pg_identifier(too_long, kind="table")

    def test_a_name_exactly_at_the_limit_is_accepted(self) -> None:
        assert to_pg_identifier("a" * MAX_IDENTIFIER_BYTES, kind="table")

    def test_the_error_names_the_kind_and_the_original(self) -> None:
        with pytest.raises(UnsafeIdentifierError) as caught:
            to_pg_identifier("bad;name", kind="column")
        message = str(caught.value)
        assert "column" in message
        assert "bad;name" in message


class TestIdentifierMap:
    def test_maps_every_name(self) -> None:
        mapping = IdentifierMap.build(["Singer", "Concert"], kind="table")
        assert mapping.safe("Singer") == "singer"
        assert mapping.safe("Concert") == "concert"
        assert len(mapping) == 2

    def test_case_only_collisions_are_refused_with_both_names(self) -> None:
        # The whole reason this class exists: `to_pg_identifier` cannot see a
        # collision, and by the time the second CREATE TABLE runs the first has
        # already succeeded.
        with pytest.raises(UnsafeIdentifierError) as caught:
            IdentifierMap.build(["Song", "song"], kind="table")
        message = str(caught.value)
        assert "'Song'" in message
        assert "'song'" in message

    def test_a_repeated_name_is_not_a_collision(self) -> None:
        assert len(IdentifierMap.build(["song", "song"], kind="table")) == 1

    def test_an_unknown_lookup_raises_rather_than_guessing(self) -> None:
        mapping = IdentifierMap.build(["song"], kind="table")
        with pytest.raises(UnsafeIdentifierError, match="was not in the set"):
            mapping.safe("album")

    def test_iteration_yields_source_to_target_pairs(self) -> None:
        assert list(IdentifierMap.build(["Song"], kind="table")) == [("Song", "song")]


class TestSqliteQuoting:
    def test_quotes_a_plain_name(self) -> None:
        assert quote_sqlite_identifier("song") == '"song"'

    def test_doubles_an_embedded_quote(self) -> None:
        # Unreachable in practice -- to_pg_identifier refuses these names before
        # they get here -- and asserted anyway, because this function should
        # still hold if it ever becomes the only barrier.
        assert quote_sqlite_identifier('a"b') == '"a""b"'
