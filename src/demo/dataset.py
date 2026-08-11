"""The demo schema and its rows, generated deterministically.

Deterministic because a demo whose contents change per run cannot have a
documented expected output, and DEMO_SCRIPT.md quotes one. The generator is
seeded and uses only the standard library, so the same commit produces the same
database on any machine.

The shape is chosen to make the interesting failure modes reachable. Three
tables and two foreign keys mean a question needs a **join**, which is where a
retriever that returns tables without their edges produces a plausible-looking
wrong query. ``ticket_price`` and ``tickets_sold`` are separate columns so that
revenue is a computed expression rather than a lookup. And ``artist.genre``
holds a small closed vocabulary, which is what makes ``profile_table`` worth
calling -- a model guessing ``'Rock and Roll'`` for a column holding ``'rock'``
is the ordinary text-to-SQL failure this project's profiling step exists for.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

SEED: Final = 20260811
"""Fixed. See the module docstring: a demo with a documented expected output
cannot have a generator that wanders."""

SCHEMA: Final = "demo"

GENRES: Final = ("rock", "jazz", "folk", "electronic", "classical")

CITIES: Final = (
    ("Ashford", 1_800),
    ("Bellhaven", 950),
    ("Cranmoor", 4_200),
    ("Dunwick", 620),
    ("Elmsworth", 2_400),
    ("Fairhollow", 1_150),
)

VENUE_SUFFIXES: Final = ("Hall", "Arena", "Playhouse", "Gardens")

ARTIST_FIRST: Final = (
    "Amber",
    "Bracken",
    "Cinder",
    "Driftwood",
    "Ember",
    "Fenwick",
    "Glass",
    "Harrow",
    "Iron",
    "Juniper",
    "Kestrel",
    "Lantern",
    "Marrow",
    "Nettle",
    "Opal",
    "Pewter",
    "Quarry",
    "Rowan",
    "Sable",
    "Thistle",
)

ARTIST_SECOND: Final = ("Choir", "Collective", "Ensemble", "Quartet", "Union", "Trio")

DDL: Final = (
    """
    CREATE TABLE venue (
        id            integer PRIMARY KEY,
        name          text    NOT NULL,
        city          text    NOT NULL,
        capacity      integer NOT NULL
    )
    """,
    """
    CREATE TABLE artist (
        id            integer PRIMARY KEY,
        name          text    NOT NULL,
        genre         text    NOT NULL,
        formed_year   integer NOT NULL
    )
    """,
    """
    CREATE TABLE event (
        id            integer PRIMARY KEY,
        venue_id      integer NOT NULL REFERENCES venue(id),
        artist_id     integer NOT NULL REFERENCES artist(id),
        event_date    date    NOT NULL,
        ticket_price  numeric(6, 2) NOT NULL,
        tickets_sold  integer NOT NULL
    )
    """,
)

COMMENTS: Final = (
    ("venue", None, "Places a performance can happen."),
    ("venue", "capacity", "Maximum ticketed attendance."),
    ("artist", None, "Performing acts."),
    ("artist", "genre", "One of: rock, jazz, folk, electronic, classical."),
    ("artist", "formed_year", "Year the act started performing together."),
    ("event", None, "One performance by one artist at one venue."),
    ("event", "ticket_price", "Face value per ticket, in the local currency."),
    ("event", "tickets_sold", "Never exceeds the venue capacity."),
)
"""Comments are indexed into the retrieval corpus, so they are part of the
demo rather than decoration -- ``genre``'s vocabulary is exactly the kind of
thing a model cannot guess and a catalog can carry."""


@dataclass(frozen=True, slots=True)
class Rows:
    """Every row the demo loads, generated once so counts can be asserted."""

    venues: tuple[tuple[int, str, str, int], ...]
    artists: tuple[tuple[int, str, str, int], ...]
    events: tuple[tuple[int, int, int, date, float, int], ...]


def build(seed: int = SEED) -> Rows:
    """Generate the dataset. Same seed, same rows, on any machine."""
    # Not a security context: this fills a demo table with venue names and
    # ticket counts. Reproducibility is the requirement, which is the opposite
    # of what a cryptographic generator provides.
    rng = random.Random(seed)  # noqa: S311

    venues: list[tuple[int, str, str, int]] = []
    for index, (city, base) in enumerate(CITIES, start=1):
        for offset, suffix in enumerate(VENUE_SUFFIXES[: 1 + index % 3]):
            venue_id = len(venues) + 1
            capacity = base + offset * 300
            venues.append((venue_id, f"{city} {suffix}", city, capacity))

    artists: list[tuple[int, str, str, int]] = []
    for artist_id, first in enumerate(ARTIST_FIRST, start=1):
        second = ARTIST_SECOND[artist_id % len(ARTIST_SECOND)]
        artists.append(
            (
                artist_id,
                f"{first} {second}",
                GENRES[artist_id % len(GENRES)],
                rng.randint(1985, 2020),
            )
        )

    start = date(2025, 1, 6)
    events: list[tuple[int, int, int, date, float, int]] = []
    for event_id in range(1, 401):
        venue = rng.choice(venues)
        artist = rng.choice(artists)
        # Sold is bounded by capacity, because a demo whose data contradicts its
        # own column comment teaches a reader to distrust the comments.
        sold = rng.randint(int(venue[3] * 0.35), venue[3])
        events.append(
            (
                event_id,
                venue[0],
                artist[0],
                start + timedelta(days=rng.randint(0, 364)),
                round(rng.uniform(12.0, 95.0), 2),
                sold,
            )
        )

    return Rows(tuple(venues), tuple(artists), tuple(events))


__all__ = ["COMMENTS", "DDL", "GENRES", "SCHEMA", "SEED", "Rows", "build"]
