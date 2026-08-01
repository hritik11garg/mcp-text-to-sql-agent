"""Profiling decisions that are made before any database is involved.

Everything here is about *what the profiler will agree to ask for*: which
identifiers it accepts, which types get extremes, and how a caller's requested
sample size is clamped. All of it runs without a connection, which is the point
-- these are the checks that must happen before a statement exists.

The database-facing behaviour is in tests/integration/test_profiling.py, and
the disclosure budget is in tests/security/test_profile_disclosure.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.exceptions import ProfilingError, UnknownTableError
from core.settings import ProfilingSettings
from profiling.profiler import TableProfiler, _supports_extremes
from schema.catalog import SchemaCatalog


@pytest.fixture
def catalog() -> SchemaCatalog:
    return SchemaCatalog(
        {
            "customers": frozenset({"id", "name", "email", "country"}),
            "orders": frozenset({"id", "customer_id", "total_amount"}),
        }
    )


class ExplodingConnection:
    """Any database access at all is a failure in this file.

    The profiler must reject an unknown identifier before it composes a
    statement, so a test that reaches the connection has caught the ordering
    bug this class exists to detect.
    """

    def cursor(self) -> Any:
        raise AssertionError("the profiler touched the database before resolving identifiers")

    def transaction(self) -> Any:
        raise AssertionError("the profiler opened a transaction before resolving identifiers")


@pytest.fixture
def profiler(catalog: SchemaCatalog, profiling_settings: ProfilingSettings) -> TableProfiler:
    return TableProfiler(ExplodingConnection(), catalog, profiling_settings)  # type: ignore[arg-type]


class TestIdentifiersAreResolvedBeforeAnySQL:
    def test_an_unknown_table_is_refused(self, profiler: TableProfiler) -> None:
        with pytest.raises(UnknownTableError):
            profiler.profile("nonexistent")

    def test_an_unknown_table_carries_a_suggestion(self, profiler: TableProfiler) -> None:
        """Same reasoning as the validator: an agent told only "no" retries
        with the same name and burns another attempt."""
        with pytest.raises(UnknownTableError) as caught:
            profiler.profile("custmers")

        assert caught.value.suggestion == "customers"

    def test_an_unknown_column_is_refused(self, profiler: TableProfiler) -> None:
        with pytest.raises(ProfilingError, match="emial"):
            profiler.profile("customers", columns=["emial"])

    def test_an_unknown_column_carries_a_suggestion(self, profiler: TableProfiler) -> None:
        with pytest.raises(ProfilingError, match="did you mean 'email'"):
            profiler.profile("customers", columns=["emial"])

    @pytest.mark.parametrize(
        "injected",
        [
            "customers; DROP TABLE agent_meta.schema_elements; --",
            'customers" ; SELECT 1; --',
            "pg_authid",
            "../../etc/passwd",
        ],
    )
    def test_an_injected_table_name_never_reaches_a_statement(
        self, profiler: TableProfiler, injected: str
    ) -> None:
        """The catalog is an allowlist, not just a spell-checker.

        ``sql.Identifier`` would quote every one of these safely, but quoting
        answers "is this escaped?" and the allowlist answers "may this be named
        at all?". Only the second bounds which relations a model-chosen string
        can reach -- ``pg_authid`` is the case that makes the difference plain.
        """
        with pytest.raises(UnknownTableError):
            profiler.profile(injected)

    def test_a_known_table_is_matched_case_insensitively(
        self, catalog: SchemaCatalog, profiling_settings: ProfilingSettings
    ) -> None:
        """PostgreSQL folds unquoted identifiers, so ``Customers`` and
        ``customers`` name the same relation and both must resolve."""
        profiler = TableProfiler(ExplodingConnection(), catalog, profiling_settings)  # type: ignore[arg-type]

        with pytest.raises(AssertionError, match="touched the database"):
            profiler.profile("CUSTOMERS")


class TestExtremesAreOnlyForTypesWhereTheyAreBounds:
    @pytest.mark.parametrize(
        "data_type",
        [
            "integer",
            "bigint",
            "smallint",
            "numeric(12,2)",
            "double precision",
            "real",
            "money",
            "date",
            "timestamp without time zone",
            "timestamp with time zone",
            "time without time zone",
            "interval",
        ],
    )
    def test_ordered_types_report_extremes(self, data_type: str) -> None:
        """``max(order_date)`` is a fact about the table, not about a person."""
        assert _supports_extremes(data_type) is True

    @pytest.mark.parametrize(
        "data_type",
        [
            "text",
            "character varying(50)",
            "char(2)",
            "uuid",
            "bytea",
            "json",
            "jsonb",
            "inet",
            "xml",
            "tsvector",
            "point",
            "mood_enum",
        ],
    )
    def test_unordered_and_textual_types_do_not(self, data_type: str) -> None:
        """The lexicographic extreme of a text column is a verbatim cell.

        ``min(name)`` is somebody's name. Returning it under the heading
        "statistics" would be a category error, and it is the one that would
        let real values out while every explicit sampling gate stayed off.
        """
        assert _supports_extremes(data_type) is False

    @pytest.mark.parametrize("data_type", ["integer[]", "numeric(10,2)[]", "date[]"])
    def test_arrays_of_ordered_types_do_not(self, data_type: str) -> None:
        """An array's extreme is a whole array of values, not a bound."""
        assert _supports_extremes(data_type) is False

    def test_an_unrecognised_type_fails_closed(self) -> None:
        """A type this allowlist has never heard of gets no extremes.

        Postgres is extensible; the next deployment may have PostGIS installed.
        Defaulting to "report it" would leak values from every type added after
        this list was written.
        """
        assert _supports_extremes("geography(Point,4326)") is False

    def test_matching_ignores_case_and_padding(self) -> None:
        assert _supports_extremes("  INTEGER ") is True


