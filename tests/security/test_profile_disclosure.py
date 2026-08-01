"""The one component whose output is row data by design.

Everywhere else, real values either stay in the database or stay in a store the
operator controls. A profile exists in order to be shown to a language model,
so it cannot be made safe by refusing to emit values -- it has to be made safe
by being precise about *which* values may be emitted.

Three independent gates, asserted here one at a time and then together:

1. A sensitive-looking column is never read at all.
2. A value seen fewer than ``PROFILE_MIN_VALUE_FREQUENCY`` times identifies a
   record rather than a category, and is withheld even when everything else is
   permitted.
3. Raw rows require ``PROFILE_ALLOW_VALUE_SAMPLING``, which is off.

Related: tests/security/test_no_row_data_in_prompt.py covers the *other*
direction -- that the schema catalog's persisted samples never reach a prompt.
The two files together are the whole answer to "what row data can leave?".
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from core.settings import ProfilingSettings
from profiling.profiler import TableProfile, TableProfiler
from schema.catalog import SchemaCatalog
from schema.sensitivity import DEFAULT_SENSITIVE_PATTERNS

pytestmark = pytest.mark.security

type Conn = psycopg.Connection[tuple[object, ...]]

SECRET_NOTE = "SSN 123-45-6789 card 4111111111111111"
"""Planted in a column with an innocuous name. Unmistakable if it escapes."""


@pytest.fixture(scope="session")
def disclosure_table(owner_connection: Conn) -> None:
    """One common value, one unique value, and a planted secret.

    ``email`` is named sensitively. ``notes`` is not, and holds the secret --
    the residual case the name-based denylist cannot catch, which is what the
    small-cell rule and the sampling flag exist to bound.
    """
    owner_connection.execute("DROP TABLE IF EXISTS public.disclosure_demo")
    owner_connection.execute("""
        CREATE TABLE public.disclosure_demo (
            id      bigserial PRIMARY KEY,
            country text,
            email   text,
            salary  numeric(12, 2),
            notes   text
        )
    """)
    owner_connection.execute("GRANT SELECT ON public.disclosure_demo TO sql_agent_ro")

    with owner_connection.cursor() as cur:
        cur.executemany(
            "INSERT INTO public.disclosure_demo (country, email, salary, notes) "
            "VALUES (%s, %s, %s, %s)",
            [("FI", f"user{i}@example.com", 50_000 + i, f"note-{i}") for i in range(20)],
        )
    owner_connection.execute(
        "INSERT INTO public.disclosure_demo (country, email, salary, notes) "
        "VALUES ('XX', 'rare@example.com', 999999, %s)",
        (SECRET_NOTE,),
    )


@pytest.fixture
def catalog() -> SchemaCatalog:
    return SchemaCatalog(
        {"disclosure_demo": frozenset({"id", "country", "email", "salary", "notes"})}
    )


def profile_of(
    connection: Conn,
    catalog: SchemaCatalog,
    settings: ProfilingSettings,
    **kwargs: object,
) -> TableProfile:
    profiler = TableProfiler(connection, catalog, settings)
    return profiler.profile("disclosure_demo", **kwargs)  # type: ignore[arg-type]


def rendered(profile: TableProfile) -> str:
    """Everything in the profile, flattened.

    Asserting against the whole object rather than a chosen field means a value
    that escapes through a field this test never considered still fails it.
    """
    return repr(profile)


class TestSensitiveColumnsAreNeverRead:
    def test_a_sensitively_named_column_yields_no_values(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        profile = profile_of(ro_connection, catalog, ProfilingSettings(), columns=["email"])
        email = profile.columns[0]

        assert email.frequent_values == ()
        assert email.sample_values == ()
        assert email.minimum is None
        assert email.distinct_count is None

    def test_no_address_of_any_kind_appears(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        profile = profile_of(ro_connection, catalog, ProfilingSettings())

        assert "@example.com" not in rendered(profile)

    def test_suppression_survives_sampling_being_turned_on(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        """The denylist is not a default that the sampling flag overrides.

        An operator turning on raw sampling is accepting disclosure of the
        columns they reviewed, not of the ones the tool was already refusing.
        """
        settings = ProfilingSettings(profile_allow_value_sampling=True, profile_sample_rows=20)
        profile = profile_of(ro_connection, catalog, settings)

        assert "@example.com" not in rendered(profile)

    def test_salary_is_suppressed_despite_being_a_numeric_type(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        """The type check and the name check are independent gates.

        ``salary`` is numeric, so the extremes rule alone would happily report
        ``max(salary)`` -- which is one person's pay. The name check runs first
        and the column is never read.
        """
        profile = profile_of(ro_connection, catalog, ProfilingSettings(), columns=["salary"])

        assert profile.columns[0].maximum is None
        assert "999999" not in rendered(profile)

    def test_the_reason_is_stated(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        profile = profile_of(ro_connection, catalog, ProfilingSettings(), columns=["email"])

        assert profile.columns[0].is_fully_suppressed

    @pytest.mark.parametrize("pattern", ["email", "ssn", "password", "salary", "credit_card"])
    def test_the_denylist_still_contains_the_patterns_this_relies_on(self, pattern: str) -> None:
        """A guard against the list being trimmed without anyone noticing that
        two independent components read it."""
        assert pattern in DEFAULT_SENSITIVE_PATTERNS


class TestRareValuesAreWithheld:
    def test_a_value_occurring_once_is_not_reported(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        """``XX`` appears exactly once, so it identifies that row's subject.

        This is the control that makes frequent values safe enough to be on by
        default: what is returned is a category label, not a record.
        """
        profile = profile_of(ro_connection, catalog, ProfilingSettings(), columns=["country"])
        values = {v.value for v in profile.columns[0].frequent_values}

        assert values == {"FI"}
        assert "XX" not in rendered(profile)

    def test_a_secret_in_an_innocuously_named_column_does_not_escape(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        """The residual case, stated plainly.

        ``notes`` defeats the name-based denylist by design -- it is the
        example SECURITY.md uses for why that list is a heuristic. What stops
        the secret here is that it occurs once, and once is below the threshold.
        """
        profile = profile_of(ro_connection, catalog, ProfilingSettings(), columns=["notes"])

        assert "123-45-6789" not in rendered(profile)
        assert "4111111111111111" not in rendered(profile)

    def test_raising_the_threshold_withholds_more(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        """The knob moves in the direction its name implies. ``FI`` occurs 20
        times, so a threshold of 50 must withhold it too."""
        settings = ProfilingSettings(profile_min_value_frequency=50)
        profile = profile_of(ro_connection, catalog, settings, columns=["country"])

        assert profile.columns[0].frequent_values == ()

    def test_withholding_is_reported_rather_than_silent(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        profile = profile_of(ro_connection, catalog, ProfilingSettings(), columns=["notes"])

        assert any("occurs fewer than" in w for w in profile.columns[0].withheld)


class TestRawSamplingIsOffByDefault:
    def test_no_sample_values_are_returned_by_default(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        profile = profile_of(ro_connection, catalog, ProfilingSettings())

        assert all(c.sample_values == () for c in profile.columns)

    def test_a_caller_cannot_request_samples_into_existence(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        """``sample_rows`` is a published tool parameter, so its value is chosen
        by a language model reading user-supplied text. It may narrow the
        default; it may not open the gate."""
        profile = profile_of(
            ro_connection, catalog, ProfilingSettings(), columns=["notes"], sample_rows=20
        )

        assert profile.columns[0].sample_values == ()
        assert "123-45-6789" not in rendered(profile)

    def test_the_reason_names_the_flag(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        """So an operator reading a profile knows the setting to change, rather
        than concluding the column is empty."""
        profile = profile_of(ro_connection, catalog, ProfilingSettings(), columns=["country"])

        assert any("PROFILE_ALLOW_VALUE_SAMPLING" in w for w in profile.columns[0].withheld)

    def test_turning_it_on_does_return_values(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        """The negative tests above would also pass if sampling were broken.

        This is what proves they are asserting a gate rather than a bug.
        """
        settings = ProfilingSettings(profile_allow_value_sampling=True, profile_sample_rows=3)
        profile = profile_of(ro_connection, catalog, settings, columns=["country"])

        assert profile.columns[0].sample_values != ()

    def test_values_are_truncated_even_when_sampling_is_allowed(
        self, ro_connection: Conn, catalog: SchemaCatalog, disclosure_table: None
    ) -> None:
        """A single wide text cell would otherwise consume the agent's whole
        context budget, and disclose a whole document with it."""
        settings = ProfilingSettings(
            profile_allow_value_sampling=True, profile_sample_rows=20, profile_max_value_chars=4
        )
        profile = profile_of(ro_connection, catalog, settings, columns=["notes"])

        assert all(len(v) <= 4 for v in profile.columns[0].sample_values)


class TestTheDisclosingStatementIsGreppable:
    """Source-level assertions, so a future refactor has to notice.

    These check the *shape* of the module rather than its behaviour, which is
    unusual and deliberate: the property being protected is that someone
    changing this file can see what they are changing.
    """

    @property
    def source(self) -> str:
        return (
            Path(__file__).resolve().parents[2] / "src" / "profiling" / "profiler.py"
        ).read_text(encoding="utf-8")

    def test_no_identifier_is_interpolated_into_sql(self) -> None:
        """Every identifier goes through ``sql.Identifier``.

        The profiler composes table and column names chosen by a model into
        statements. An f-string or ``%`` on any of those lines would be a
        Critical finding, and it is the kind of change that looks harmless in
        review.
        """
        for line in self.source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("#", '"', "*")):
                continue
            assert 'f"SELECT' not in stripped
            assert "f'SELECT" not in stripped
            assert '" % ' not in stripped

    def test_raw_values_are_read_in_exactly_one_place(self) -> None:
        """Statistics aggregate; ``_sample`` is the only method that returns
        cells verbatim. Keeping it separate is what makes the disclosure
        surface one function rather than a property of the whole file."""
        assert self.source.count("def _sample(") == 1

    def test_the_sensitivity_check_precedes_the_statistics_call(self) -> None:
        """Ordering is the control. Reading values and filtering afterwards
        would put them in this process, in the driver's buffers, and in any
        exception that quoted the query."""
        source = self.source
        assert source.index("is_sensitive(column") < source.index("self._run_stats(")
