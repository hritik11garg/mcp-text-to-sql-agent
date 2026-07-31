"""Running a validated query under limits that the caller cannot raise.

This is the only component that executes model-authored SQL, and it is written
on the assumption that everything upstream of it has failed:

**It re-validates.** It does not trust that `validate_sql` was called. Another
MCP host can call `execute_sql` directly, and a tool that is only safe when
invoked in the right order is not safe -- see docs/architecture/MCP.md section 3.3.

**It bounds every result.** The row limit is injected into the AST rather than
requested in the prompt, because an instruction to a model is not an
enforcement mechanism.

**It records what happened.** The audit write goes over a *different*
connection, as the owner, because the read-only role has no privileges on
``agent_meta`` -- generated SQL cannot read or erase the trail that records it.
"""

from __future__ import annotations

import logging
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import psycopg
import sqlglot
from psycopg import Connection
from sqlglot import exp

from core.exceptions import (
    ExecutionError,
    PermissionDeniedError,
    SQLValidationError,
    StatementTimeoutError,
)
from core.settings import ExecutionSettings
from validation.validator import ALLOWED_ROOTS, DIALECT, SQLValidator

logger = logging.getLogger(__name__)

_AUDIT_SQL = """
    INSERT INTO agent_meta.query_audit
        (request_id, trace_id, session_id, question, generated_sql,
         validation_attempts, db_role, duration_ms, row_count, truncated,
         outcome, error_type)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


@runtime_checkable
class ConnectionSource(Protocol):
    """Anything that can hand out a read-only connection.

    A ``psycopg_pool.ConnectionPool`` satisfies this structurally, and so does
    the single-connection adapter below. The executor is written against the
    protocol so that introducing a real pool with the API layer is a wiring
    change rather than a rewrite -- there is no concurrent caller yet, and a
    pool with one client is machinery without a job.
    """

    def connection(self) -> AbstractContextManager[Connection[Any]]: ...


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Rows, plus everything needed to explain how they were bounded."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    truncated: bool
    duration_ms: float
    executed_sql: str
    """The SQL *as executed*, after the row limit was injected.

    Kept because the query the agent wrote and the query the database ran are
    not the same string, and debugging a result against the wrong one wastes a
    great deal of time.
    """

    row_limit: int
    estimated_cost: float | None = None


class SQLExecutor:
    """Executes a query under a row limit, a statement timeout, and an audit.

    Args:
        source: Supplies **read-only** connections.
        validator: Re-run on every call. Never assumed to have run already.
        settings: The ceilings. A caller may ask for less, never for more.
        audit: Optional; writes to ``agent_meta`` over the owner connection.
    """

    def __init__(
        self,
        source: ConnectionSource,
        validator: SQLValidator,
        settings: ExecutionSettings,
        *,
        audit: AuditLog | None = None,
        dialect: str = DIALECT,
    ) -> None:
        self._source = source
        self._validator = validator
        self._settings = settings
        self._audit = audit
        self._dialect = dialect

    def execute(
        self,
        sql: str,
        *,
        max_rows: int | None = None,
        timeout_ms: int | None = None,
        question: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        validation_attempts: int = 1,
    ) -> QueryResult:
        """Validate, bound, run, and record.

        Raises:
            SQLValidationError: re-validation rejected the query.
            StatementTimeoutError: the query exceeded its timeout.
            PermissionDeniedError: the read-only role refused it.
            ExecutionError: anything else the database refused.
        """
        limit = self._settings.clamp_rows(max_rows)
        timeout = self._settings.clamp_timeout_ms(timeout_ms)

        validation = self._validator.validate(sql)
        if not validation.valid:
            self._record(
                outcome="rejected",
                sql=sql,
                error_type=validation.error_type,
                question=question,
                request_id=request_id,
                trace_id=trace_id,
                session_id=session_id,
                validation_attempts=validation_attempts,
            )
            raise SQLValidationError(
                validation.error_type or "invalid",
                validation.feedback,
                validation.identifier,
            )

        bounded, effective, server_capped = apply_row_limit(sql, limit, dialect=self._dialect)

        started = time.perf_counter()
        try:
            with self._source.connection() as conn, conn.transaction(), conn.cursor() as cur:
                cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout),))
                cur.execute(bounded)
                fetched = cur.fetchall()
                columns = tuple(d.name for d in cur.description or ())
        except psycopg.Error as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            error = _translate(exc)
            self._record(
                outcome="error",
                sql=bounded,
                error_type=type(error).__name__,
                duration_ms=duration_ms,
                question=question,
                request_id=request_id,
                trace_id=trace_id,
                session_id=session_id,
                validation_attempts=validation_attempts,
            )
            raise error from exc

        duration_ms = (time.perf_counter() - started) * 1000

        # One row beyond the limit was requested precisely so that "there were
        # exactly N rows" and "there were more than N" can be told apart.
        overflowed = len(fetched) > effective
        rows = tuple(tuple(row) for row in fetched[:effective])
        truncated = overflowed and server_capped

        self._record(
            outcome="success",
            sql=bounded,
            duration_ms=duration_ms,
            row_count=len(rows),
            truncated=truncated,
            question=question,
            request_id=request_id,
            trace_id=trace_id,
            session_id=session_id,
            validation_attempts=validation_attempts,
        )

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            duration_ms=duration_ms,
            executed_sql=bounded,
            row_limit=effective,
            estimated_cost=validation.estimated_cost,
        )

    def _record(self, **fields: Any) -> None:
        if self._audit is not None:
            self._audit.record(**fields)


def apply_row_limit(sql: str, max_rows: int, *, dialect: str = DIALECT) -> tuple[str, int, bool]:
    """Bound a query's result size at the AST level.

    Returns ``(sql, effective_limit, server_capped)``.

    **Smaller wins.** A caller asking for fewer rows than the ceiling gets what
    they asked for; a caller asking for more gets the ceiling. The limit is set
    on the parse tree rather than appended as text, because appending is wrong
    for a query that already has a `LIMIT`, wrong for `UNION`, and wrong for
    anything ending in a comment or a semicolon.

    ``server_capped`` reports whether *this* limit did the cutting, which is
    what separates "the result was truncated" from "the query asked for ten
    rows and got ten rows". Reporting the second as truncation would tell the
    agent it had lost data when it had not.

    One extra row is requested so the caller can distinguish a full page from a
    truncated one without a second query.
    """
    statement = sqlglot.parse_one(sql, read=dialect)
    if not isinstance(statement, ALLOWED_ROOTS):  # pragma: no cover - see validator
        # The same tuple validation allows, so the two cannot disagree about
        # what a runnable statement is. Narrowing here is also what gives this
        # function both `.args` (from Expression) and `.limit()` (from Query) --
        # sqlglot splits them across two bases and neither has both.
        raise ExecutionError("the query could not be re-parsed to apply a row limit")

    existing = _existing_limit(statement)
    if existing is None:
        effective, server_capped = max_rows, True
    else:
        effective = min(existing, max_rows)
        server_capped = existing > max_rows

    bounded = statement.limit(effective + 1)
    return bounded.sql(dialect=dialect), effective, server_capped


def _existing_limit(statement: exp.Expression) -> int | None:
    """The query's own `LIMIT`, if it is a plain integer.

    A non-literal limit -- a parameter, or an expression -- is reported as
    absent, so the ceiling applies. Guessing at a value that is not knowable
    statically would be the one way to end up applying a limit larger than the
    ceiling.
    """
    node = statement.args.get("limit")
    if not isinstance(node, exp.Limit):
        return None

    expression = node.expression
    if not isinstance(expression, exp.Literal) or not expression.is_int:
        return None

    try:
        value = int(expression.name)
    except ValueError:  # pragma: no cover - is_int already guarantees this
        return None

    return value if value >= 0 else None


def _translate(exc: psycopg.Error) -> ExecutionError:
    """Driver failure to a domain error the agent can branch on."""
    sqlstate = getattr(exc, "sqlstate", None) or ""
    diag = getattr(exc, "diag", None)
    message = getattr(diag, "message_primary", None) or "the query could not be executed"

    if sqlstate == "57014":
        # A resource signal, not a correctness one. Retrying the same query
        # unchanged will time out again, so the agent must narrow it instead.
        return StatementTimeoutError(str(message))
    if sqlstate in {"42501", "25006"}:
        # 25006 is "read-only sql transaction" -- the role refusing a write.
        return PermissionDeniedError(str(message))
    return ExecutionError(str(message))


class AuditLog:
    """Append-only record of every query attempt.

    Runs as the **owner**, on a connection separate from the one executing the
    query. Two consequences, both deliberate: generated SQL cannot reach the
    trail that records it, and a rolled-back query still leaves a row behind.

    **Result values are never stored.** Only shapes and outcomes. Writing rows
    here would copy the protected data into a second store and defeat the point
    of bounding what the read-only role can reach -- see the comment on the
    table in migration 001.
    """

    def __init__(self, connection: Connection[Any], *, db_role: str | None = None) -> None:
        self._conn = connection
        self._db_role = db_role

    def record(
        self,
        *,
        outcome: str,
        sql: str,
        error_type: str | None = None,
        duration_ms: float | None = None,
        row_count: int | None = None,
        truncated: bool | None = None,
        question: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        validation_attempts: int = 1,
    ) -> None:
        """Write one audit row.

        A failure here is logged rather than raised. The trade is deliberate
        and worth stating: a transient problem writing to ``agent_meta`` would
        otherwise fail every read the system serves. The compensating control
        is that the error log carries the same fields, so the record survives
        in a second place. Stage 6 should alert on it -- an audit gap that
        nobody is told about is the same as no audit.
        """
        payload = (
            request_id,
            trace_id,
            session_id,
            question,
            sql,
            validation_attempts,
            self._db_role,
            int(duration_ms) if duration_ms is not None else None,
            row_count,
            truncated,
            outcome,
            error_type,
        )
        try:
            self._conn.execute(_AUDIT_SQL, payload)
        except psycopg.Error:
            logger.exception(
                "audit write failed; recording the entry in the log instead",
                extra={
                    "outcome": outcome,
                    "error_type": error_type,
                    "request_id": request_id,
                    "session_id": session_id,
                    "row_count": row_count,
                    "duration_ms": duration_ms,
                },
            )


class SingleConnectionSource:
    """A `ConnectionSource` backed by one connection.

    For tests and for the single-threaded path that exists today. It hands the
    same connection out every time and never closes it, which is exactly what a
    pool must not do -- hence the separate type rather than a flag.
    """

    def __init__(self, connection: Connection[Any]) -> None:
        self._conn = connection

    def connection(self) -> AbstractContextManager[Connection[Any]]:
        return _Borrowed(self._conn)


class _Borrowed(AbstractContextManager[Connection[Any]]):
    def __init__(self, conn: Connection[Any]) -> None:
        self._conn = conn

    def __enter__(self) -> Connection[Any]:
        return self._conn

    def __exit__(self, *exc_info: object) -> None:
        return None


def rows_as_dicts(result: QueryResult) -> list[dict[str, Any]]:
    """Convenience for serializing a result. Not used on the hot path."""
    return [dict(zip(result.columns, row, strict=True)) for row in result.rows]


__all__ = [
    "AuditLog",
    "ConnectionSource",
    "QueryResult",
    "SQLExecutor",
    "SingleConnectionSource",
    "apply_row_limit",
    "rows_as_dicts",
]
