"""Write attempts disguised as SELECTs, refused at both layers.

Each payload is asserted twice, and the pairing is the point:

1. **The validator rejects it** — a fast, specific error the agent can act on.
2. **PostgreSQL rejects it too** — the boundary that actually holds.

The second assertion is the one that matters. A parser has to model every
construct the database understands, and the construct it models wrongly is the
one that gets through; the read-only role does not have that failure mode. If
these ever disagree, the validator is the one that is wrong.
"""

from __future__ import annotations

import psycopg
import pytest

from core.settings import ExecutionSettings
from schema.catalog import SchemaCatalog
from validation.validator import EXPLAIN_PREFIX, SQLValidator

pytestmark = pytest.mark.security

type Conn = psycopg.Connection[tuple[object, ...]]

WRITE_ATTEMPTS = [
    pytest.param(
        "WITH gone AS (DELETE FROM public.orders RETURNING id) SELECT * FROM gone",
        id="data-modifying-cte-delete",
    ),
    pytest.param(
        "WITH added AS (INSERT INTO public.orders (id) VALUES (99) RETURNING id) "
        "SELECT * FROM added",
        id="data-modifying-cte-insert",
    ),
    pytest.param(
        "WITH bumped AS (UPDATE public.orders SET total_amount = 0 RETURNING id) "
        "SELECT * FROM bumped",
        id="data-modifying-cte-update",
    ),
    pytest.param("SELECT * INTO public.stolen FROM public.customers", id="select-into"),
    pytest.param("SELECT 1; DROP TABLE public.orders", id="stacked-statements"),
    pytest.param("SELECT id FROM public.orders FOR UPDATE", id="row-locking"),
    pytest.param("CREATE TABLE public.evil (a int)", id="ddl"),
    pytest.param("DROP TABLE public.orders", id="drop"),
    pytest.param("COPY public.customers TO STDOUT", id="copy-out"),
]

SILENTLY_IGNORED = [
    pytest.param("VACUUM FULL public.orders", id="vacuum-full"),
    pytest.param("VACUUM public.orders", id="vacuum"),
    pytest.param("ANALYZE public.orders", id="analyze"),
]
"""Maintenance commands PostgreSQL does **not** refuse from a non-owner.

It emits a warning and skips the table instead. No data changes and nothing is
disclosed, so this is not a hole -- but it is the one place where the two
layers genuinely differ, and the parser is doing work the role does not.
Recorded because "the role refuses everything dangerous" is very nearly true,
and the exception is worth knowing rather than assuming.
"""


@pytest.fixture
def validator(ro_connection: Conn, catalog_schema: None) -> SQLValidator:
    return SQLValidator(
        ro_connection,
        SchemaCatalog({"orders": frozenset({"id", "total_amount", "customer_id"})}),
        ExecutionSettings(),
    )


@pytest.mark.parametrize("sql", WRITE_ATTEMPTS)
def test_validator_refuses_the_write_attempt(validator: SQLValidator, sql: str) -> None:
    result = validator.validate(sql)

    assert not result.valid
    assert result.error_type in {"not_read_only", "multiple_statements"}


@pytest.mark.parametrize("sql", WRITE_ATTEMPTS)
def test_the_database_refuses_it_independently(ro_connection: Conn, sql: str) -> None:
    """Validation removed. The role alone has to hold, and it does."""
    with pytest.raises(psycopg.Error):
        ro_connection.execute(sql)  # type: ignore[arg-type]


def test_the_target_table_survived_every_attempt(ro_connection: Conn, catalog_schema: None) -> None:
    """Proof the tests above were aimed at something real.

    Without this, "the write was refused" and "the write silently did nothing"
    look identical from a passing test suite.
    """
    row = ro_connection.execute("SELECT count(*) FROM public.orders").fetchone()

    assert row is not None
    assert ro_connection.execute("SELECT to_regclass('public.stolen')").fetchone() == (None,)
    assert ro_connection.execute("SELECT to_regclass('public.evil')").fetchone() == (None,)


@pytest.mark.parametrize("sql", SILENTLY_IGNORED)
def test_maintenance_commands_are_stopped_by_the_parser_not_the_role(
    validator: SQLValidator, ro_connection: Conn, sql: str
) -> None:
    """The one asymmetry between the two layers, asserted rather than assumed."""
    assert not validator.validate(sql).valid

    # No exception: PostgreSQL warns and skips rather than refusing.
    ro_connection.execute(sql)  # type: ignore[arg-type]


def test_explain_is_never_explain_analyze() -> None:
    """ANALYZE executes the statement.

    Adding it would turn the side-effect-free tier the agent is told it may
    retry freely into the expensive one it must not -- silently, because the
    results are discarded either way. Asserted against the constant that is
    actually executed, so a docstring mentioning ANALYZE cannot fail this and,
    more importantly, cannot pass it either.
    """
    assert EXPLAIN_PREFIX.startswith("EXPLAIN ")
    assert "ANALYZE" not in EXPLAIN_PREFIX.upper()
