"""Sample values are real customer data. These tests decide where it can go.

Serialized schema text is written to ``agent_meta.schema_elements`` and later
quoted into prompts sent to a third-party model. That path bypasses the
read-only role entirely -- the data is read legitimately and then transmitted --
so the containment boundary that protects everything else does not apply here.
OWASP LLM06 (sensitive information disclosure).

The controls, in order of how much they are relied on:

1. Sampling is **off by default**. This is the real one.
2. Sensitive-looking columns are never read, even when it is on.
3. Serialization drops sample values for those columns a second time.
4. Values are truncated in SQL, so nothing large crosses the wire.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from core.settings import RetrievalSettings
from schema.introspection import PostgresIntrospector
from schema.serialization import serialize_column

pytestmark = pytest.mark.security

type Conn = psycopg.Connection[tuple[object, ...]]


class TestDefaultIsNoSampling:
    def test_setting_defaults_to_off(self) -> None:
        # The control that actually protects an operator who never read the
        # docs. If this default ever flips, every deployment starts sending
        # real rows to a third party after a routine upgrade.
        assert RetrievalSettings().schema_sample_values is False

    def test_introspection_reads_no_values_by_default(
        self, ro_connection: Conn, catalog_schema: None
    ) -> None:
        snapshot = PostgresIntrospector(ro_connection, schema="public").snapshot()
        sampled = [
            column for table in snapshot.tables for column in table.columns if column.sample_values
        ]
        assert sampled == []


class TestSensitiveColumnsAreNeverRead:
    @pytest.fixture
    def sampling_introspector(
        self, ro_connection: Conn, catalog_schema: None
    ) -> PostgresIntrospector:
        return PostgresIntrospector(
            ro_connection,
            schema="public",
            sample_values=True,
            sample_count=3,
        )

    def test_email_values_never_leave_the_database(
        self, sampling_introspector: PostgresIntrospector
    ) -> None:
        snapshot = sampling_introspector.snapshot()
        customers = next(t for t in snapshot.tables if t.name == "customers")
        email = next(c for c in customers.columns if c.name == "email")

        assert email.sample_values == ()
        # And the serialized form -- the text that actually reaches a prompt --
        # carries nothing either.
        assert "example.com" not in serialize_column(email)

    def test_non_sensitive_columns_are_still_sampled(
        self, sampling_introspector: PostgresIntrospector
    ) -> None:
        """A denylist that blocked everything would pass every test above and
        make the feature useless. Assert it still does its job."""
        snapshot = sampling_introspector.snapshot()
        customers = next(t for t in snapshot.tables if t.name == "customers")
        country = next(c for c in customers.columns if c.name == "country")

        assert set(country.sample_values) == {"FI", "GB"}

    def test_extra_patterns_from_configuration_are_applied(
        self, ro_connection: Conn, catalog_schema: None
    ) -> None:
        introspector = PostgresIntrospector(
            ro_connection,
            schema="public",
            sample_values=True,
            sensitive_patterns=("country",),
        )
        snapshot = introspector.snapshot()
        customers = next(t for t in snapshot.tables if t.name == "customers")
        country = next(c for c in customers.columns if c.name == "country")

        assert country.sample_values == ()


class TestSamplingIsBounded:
    def test_values_are_truncated_in_sql(self, ro_connection: Conn, catalog_schema: None) -> None:
        """Truncation happens in the database, not after fetching.

        A text column holding a megabyte per row would otherwise be pulled
        across the wire in full before being cut -- an availability problem
        (memory) as well as a disclosure one.
        """
        introspector = PostgresIntrospector(
            ro_connection,
            schema="public",
            sample_values=True,
            sample_max_chars=2,
        )
        snapshot = introspector.snapshot()
        customers = next(t for t in snapshot.tables if t.name == "customers")
        names = next(c for c in customers.columns if c.name == "name")

        assert all(len(value) <= 2 for value in names.sample_values)

    def test_sample_count_is_respected(self, ro_connection: Conn, catalog_schema: None) -> None:
        introspector = PostgresIntrospector(
            ro_connection, schema="public", sample_values=True, sample_count=1
        )
        snapshot = introspector.snapshot()
        customers = next(t for t in snapshot.tables if t.name == "customers")
        assert all(len(c.sample_values) <= 1 for c in customers.columns)

    def test_zero_count_disables_sampling(self, ro_connection: Conn, catalog_schema: None) -> None:
        introspector = PostgresIntrospector(
            ro_connection, schema="public", sample_values=True, sample_count=0
        )
        snapshot = introspector.snapshot()
        assert all(not c.sample_values for t in snapshot.tables for c in t.columns)


class TestIdentifierInjection:
    """Table and column names are composed into SQL because identifiers cannot
    be bound as parameters. They come from the catalog, not from a request --
    but anyone who can create a table controls them."""

    def test_hostile_identifiers_are_quoted_not_interpolated(
        self, owner_connection: Conn, catalog_schema: None
    ) -> None:
        hostile = 'evil"; DROP TABLE public.customers; --'
        owner_connection.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS public.injection_probe ({col} text)").format(
                col=sql.Identifier(hostile)
            )
        )
        owner_connection.execute("INSERT INTO public.injection_probe VALUES ('harmless')")
        try:
            snapshot = PostgresIntrospector(
                owner_connection, schema="public", sample_values=True
            ).snapshot()

            probe = next(t for t in snapshot.tables if t.name == "injection_probe")
            assert probe.columns[0].name == hostile
            assert probe.columns[0].sample_values == ("harmless",)

            # The payload named customers explicitly. It is still there.
            owner_connection.execute("SELECT 1 FROM public.customers LIMIT 1")
        finally:
            owner_connection.execute("DROP TABLE IF EXISTS public.injection_probe")