class TestSampleRowsAreClamped:
    def test_sampling_off_returns_zero_however_much_is_asked_for(self) -> None:
        """The flag is the gate. A caller cannot open it by asking louder."""
        settings = ProfilingSettings(profile_allow_value_sampling=False)

        assert settings.clamp_sample_rows(20) == 0

    def test_sampling_off_ignores_the_configured_count(self) -> None:
        settings = ProfilingSettings(profile_allow_value_sampling=False, profile_sample_rows=10)

        assert settings.clamp_sample_rows(None) == 0

    def test_sampling_on_honours_a_smaller_request(self) -> None:
        settings = ProfilingSettings(profile_allow_value_sampling=True, profile_sample_rows=5)

        assert settings.clamp_sample_rows(2) == 2

    def test_sampling_on_caps_a_larger_request(self) -> None:
        """The caller is a language model reading user-supplied text, so this
        is an input to bound rather than a request to honour."""
        settings = ProfilingSettings(profile_allow_value_sampling=True, profile_sample_rows=5)

        assert settings.clamp_sample_rows(10_000) == 5

    def test_a_negative_request_floors_at_zero(self) -> None:
        settings = ProfilingSettings(profile_allow_value_sampling=True)

        assert settings.clamp_sample_rows(-1) == 0


class TestBoundsCannotBeConfiguredAway:
    def test_the_small_cell_threshold_cannot_drop_below_two(self) -> None:
        """A threshold of 1 would make a value unique to one record reportable,
        which is the exact disclosure the rule exists to prevent. The floor is
        in the type so no deployment can configure past it.
        """
        with pytest.raises(ValueError, match="greater than or equal to 2"):
            ProfilingSettings(profile_min_value_frequency=1)

    def test_the_column_cap_has_a_ceiling(self) -> None:
        with pytest.raises(ValueError, match="less than or equal to 200"):
            ProfilingSettings(profile_max_columns=10_000)

    def test_the_scan_limit_has_a_ceiling(self) -> None:
        with pytest.raises(ValueError, match="less than or equal to 1000000"):
            ProfilingSettings(profile_scan_limit=10_000_000)

    def test_the_sample_count_has_a_ceiling(self) -> None:
        """Matches the ``maximum: 20`` in the published profile_table schema."""
        with pytest.raises(ValueError, match="less than or equal to 20"):
            ProfilingSettings(profile_sample_rows=100)

    def test_value_truncation_has_a_ceiling(self) -> None:
        with pytest.raises(ValueError, match="less than or equal to 200"):
            ProfilingSettings(profile_max_value_chars=100_000)


class TestDefaultsAreTheSafeOnes:
    def test_raw_sampling_is_off(self) -> None:
        assert ProfilingSettings().profile_allow_value_sampling is False

    def test_the_small_cell_threshold_is_the_conventional_five(self) -> None:
        assert ProfilingSettings().profile_min_value_frequency == 5

    def test_a_default_profile_clamps_samples_to_nothing(self) -> None:
        assert ProfilingSettings().clamp_sample_rows(None) == 0
