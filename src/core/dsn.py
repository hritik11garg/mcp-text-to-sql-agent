"""Connection strings: one form for each driver, and never in an error message.

Two problems that only appear when a real ``.env`` meets real code, which is
why both survived to be found by hand rather than by a test.

**A SQLAlchemy URL is not a libpq URL.** Alembic needs
``postgresql+psycopg://...`` to select its driver. psycopg's own parser rejects
that string outright -- ``missing "=" after "postgresql+psycopg://..."`` -- so
every component that connects directly needs the ``+driver`` removed.
``.env.example`` ships the SQLAlchemy form for ``DATABASE_URL`` and the plain
form for ``DATABASE_RO_URL``, so an operator following it exactly gets a
working read-only connection and an owner connection that cannot open. The test
suite never caught it because ``conftest`` normalised the URL itself, which
meant the workaround lived in the tests and the defect lived in production.

**A driver error quotes the whole DSN back at you.** psycopg puts the
connection string in the message, password included, and any handler that logs
``str(exc)`` writes a live credential to a terminal, a log file, or a CI
transcript. SECURITY.md section 9 says never log connection strings; this is
the function that makes that true rather than aspirational.
"""

from __future__ import annotations

import re

__all__ = ["libpq_dsn", "redact_dsn"]

_DRIVER = re.compile(r"^(postgres(?:ql)?)\+[A-Za-z0-9_]+://")
_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z0-9+]*://)(?P<user>[^:/@\s]+):(?P<pw>[^@\s]*)@")
_KEYWORD_PASSWORD = re.compile(r"\bpassword\s*=\s*(?P<pw>[^\s'\"]+)")


def libpq_dsn(url: object) -> str:
    """Normalise a configured URL into something psycopg can parse.

    Accepts a ``SecretStr``, a plain string, or anything with
    ``get_secret_value``. Strips a SQLAlchemy ``+driver`` suffix and leaves
    everything else untouched -- this rewrites the scheme and nothing more, so
    a keyword/value DSN or a URL with query parameters passes through intact.
    """
    raw = url.get_secret_value() if hasattr(url, "get_secret_value") else str(url)
    return _DRIVER.sub(r"\1://", raw)


def redact_dsn(text: str) -> str:
    """Remove credentials from anything about to be logged or shown.

    Covers both DSN spellings, because psycopg errors quote whichever form was
    passed in: the URL form ``postgresql://user:secret@host`` and the
    keyword form ``password=secret``.

    The user name is deliberately kept. Which *role* failed to connect is the
    single most useful fact in a connection error -- owner versus read-only is
    the difference between a broken deployment and a working containment
    boundary -- and it is not a secret.
    """
    text = _CREDENTIALS.sub(lambda m: f"{m['scheme']}{m['user']}:[REDACTED]@", text)
    return _KEYWORD_PASSWORD.sub("password=[REDACTED]", text)
