"""The demo dataset is generated, so its properties have to be asserted.

`DEMO_SCRIPT.md` quotes expected output and the README shows a result. Both are
claims about rows that no human typed -- if the generator wanders, the documents
become wrong in a way nobody notices until an interviewer runs the command.

The other reason to test it: the dataset exists so a stranger's first run works.
A demo whose data contradicts its own column comments teaches that reader to
distrust the comments, which is the opposite of what a schema catalog is for.
"""

from __future__ import annotations

import pytest

from demo import dataset

pytestmark = pytest.mark.unit


class TestItIsDeterministic:
    def test_the_same_seed_produces_the_same_rows(self) -> None:
        assert dataset.build() == dataset.build()

    def test_a_different_seed_produces_different_rows(self) -> None:
        """Otherwise the seed is decoration and the determinism above is luck."""
        assert dataset.build(seed=1).events != dataset.build(seed=2).events

    def test_the_row_counts_are_the_ones_the_documents_quote(self) -> None:
        rows = dataset.build()
        assert (len(rows.venues), len(rows.artists), len(rows.events)) == (12, 20, 400)


class TestTheDataAgreesWithItsOwnComments:
    """Each of these is a sentence in ``COMMENTS``, asserted rather than trusted."""

    def test_tickets_sold_never_exceeds_venue_capacity(self) -> None:
        rows = dataset.build()
        capacity = {venue[0]: venue[3] for venue in rows.venues}
        for _, venue_id, _, _, _, sold in rows.events:
            assert sold <= capacity[venue_id]

    def test_every_genre_is_in_the_documented_vocabulary(self) -> None:
        """The comment names five values. A sixth would make it a lie, and the
        comment is indexed into the retrieval corpus."""
        assert {artist[2] for artist in dataset.build().artists} <= set(dataset.GENRES)

    def test_every_documented_genre_actually_occurs(self) -> None:
        """The reverse: a vocabulary listing a value no row has is equally wrong."""
        assert {artist[2] for artist in dataset.build().artists} == set(dataset.GENRES)


class TestItSupportsTheQuestionsTheDemoAsks:
    def test_the_foreign_keys_resolve(self) -> None:
        """A dangling reference would fail the load, but only after the DDL --
        so the failure would look like a database problem, not a data one."""
        rows = dataset.build()
        venues = {venue[0] for venue in rows.venues}
        artists = {artist[0] for artist in rows.artists}
        for _, venue_id, artist_id, *_ in rows.events:
            assert venue_id in venues
            assert artist_id in artists

    def test_a_group_by_with_having_returns_more_than_one_row(self) -> None:
        """The demo question is "genres with more than 50 events".

        A dataset where that returns one row, or none, makes the demo look
        broken while being correct -- and the threshold is in a document.
        """
        counts: dict[str, int] = {}
        rows = dataset.build()
        genre_of = {artist[0]: artist[2] for artist in rows.artists}
        for _, _, artist_id, *_ in rows.events:
            counts[genre_of[artist_id]] = counts.get(genre_of[artist_id], 0) + 1

        assert sum(1 for total in counts.values() if total > 50) >= 3

    def test_more_than_one_venue_per_city(self) -> None:
        """So that "which city sold the most tickets" needs an aggregate rather
        than a lookup."""
        cities = [venue[2] for venue in dataset.build().venues]
        assert len(cities) > len(set(cities))


class TestTheSchemaIsWhatTheSeederWillCreate:
    def test_every_commented_table_appears_in_the_ddl(self) -> None:
        """A comment naming a table that does not exist fails at load time, in
        the middle of seeding, on a stack a stranger just started."""
        ddl = " ".join(dataset.DDL)
        for table, _, _ in dataset.COMMENTS:
            assert f"CREATE TABLE {table} " in ddl

    def test_every_commented_column_appears_in_its_table(self) -> None:
        for table, column, _ in dataset.COMMENTS:
            if column is None:
                continue
            body = next(part for part in dataset.DDL if f"CREATE TABLE {table} " in part)
            assert column in body

    def test_the_dataset_name_is_not_the_settings_default(self) -> None:
        """``demo`` rather than ``default``.

        The catalog namespace and the executor's search_path must both name it,
        and leaving it as the settings default would make a mismatch invisible
        -- an empty catalog under ``default`` is what a fresh install already
        has.
        """
        assert dataset.SCHEMA == "demo"
