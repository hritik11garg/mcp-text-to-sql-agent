"""Profiling against a real PostgreSQL, over the read-only role.

Not a mock, for the same reason the rest of the integration suite is not: the
things worth checking here are what ``count(DISTINCT)`` does to a ``json``
column, what ``reltuples`` reports before an ``ANALYZE``, and whether a
statement composed from a catalog name actually runs. None of those have a
useful fake.

The disclosure rules these tests exercise incidentally are asserted directly in
tests/security/test_profile_disclosure.py.
"""

from __future__ import annotations

import psycopg
import pytest

from core.exceptions import ProfilingError, UnknownTableError
from core.settings import ProfilingSettings
from profiling.profiler import TableProfile, TableProfiler
from schema.catalog import SchemaCatalog

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[tuple[object, ...]]

RARE_COUNTRY = "XX"
"""Appears once, so the small-cell rule must withhold it."""


@pytest.fixture(scope="session")
def profile_table(owner_connection: Conn) -> None:
    """A table shaped to exercise every branch of the disclosure budget.

    ``country`` has two common values and one that occurs a single time.
    ``notes`` is entirely unique. ``email`` is named sensitively. ``payload``
    is a type that cannot be grouped or ordered at all.
    """
    owner_connection.execute("DROP TABLE IF EXISTS public.profile_demo")
    owner_connection.execute("""
        CREATE TABLE public.profile_demo (
            id         bigserial PRIMARY KEY,
            country    text,
            amount     numeric(12, 2),
            created_at timestamp without time zone,
            notes      text,
            email      text,
            payload    json
        )
    """)
    owner_connection.execute("GRANT SELECT ON public.profile_demo TO sql_agent_ro")

    rows = []
    for i in range(60):
        rows.append(("FI", 10 + i, f"note-{i}", f"user{i}@example.com"))
    for i in range(30):
        rows.append(("GB", 500 + i, f"gb-note-{i}", f"gb{i}@example.com"))
    rows.append((RARE_COUNTRY, 999_999, "the only one", "rare@example.com"))

    with owner_connection.cursor() as cur:
        cur.executemany(
            "INSERT INTO public.profile_demo "
            "(country, amount, created_at, notes, email, payload) "
            "VALUES (%s, %s, now(), %s, %s, '{\"k\": 1}'::json)",
            rows,
        )
    # A few nulls, so null_fraction is a number worth checking rather than 0.0.
    owner_connection.execute(
        "INSERT INTO public.profile_demo (country, amount) VALUES (NULL, NULL), (NULL, NULL)"
    )


@pytest.fixture
def catalog() -> SchemaCatalog:
    return SchemaCatalog(
        {
            "profile_demo": frozenset(
                {"id", "country", "amount", "created_at", "notes", "email", "payload"}
            )
        }
    )


@pytest.fixture
def profiler(ro_connection: Conn, catalog: SchemaCatalog, profile_table: None) -> TableProfiler:
    return TableProfiler(ro_connection, catalog, ProfilingSettings())


def column(profile: TableProfile, name: str) -> object:
    return next(c for c in profile.columns if c.column == name)


class TestStatistics:
    def test_every_catalogued_column_is_profiled(self, profiler: TableProfiler) -> None:
        profile = profiler.profile("profile_demo")

        assert {c.column for c in profile.columns} == {
            "id",
            "country",
            "amount",
            "created_at",
            "notes",
            "email",
            "payload",
        }

    def test_null_fraction_is_computed(self, profiler: TableProfiler) -> None:
        profile = profiler.profile("profile_demo", columns=["country"])

        # 2 nulls out of 93 rows.
        assert column(profile, "country").null_fraction == pytest.approx(2 / 93, abs=1e-3)  # type: ignore[attr-defined]

    def test_distinct_count_is_computed(self, profiler: TableProfiler) -> None:
        profile = profiler.profile("profile_demo", columns=["country"])

        assert column(profile, "country").distinct_count == 3  # type: ignore[attr-defined]

    def test_a_numeric_column_reports_extremes(self, profiler: TableProfiler) -> None:
        profile = profiler.profile("profile_demo", columns=["amount"])
        amount = column(profile, "amount")

        assert amount.minimum == "10.00"  # type: ignore[attr-defined]
        assert amount.maximum == "999999.00"  # type: ignore[attr-defined]

    def test_a_timestamp_column_reports_extremes(self, profiler: TableProfiler) -> None:
        profile = profiler.profile("profile_demo", columns=["created_at"])

        assert column(profile, "created_at").minimum is not None  # type: ignore[attr-defined]

    def test_a_text_column_reports_no_extremes(self, profiler: TableProfiler) -> None:
        profile = profiler.profile("profile_demo", columns=["notes"])
        notes = column(profile, "notes")

        assert notes.minimum is None  # type: ignore[attr-defined]
        assert notes.maximum is None  # type: ignore[attr-defined]

    def test_the_row_estimate_comes_from_the_planner(self, profiler: TableProfiler) -> None:
        """``reltuples`` is -1 until the table is analysed, and reporting -1 as
        a row count would be worse than reporting nothing."""
        profile = profiler.profile("profile_demo", columns=["id"])

        assert profile.row_estimate is None or profile.row_estimate >= 0

    def test_the_scanned_row_bound_is_reported(self, profiler: TableProfiler) -> None:
        """Every number in the profile is computed over at most this many rows,
        and a reader who does not know that will over-trust all of them."""
        profile = profiler.profile("profile_demo", columns=["id"])

        assert profile.scanned_rows == ProfilingSettings().profile_scan_limit


