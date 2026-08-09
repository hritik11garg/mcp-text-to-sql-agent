"""No generated write is ever accepted -- asserted over generated statements.

The first of the properties named in docs/project/ENGINEERING_MATRIX.md
section 38. ``tests/unit/test_validator.py`` and
``tests/security/test_sql_validation.py`` already assert this over examples
somebody chose; what follows asserts it over examples nobody chose.

**This is a second barrier, not the boundary.** The read-only role is
(docs/operations/SECURITY.md section 5, invariant I-1). A property test on a
parser cannot make the parser complete, and the module docstring on
``validation.validator`` is explicit that a parser is a poor place to enforce
containment. What this suite buys is that the *second* barrier fails on
statements a human would not have thought to write, before the first barrier
has to catch them.

The catalog is deliberately **empty** in these tests. An empty catalog makes
``_check_identifiers`` return immediately, so a rejection here can only have
come from the read-only stage or from parsing -- never from ``orders`` not
being a table the test happened to declare. A property that passes because the
identifier stage rejected everything would prove nothing about write
containment.
"""

from __future__ import annotations

from typing import Any, NoReturn

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests import strategies as sql

from core.settings import ExecutionSettings
from schema.catalog import SchemaCatalog
from validation.validator import SQLValidator, ValidationStage

pytestmark = pytest.mark.property


class ExplodingConnection:
    """Fails the test if a stage reaches for the database.

    Same claim as ``tests/unit/test_validator.py``, and it carries more weight
    here: these tests run hundreds of examples, so a stage that quietly opened
    a cursor would turn a fast suite into one that needs a database and a
    minute -- and the first thing anyone does with a slow security suite is
    stop running it.
    """

    def cursor(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("static validation must not touch the database")

    def transaction(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("static validation must not touch the database")


def make_validator() -> SQLValidator:
    return SQLValidator(
        ExplodingConnection(),  # type: ignore[arg-type]
        SchemaCatalog({}),
        ExecutionSettings(),
    )


VALIDATOR = make_validator()
"""Built once. It holds no per-call state, and rebuilding it per example would
add construction cost to every one of five hundred runs for no assertion."""


class TestNoWriteIsAccepted:
    """The property, in the four positions a write can occupy."""

    @given(sql.writes())
    def test_a_bare_write_is_refused(self, statement: str) -> None:
        result = VALIDATOR.validate_static(statement)

        assert not result.valid, f"accepted a write: {statement!r}"

    @given(sql.writes(), sql.selects())
    def test_a_write_stacked_after_a_select_is_refused(self, write: str, select: str) -> None:
        """The classic injection shape, and the one a root-node check misses.

        ``SELECT 1; DROP TABLE orders`` has a perfectly innocent first
        statement. Anything that inspects only the first parsed statement --
        or stops at the first semicolon -- accepts it.
        """
        result = VALIDATOR.validate_static(f"{select}; {write}")

        assert not result.valid, f"accepted a stacked write: {write!r}"

    @given(sql.writes(), sql.selects())
    def test_a_write_stacked_before_a_select_is_refused(self, write: str, select: str) -> None:
        """The same shape reversed, because "check the last statement" is the
        other half-fix somebody reaches for."""
        result = VALIDATOR.validate_static(f"{write}; {select}")

        assert not result.valid, f"accepted a stacked write: {write!r}"

    @given(sql.dml(), sql.selects())
    def test_a_data_modifying_cte_is_refused_at_the_read_only_stage(
        self, write: str, select: str
    ) -> None:
        """The strongest form of the property, and the one worth reading.

        This statement **parses cleanly**, has a ``Select`` at its root, and
        deletes every row in the table. So it is not enough to assert that it
        was refused -- a parse error would satisfy that while proving the
        opposite. The stage is asserted because only the tree walk can produce
        it, and the tree walk is the thing under test.
        """
        del select  # drawn for shrinking symmetry with the sibling properties
        # S608 is suppressed below: building hostile SQL from generated input
        # is the test, and the string goes to the validator, never a database.
        statement = f"WITH doomed AS ({write} RETURNING id) SELECT * FROM doomed"  # noqa: S608

        result = VALIDATOR.validate_static(statement)

        assert not result.valid
        assert result.stage_failed is ValidationStage.READ_ONLY, (
            f"refused, but not as a write: {statement!r} failed at {result.stage_failed}"
        )
        assert result.error_type == "not_read_only"

    @given(sql.writes())
    def test_a_write_inside_a_subquery_is_refused(self, write: str) -> None:
        result = VALIDATOR.validate_static(
            f"SELECT * FROM orders WHERE id IN ({write})"  # noqa: S608 -- see above
        )

        assert not result.valid, f"accepted a nested write: {write!r}"


class TestNoReadIsRefusedAsAWrite:
    """The dual, without which the suite above is satisfied by rejecting
    everything.

    A false rejection is not a security failure, so it does not belong in the
    release gate on its own -- but it is the failure that makes people delete
    the check. A validator that refuses ``UNION`` is a validator somebody
    disables.
    """

    @given(sql.selects())
    def test_a_read_only_statement_never_fails_the_read_only_stage(self, statement: str) -> None:
        result = VALIDATOR.validate_static(statement)

        assert result.stage_failed is not ValidationStage.READ_ONLY, (
            f"refused a read-only statement as a write: {statement!r}"
        )

    @given(sql.selects())
    def test_a_read_only_statement_is_accepted_outright(self, statement: str) -> None:
        """Stronger than the above: not merely "not refused as a write", but
        accepted. Any failure here is a parse gap, which is worth knowing about
        separately from a containment gap."""
        result = VALIDATOR.validate_static(statement)

        assert result.valid, (
            f"refused a valid read-only statement at {result.stage_failed}: "
            f"{statement!r} -- {result.message}"
        )


class TestTheRefusalCarriesNoDriverDetail:
    """Static rejection messages are written here, not by a driver.

    MCP.md section 6 forbids driver output in tool errors, and the reason is
    that PostgreSQL's messages carry statement positions and context lines that
    describe the schema. Nothing in the static stages talks to a database, so
    the only way a driver string could appear is if one were pasted into a
    message -- which is exactly the kind of edit a property catches and a
    review does not.
    """

    @given(sql.writes())
    def test_a_refusal_never_echoes_the_whole_statement(self, statement: str) -> None:
        result = VALIDATOR.validate_static(statement)

        assert result.message is not None
        assert statement not in result.message


class TestIllegalInputIsRefusedRatherThanCrashing:
    """Arbitrary text is not a security property; not crashing on it is.

    This is the one strategy here that *is* a fuzzer, and it is deliberately
    narrow in what it claims. It does not assert that random text is rejected
    -- ``SELECT 1`` is random text that should be accepted. It asserts that
    ``validate_static`` always **returns**, because the caller is a loop in the
    self-correction path and an exception there aborts a request rather than
    correcting it.
    """

    @given(st.text(max_size=200))
    def test_arbitrary_text_returns_a_result(self, text: str) -> None:
        result = VALIDATOR.validate_static(text)

        assert result.valid in (True, False)
        if not result.valid:
            assert result.stage_failed is not None
            assert result.error_type is not None
            assert result.message
