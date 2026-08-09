"""``truncated=true`` implies the *server's* limit did the cutting.

The second property named in docs/project/ENGINEERING_MATRIX.md section 38.

``tests/unit/test_row_limit.py`` covers ``apply_row_limit`` over five chosen
cases. This covers the whole executor path -- clamp, inject, fetch, decide --
over generated combinations of four interacting numbers: the caller's request,
the query's own ``LIMIT``, the configured default and the configured ceiling.
Four numbers is 24 orderings, and the hand-written suite exercises five.

**The distinction being defended is not cosmetic.** ``truncated`` tells the
agent it lost data. Reporting it when a caller's own ``LIMIT 10`` returned ten
rows would make the agent re-ask a question it had already answered correctly;
*failing* to report it when the ceiling cut a result would let it state a total
that is silently wrong. The second is the one that reaches a user as a
confident number, which is why the property is written as an implication in
both directions.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests import strategies as sql

from core.settings import ExecutionSettings
from execution.executor import SQLExecutor, apply_row_limit
from validation.validator import ValidationResult

pytestmark = pytest.mark.property


# --- a database that honours the limit it is given -------------------------

_TRAILING_LIMIT = re.compile(r"LIMIT\s+(\d+)\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Column:
    name: str


class _FakeCursor:
    """Returns rows *subject to the LIMIT it was handed*.

    This is the fake's one real claim, and the suite is worthless without it.
    A fake that ignored the injected ``LIMIT`` and returned everything would
    make ``truncated`` come out right by accident -- the executor compares
    what it *received* against the limit, so a fake that over-delivers hides
    the case where injection failed to happen at all.

    The limit is read back with a regular expression rather than by calling
    the production helper. Reusing ``_existing_limit`` here would compare the
    code against itself.
    """

    description = (_Column("id"),)

    def __init__(self, available: int) -> None:
        self._available = available
        self._rows: list[tuple[Any, ...]] = []
        self.statements: list[str] = []

    def execute(self, statement: str, params: object = None) -> None:
        del params
        self.statements.append(statement)
        if statement.lstrip().upper().startswith("SELECT SET_CONFIG"):
            return
        match = _TRAILING_LIMIT.search(statement)
        assert match is not None, (
            f"no trailing LIMIT in the executed SQL: {statement!r} -- the row "
            f"limit was not injected, and every assertion below would pass "
            f"anyway"
        )
        limit = int(match.group(1))
        self._rows = [(index,) for index in range(min(self._available, limit))]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, available: int) -> None:
        self.cur = _FakeCursor(available)

    def cursor(self) -> _FakeCursor:
        return self.cur

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield


class _FakeSource:
    def __init__(self, available: int) -> None:
        self.conn = _FakeConnection(available)

    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        yield self.conn


class _AlwaysValid:
    """Validation is not what is under test here.

    Substituted rather than mocked out, because ``execute`` calls the validator
    unconditionally and a real one would need a database for ``EXPLAIN``.
    Write containment over generated input is asserted separately, in
    tests/security/test_property_write_containment.py.
    """

    def validate(self, statement: str) -> ValidationResult:
        return ValidationResult(valid=True, sql=statement)


class _RecordingAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, **fields: Any) -> None:
        self.rows.append(fields)


# --- the reference model ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Expected:
    """What the four numbers imply, worked out independently of the executor."""

    effective: int
    server_capped: bool
    row_count: int
    truncated: bool


def _model(
    *, available: int, requested: int | None, query_limit: int | None, settings: ExecutionSettings
) -> _Expected:
    ceiling = settings.clamp_rows(requested)
    if query_limit is None:
        effective, server_capped = ceiling, True
    else:
        effective, server_capped = min(query_limit, ceiling), query_limit > ceiling

    return _Expected(
        effective=effective,
        server_capped=server_capped,
        row_count=min(available, effective),
        truncated=available > effective and server_capped,
    )


SETTINGS = ExecutionSettings(max_rows_default=20, max_rows_ceiling=40)
"""Small numbers on purpose.

