"""Connection strings: parsed correctly, and never echoed back with a password.

Both of these were found by running the loader against a real ``.env`` rather
than by any test, which is the uncomfortable part. The URL-form bug meant the
owner connection could not open at all for anyone following ``.env.example``;
the redaction bug printed a live database password to a terminal the first time
that failure happened.

SECURITY.md section 9 already said "never log connection strings". These are
the tests that make it true rather than aspirational.
"""

from __future__ import annotations

import psycopg
import pytest

from composition.resources import _connect
from core.dsn import libpq_dsn, redact_dsn
from core.exceptions import ConfigurationError

SECRET = "hunter2-not-a-real-password"  # fixture value; every test asserts it is absent


class TestLibpqDsn:
    def test_strips_a_sqlalchemy_driver_suffix(self) -> None:
        # `.env.example` ships this form for DATABASE_URL because alembic needs
        # it. psycopg rejects it outright with `missing "=" after ...`.
        assert (
            libpq_dsn("postgresql+psycopg://u:p@localhost:5442/analytics")
            == "postgresql://u:p@localhost:5442/analytics"
        )

    def test_leaves_a_plain_url_untouched(self) -> None:
        url = "postgresql://u:p@localhost:5432/analytics"
        assert libpq_dsn(url) == url

    def test_handles_the_postgres_scheme_alias(self) -> None:
        assert libpq_dsn("postgres+psycopg://u:p@h/db") == "postgres://u:p@h/db"

    def test_leaves_a_keyword_value_dsn_untouched(self) -> None:
        dsn = "host=localhost port=5442 dbname=analytics user=u password=p"
        assert libpq_dsn(dsn) == dsn

    def test_preserves_query_parameters(self) -> None:
        assert (
            libpq_dsn("postgresql+psycopg://u:p@h/db?sslmode=require")
            == "postgresql://u:p@h/db?sslmode=require"
        )

    def test_accepts_a_secretstr(self) -> None:
        from pydantic import SecretStr

        assert libpq_dsn(SecretStr("postgresql+psycopg://u:p@h/db")) == "postgresql://u:p@h/db"


class TestRedaction:
    def test_removes_a_password_from_a_url(self) -> None:
        text = f"connection failed: postgresql://t2sql_owner:{SECRET}@localhost:5442/analytics"
        redacted = redact_dsn(text)

        assert SECRET not in redacted
        assert "[REDACTED]" in redacted

    def test_removes_a_password_from_a_keyword_dsn(self) -> None:
        assert SECRET not in redact_dsn(f"host=h dbname=d user=u password={SECRET}")

    def test_keeps_the_role_name(self) -> None:
        # Which role failed is the most useful fact in a connection error --
        # owner versus read-only is the difference between a broken deployment
        # and a working containment boundary -- and it is not a secret.
        redacted = redact_dsn(f"postgresql://sql_agent_login:{SECRET}@localhost/analytics")
        assert "sql_agent_login" in redacted
        assert SECRET not in redacted

    def test_leaves_text_with_no_credentials_alone(self) -> None:
        assert redact_dsn("relation does not exist") == "relation does not exist"

    def test_redacts_every_occurrence(self) -> None:
        text = f"a postgresql://u:{SECRET}@h/d b postgresql://v:{SECRET}@h/d"
        assert SECRET not in redact_dsn(text)


class TestConnectionFailuresDoNotLeak:
    """The path that actually printed a password."""

    def test_an_unreachable_host_reports_without_the_credential(self) -> None:
        # Port 1 is reliably closed, so this fails fast without a live server.
        url = f"postgresql+psycopg://t2sql_owner:{SECRET}@127.0.0.1:1/analytics"

        with pytest.raises(ConfigurationError) as caught:
            _connect(url, "DATABASE_URL", timeout_ms=1_000)

        message = str(caught.value)
        assert SECRET not in message
        assert "DATABASE_URL" in message

    def test_the_original_exception_is_not_chained_into_the_message(self) -> None:
        # `raise ... from None` on purpose: a chained psycopg error carries the
        # unredacted DSN in its own __str__, and a traceback printed by any
        # outer handler would put it back on screen.
        url = f"postgresql://t2sql_owner:{SECRET}@127.0.0.1:1/analytics"

        with pytest.raises(ConfigurationError) as caught:
            _connect(url, "DATABASE_URL", timeout_ms=1_000)

        assert caught.value.__cause__ is None
        assert SECRET not in repr(caught.value)

    def test_psycopg_really_does_quote_the_dsn(self) -> None:
        # The premise of all of the above, pinned so it cannot rot silently.
        #
        # Written first with an unreachable *port*, which fails with a bare
        # "connection timeout expired" and no DSN -- so the test failed and was
        # right to. The leak comes from the PARSE error: an unrecognised scheme
        # makes psycopg quote the entire string back, which is exactly what
        # `postgresql+psycopg://` produces and exactly what put a live password
        # on screen.
        with pytest.raises(psycopg.Error) as caught:
            psycopg.connect(f"postgresql+psycopg://t2sql_owner:{SECRET}@localhost/analytics")

        assert SECRET in str(caught.value)
        assert SECRET not in redact_dsn(str(caught.value))