class TestFrequentValues:
    def test_common_values_are_returned_with_their_counts(self, profiler: TableProfiler) -> None:
        profile = profiler.profile("profile_demo", columns=["country"])
        values = {v.value: v.count for v in column(profile, "country").frequent_values}  # type: ignore[attr-defined]

        assert values == {"FI": 60, "GB": 30}

    def test_values_are_ordered_by_frequency(self, profiler: TableProfiler) -> None:
        profile = profiler.profile("profile_demo", columns=["country"])
        counts = [v.count for v in column(profile, "country").frequent_values]  # type: ignore[attr-defined]

        assert counts == sorted(counts, reverse=True)

    def test_an_all_unique_column_returns_none_and_says_why(self, profiler: TableProfiler) -> None:
        """``notes`` is unique per row, so every value is a record. The reason
        matters: an agent told only that the list is empty asks again."""
        profile = profiler.profile("profile_demo", columns=["notes"])
        notes = column(profile, "notes")

        assert notes.frequent_values == ()  # type: ignore[attr-defined]
        assert any("occurs fewer than 5 times" in w for w in notes.withheld)  # type: ignore[attr-defined]

    def test_statistics_survive_when_no_value_is_frequent_enough(
        self, profiler: TableProfiler
    ) -> None:
        """The join that carries frequent values must not discard the
        statistics of the columns that have none -- which is exactly the case
        where the statistics are all the agent has left."""
        profile = profiler.profile("profile_demo", columns=["notes"])

        assert column(profile, "notes").distinct_count == 91  # type: ignore[attr-defined]


class TestDegradation:
    def test_a_column_the_database_cannot_group_degrades_alone(
        self, profiler: TableProfiler
    ) -> None:
        """``json`` has no equality operator, so ``count(DISTINCT)`` fails.

        One unprofileable column must not fail the profile -- the agent asked
        in order to disambiguate, and a partial answer with a reason beats an
        error it cannot act on.
        """
        profile = profiler.profile("profile_demo", columns=["payload", "country"])

        assert column(profile, "payload").distinct_count is None  # type: ignore[attr-defined]
        assert column(profile, "country").distinct_count == 3  # type: ignore[attr-defined]

    def test_the_degradation_reason_carries_no_driver_text(self, profiler: TableProfiler) -> None:
        """MCP.md section 6 forbids raw driver output crossing a tool boundary,
        and this string is destined for one."""
        profile = profiler.profile("profile_demo", columns=["payload"])
        withheld = " ".join(column(profile, "payload").withheld)  # type: ignore[attr-defined]

        assert "json" not in withheld.casefold() or "operator" not in withheld.casefold()
        assert "HINT" not in withheld

    def test_an_unknown_table_is_refused_against_a_live_connection(
        self, profiler: TableProfiler
    ) -> None:
        with pytest.raises(UnknownTableError):
            profiler.profile("pg_authid")

    def test_an_unknown_column_is_refused(self, profiler: TableProfiler) -> None:
        with pytest.raises(ProfilingError):
            profiler.profile("profile_demo", columns=["nope"])


class TestWidthCap:
    def test_a_wide_table_is_truncated_not_refused(
        self, ro_connection: Conn, catalog: SchemaCatalog, profile_table: None
    ) -> None:
        profiler = TableProfiler(ro_connection, catalog, ProfilingSettings(profile_max_columns=2))
        profile = profiler.profile("profile_demo")

        assert len(profile.columns) == 2

    def test_truncation_is_reported(
        self, ro_connection: Conn, catalog: SchemaCatalog, profile_table: None
    ) -> None:
        """Silently returning two of seven columns would have the agent
        conclude the other five do not exist."""
        profiler = TableProfiler(ro_connection, catalog, ProfilingSettings(profile_max_columns=2))
        profile = profiler.profile("profile_demo")

        assert profile.columns_omitted == 5

    def test_an_explicit_column_list_is_not_truncated_below_the_cap(
        self, ro_connection: Conn, catalog: SchemaCatalog, profile_table: None
    ) -> None:
        """Naming three columns of a wide table is what the cap exists to
        encourage, so it must not then be penalised by it."""
        profiler = TableProfiler(ro_connection, catalog, ProfilingSettings(profile_max_columns=3))
        profile = profiler.profile("profile_demo", columns=["id", "country", "amount"])

        assert len(profile.columns) == 3
        assert profile.columns_omitted == 0


class TestItRunsAsTheReadOnlyRole:
    def test_profiling_a_table_the_role_cannot_read_fails_closed(
        self, ro_connection: Conn, catalog_schema: None
    ) -> None:
        """``internal_payroll`` is catalogued here but the role has no SELECT.

        The profiler holds the read-only connection precisely so this is the
        failure mode: a column that degrades with a reason, not a successful
        read of a table the agent was never granted.
        """
        catalog = SchemaCatalog({"internal_payroll": frozenset({"id", "amount"})})
        profiler = TableProfiler(ro_connection, catalog, ProfilingSettings())

        profile = profiler.profile("internal_payroll", columns=["amount"])

        assert column(profile, "amount").distinct_count is None  # type: ignore[attr-defined]
        assert column(profile, "amount").withheld != ()  # type: ignore[attr-defined]