The interesting behaviour is at the boundaries -- available exactly at the
limit, one over, one under -- and a ceiling of 5,000 against a generator
producing sixty rows would put every example on the same side of it.
"""


def _build(available: int) -> tuple[SQLExecutor, _FakeSource, _RecordingAudit]:
    source = _FakeSource(available)
    audit = _RecordingAudit()
    executor = SQLExecutor(
        source,  # type: ignore[arg-type]
        _AlwaysValid(),  # type: ignore[arg-type]
        SETTINGS,
        audit=audit,  # type: ignore[arg-type]
    )
    return executor, source, audit


AVAILABLE = st.integers(min_value=0, max_value=60)
REQUESTED = st.none() | st.integers(min_value=0, max_value=60)
QUERY_LIMIT = st.none() | st.integers(min_value=0, max_value=60)


@st.composite
def _where_the_callers_limit_wins(draw: st.DrawFn) -> tuple[int | None, int]:
    """A request whose own ``LIMIT`` is at or below the effective ceiling.

    Constructed rather than filtered. The first version drew both numbers
    freely and used ``assume`` to discard the rest, which threw away most of
    the examples and tripped Hypothesis' filtering health check -- and the
    deeper problem is that heavy filtering distorts the distribution, so the
    surviving examples cluster wherever the filter happens to be generous.
    Deriving the second number from the first keeps every example useful.
    """
    requested = draw(REQUESTED)
    ceiling = SETTINGS.clamp_rows(requested)
    return requested, draw(st.integers(min_value=0, max_value=ceiling))


def _query(query_limit: int | None) -> str:
    # S608 is suppressed below: the interpolated value is a generated integer
    # and the string goes to a fake cursor, never a database. Varying the
    # query's own LIMIT is the point of the suite.
    if query_limit is None:
        return "SELECT id FROM orders"
    return f"SELECT id FROM orders LIMIT {query_limit}"  # noqa: S608


class TestTruncation:
    @given(AVAILABLE, REQUESTED, QUERY_LIMIT)
    def test_truncated_means_the_server_limit_did_the_cutting(
        self, available: int, requested: int | None, query_limit: int | None
    ) -> None:
        """The property, stated as the implication it is.

        ``truncated`` may only be true when **both** halves hold: rows were
        actually dropped, *and* the limit that dropped them was the server's
        rather than the caller's own.
        """
        executor, _, _ = _build(available)

        result = executor.execute(_query(query_limit), max_rows=requested)

        if result.truncated:
            assert available > result.row_count, "reported truncation with nothing left behind"
            assert result.row_limit == SETTINGS.clamp_rows(requested), (
                "reported truncation, but the caller's own LIMIT is what bound the result"
            )

    @given(AVAILABLE, _where_the_callers_limit_wins())
    def test_a_callers_own_limit_is_never_reported_as_truncation(
        self, available: int, case: tuple[int | None, int]
    ) -> None:
        """The converse, and the one that stops the agent re-asking.

        ``SELECT ... LIMIT 10`` returning ten rows is a complete answer.
        Calling it truncated would tell the agent it had lost data it never
        asked for.
        """
        requested, query_limit = case
        executor, _, _ = _build(available)

        result = executor.execute(_query(query_limit), max_rows=requested)

        assert not result.truncated

    @given(AVAILABLE, REQUESTED, QUERY_LIMIT)
    def test_the_result_matches_the_model_exactly(
        self, available: int, requested: int | None, query_limit: int | None
    ) -> None:
        """Everything at once, against limits worked out independently."""
        executor, _, _ = _build(available)
        expected = _model(
            available=available,
            requested=requested,
            query_limit=query_limit,
            settings=SETTINGS,
        )

        result = executor.execute(_query(query_limit), max_rows=requested)

        assert result.row_limit == expected.effective
        assert result.row_count == expected.row_count
        assert result.truncated == expected.truncated
        assert len(result.rows) == result.row_count

    @given(AVAILABLE, REQUESTED, QUERY_LIMIT)
    def test_a_caller_never_receives_more_than_the_ceiling(
        self, available: int, requested: int | None, query_limit: int | None
    ) -> None:
        """The ceiling is a ceiling. Nothing a caller sends may raise it."""
        executor, _, _ = _build(available)

        result = executor.execute(_query(query_limit), max_rows=requested)

        assert result.row_count <= result.row_limit
        assert result.row_limit <= SETTINGS.max_rows_ceiling

    @given(AVAILABLE, REQUESTED, QUERY_LIMIT)
    def test_one_extra_row_is_always_requested(
        self, available: int, requested: int | None, query_limit: int | None
    ) -> None:
        """How a full page and a truncated one are told apart without a second
        query -- and the mechanism the whole property rests on."""
        executor, source, _ = _build(available)

        result = executor.execute(_query(query_limit), max_rows=requested)

        executed = source.conn.cur.statements[-1]
        match = _TRAILING_LIMIT.search(executed)
        assert match is not None
        assert int(match.group(1)) == result.row_limit + 1

    @given(AVAILABLE, REQUESTED, QUERY_LIMIT)
    def test_every_attempt_leaves_exactly_one_audit_row(
        self, available: int, requested: int | None, query_limit: int | None
    ) -> None:
        """An execution that leaves no trail is the failure nobody detects.

        Written as a property after exactly that defect turned up in the
        validator: an unhandled parse exception raised *before* the executor's
        rejection audit, so the attempt vanished. Counting rows over generated
        input is what would have caught it here.
        """
        executor, _, audit = _build(available)

        executor.execute(_query(query_limit), max_rows=requested)

        assert len(audit.rows) == 1
        assert audit.rows[0]["outcome"] == "success"
        assert audit.rows[0]["truncated"] is not None


class TestRowLimitingIsTotalOverWhatValidationAccepts:
    """Whatever the validator accepts, the executor can bound.

    A cross-component property, and the reason ``ALLOWED_ROOTS`` is public.
    Two independent notions of "a statement we can run" would drift, and the
    direction they drift in is the executor choking on a query validation just
    approved -- an internal error on a query the agent was told was fine.
    """

    @given(sql.selects())
    def test_every_accepted_select_can_be_row_limited(self, statement: str) -> None:
        bounded, effective, _ = apply_row_limit(statement, 100)

        assert effective <= 100
        assert _TRAILING_LIMIT.search(bounded) is not None

    @given(sql.selects(), st.integers(min_value=1, max_value=1000))
    def test_the_effective_limit_never_exceeds_the_ceiling(
        self, statement: str, ceiling: int
    ) -> None:
        _, effective, _ = apply_row_limit(statement, ceiling)

        assert effective <= ceiling
