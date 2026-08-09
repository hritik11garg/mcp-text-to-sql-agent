"""SQL validation: five stages, cheapest first, stop at the first failure.

Contract in docs/architecture/MCP.md section 3.2. This tier exists to be safe
to call in a loop -- the self-correction path may validate three or four
candidates per question, so nothing here may have a side effect and nothing
here may be expensive.

**This is not the security boundary.** The read-only role is
(docs/operations/SECURITY.md section 5). A parser is a poor place to enforce
containment: it has to model every construct the database understands, and the
one it models wrongly is the one that gets through. What this tier buys is a
*fast, specific error* the agent can act on, and a second independent barrier
in front of a boundary that already holds on its own.

Read the two together: validation rejects `DELETE` because it is not a
`SELECT`; the database would refuse it anyway because the role cannot write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import psycopg
import sqlglot
from psycopg import Connection
from sqlglot import exp
from sqlglot.errors import SqlglotError

from core.settings import ExecutionSettings
from schema.catalog import SchemaCatalog

logger = logging.getLogger(__name__)

DIALECT = "postgres"

MAX_SQL_CHARS = 100_000
"""Refuse absurdly long input before parsing it.

The parser is the first thing that touches caller-supplied text, and building
an AST is superlinear in pathological cases. A megabyte of nested parentheses
is a denial-of-service primitive, not a query.
"""

EXPLAIN_PREFIX = "EXPLAIN (FORMAT JSON) "
"""The only thing this module ever executes against the target database.

A named constant rather than an inline f-string so that "validation never runs
the query" is a one-line assertion in the security suite instead of a promise
in a docstring. `ANALYZE` must never appear here: it executes the statement,
and it would do so silently, because the results are discarded either way.
"""

ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
"""Statement types that can be read-only at the top level.

Set operations are included because ``SELECT ... UNION SELECT ...`` parses with
a ``Union`` root, not a ``Select`` one.

Public because the executor narrows to the same tuple before injecting a row
limit. Two lists of "shapes we accept" would drift, and the direction they
would drift in is the executor accepting something validation rejects.
"""

_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Copy,
    exp.Command,
)
"""Nodes that may not appear **anywhere** in the tree, root or not.

``exp.Command`` is the important one and the least obvious: sqlglot parses
anything it does not model into it, so `VACUUM`, `CALL`, `DO $$ ... $$` and
every construct added in a future PostgreSQL version land there. Treating an
opaque node as acceptable would mean trusting a parser precisely where it
admits it does not understand the input.
"""


class ValidationStage(StrEnum):
    PARSE = "parse"
    SINGLE_STATEMENT = "single_statement"
    READ_ONLY = "read_only"
    IDENTIFIERS = "identifiers"
    EXPLAIN = "explain"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The outcome of one validation attempt.

    Carries enough for the agent to *correct itself* rather than merely retry:
    which stage failed, which identifier was at fault, and the nearest real
    name. A bare "invalid" would cost a full generation round trip to learn
    nothing.
    """

    valid: bool
    sql: str
    stage_failed: ValidationStage | None = None
    error_type: str | None = None
    message: str | None = None
    identifier: str | None = None
    suggestion: str | None = None
    estimated_cost: float | None = None
    plan_summary: str | None = None
    tables: tuple[str, ...] = ()

    @property
    def feedback(self) -> str:
        """One line to hand back to the model on a failed attempt."""
        if self.valid:
            return "valid"
        parts = [self.message or "validation failed"]
        if self.suggestion:
            parts.append(f"Nearest match: {self.suggestion}")
        return " ".join(parts)


class SQLValidator:
    """Parses, checks and `EXPLAIN`s a candidate query without running it.

    Args:
        connection: The **read-only** connection. `EXPLAIN` is planning, not
            execution, but it is still the target database and there is no
            reason to touch it with more privilege than a `SELECT` needs.
        catalog: Namespace for identifier resolution.
        execution: Supplies the cost ceiling and the statement timeout.
    """

    def __init__(
        self,
        connection: Connection[Any],
        catalog: SchemaCatalog,
        execution: ExecutionSettings,
        *,
        dialect: str = DIALECT,
    ) -> None:
        self._conn = connection
        self._catalog = catalog
        self._execution = execution
        self._dialect = dialect

    def validate(self, sql: str) -> ValidationResult:
        """Run all five stages in order, returning at the first failure."""
        static = self.validate_static(sql)
        if not static.valid:
            return static
        return self._explain(sql, sql.strip(), static.tables)

    def validate_static(self, sql: str) -> ValidationResult:
        """Stages 1-4: everything decidable from the text and the catalog.

        Separated from `EXPLAIN` because these stages perform **no I/O at all**,
        which is worth being able to state and to test. It also means a caller
        can still reject obviously bad SQL when the database is unreachable,
        rather than losing validation entirely with it.
        """
        text = sql.strip()
        if not text:
            return _fail(sql, ValidationStage.PARSE, "syntax_error", "the query is empty")
        if len(text) > MAX_SQL_CHARS:
            return _fail(
                sql,
                ValidationStage.PARSE,
                "syntax_error",
                f"the query is {len(text)} characters, over the {MAX_SQL_CHARS} limit",
            )

        try:
            statements = sqlglot.parse(text, read=self._dialect)
        except SqlglotError as exc:
            # The **base** class, not `ParseError`. `TokenError` is its sibling,
            # not its subclass, and it is what sqlglot raises for an
            # unterminated string, identifier, comment or dollar-quote -- which
            # is precisely the shape of a generation that ran out of output
            # tokens mid-literal. `... WHERE name = 'Rob` is not an exotic
            # input on a free tier with a low token cap; it is the ordinary
            # failure of the provider this project defaults to.
            #
            # Catching the leaf meant that input escaped as an unhandled
            # exception: the caller got `internal_error` instead of an
            # actionable `syntax_error`, the self-correction loop aborted the
            # request rather than correcting it, and -- worst -- the executor
            # raised before reaching its `outcome="rejected"` audit write, so
            # the attempt left no trail. Found by
            # tests/security/test_property_write_containment.py on its first
            # run; no example-based test had thought to end a string early.
            return _fail(sql, ValidationStage.PARSE, "syntax_error", _first_line(str(exc)))

        parsed = [s for s in statements if s is not None]
        if len(parsed) != 1:
            return _fail(
                sql,
                ValidationStage.SINGLE_STATEMENT,
                "multiple_statements",
                f"expected exactly one statement, found {len(parsed)}. "
                f"Stacked statements are never accepted.",
            )

        statement = parsed[0]
        if not isinstance(statement, exp.Expression):
            # sqlglot.parse is typed as returning the wider Expr base. Anything
            # that is not a full Expression cannot be inspected for a read-only
            # guarantee, and refusing beats filtering it out -- dropping it
            # would change the statement count and let a stacked query through.
            return _fail(
                sql,
                ValidationStage.PARSE,
                "syntax_error",
                "the parser produced a tree this validator cannot inspect",
            )

        read_only_failure = self._check_read_only(sql, statement)
        if read_only_failure is not None:
            return read_only_failure

        tables = _referenced_tables(statement)

        identifier_failure = self._check_identifiers(sql, statement, tables)
        if identifier_failure is not None:
            return identifier_failure

        return ValidationResult(valid=True, sql=sql, tables=tables)

    # --- stage 3: read-only -------------------------------------------------

    def _check_read_only(self, sql: str, statement: exp.Expression) -> ValidationResult | None:
        """Reject anything that could change state.

        The root check alone is **not sufficient**, and that is the whole point
        of this method. PostgreSQL allows data-modifying CTEs, so

            WITH gone AS (DELETE FROM orders RETURNING id) SELECT * FROM gone

        parses with a ``Select`` at the root and deletes every row. The tree
        walk below is what catches it.
        """
        if not isinstance(statement, ALLOWED_ROOTS):
            return _fail(
                sql,
                ValidationStage.READ_ONLY,
                "not_read_only",
                f"only SELECT statements are permitted, got {statement.key.upper()}",
            )

        for node in statement.walk():
            if isinstance(node, _FORBIDDEN_NODES):
                return _fail(
                    sql,
                    ValidationStage.READ_ONLY,
                    "not_read_only",
                    f"{node.key.upper()} is not permitted anywhere in the query, "
                    f"including inside a CTE or subquery",
                )
            if isinstance(node, exp.Lock):
                # FOR UPDATE / FOR SHARE take row locks. Harmless under a
                # read-only transaction, which refuses them -- but refusing
                # here gives the agent a reason instead of a driver error.
                return _fail(
                    sql,
                    ValidationStage.READ_ONLY,
                    "not_read_only",
                    "row locking (FOR UPDATE / FOR SHARE) is not permitted",
                )

        if statement.args.get("into"):
            # SELECT ... INTO creates a table in PostgreSQL. Root is Select and
            # the tree holds no DDL node, so only this check sees it.
            return _fail(
                sql,
                ValidationStage.READ_ONLY,
                "not_read_only",
                "SELECT ... INTO creates a table and is not permitted; use a plain SELECT",
            )

        return None

    # --- stage 4: identifiers ----------------------------------------------

    def _check_identifiers(
        self, sql: str, statement: exp.Expression, tables: tuple[str, ...]
    ) -> ValidationResult | None:
        """Resolve names against the catalog, with a suggestion when close.

        Deliberately biased towards accepting. A false rejection blocks a valid
        query for a reason the agent cannot fix; a false acceptance is caught
        by `EXPLAIN` one stage later, at the cost of one round trip. So this
        only rejects what it is confident about, and skips entirely when the
        catalog is empty.
        """
        if self._catalog.is_empty:
            return None

        local_names = _local_names(statement)

        for table in tables:
            if table.casefold() in local_names or self._catalog.has_table(table):
                continue
            return _fail(
                sql,
                ValidationStage.IDENTIFIERS,
                "table_not_found",
                f'table "{table}" does not exist',
                identifier=table,
                suggestion=self._catalog.suggest_table(table),
            )

        real_tables = [t for t in tables if t.casefold() not in local_names]
        if not real_tables:
            return None

        reachable = self._catalog.columns_of(real_tables) | local_names

        for column in statement.find_all(exp.Column):
            name = column.name
            if not name or name == "*":
                continue
            qualifier = column.table

            if qualifier:
                if qualifier.casefold() in local_names:
                    continue
                target = _resolve_alias(statement, qualifier)
                if not self._catalog.has_table(target):
                    continue
                if self._catalog.has_column(target, name):
                    continue
                return _fail(
                    sql,
                    ValidationStage.IDENTIFIERS,
                    "unknown_identifier",
                    f'column "{name}" does not exist on table "{target}"',
                    identifier=name,
                    suggestion=self._catalog.suggest_column(name, [target]),
                )

            if name.casefold() not in reachable:
                return _fail(
                    sql,
                    ValidationStage.IDENTIFIERS,
                    "unknown_identifier",
                    f'column "{name}" does not exist on any referenced table',
                    identifier=name,
                    suggestion=self._catalog.suggest_column(name, real_tables),
                )

        return None

    # --- stage 5: EXPLAIN ---------------------------------------------------

    def _explain(self, sql: str, text: str, tables: tuple[str, ...]) -> ValidationResult:
        """Ask the planner, without running the query.

        The statement executed is `EXPLAIN_PREFIX` and nothing else -- see the
        constant for why that matters and what must never be added to it.
        """
        timeout_ms = self._execution.clamp_timeout_ms(None)
        try:
            with self._conn.transaction(), self._conn.cursor() as cur:
                cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout_ms),))
                cur.execute(EXPLAIN_PREFIX + text)
                row = cur.fetchone()
        except psycopg.Error as exc:
            return _from_database_error(sql, exc)

        cost, summary = _read_plan(row)

        if cost is not None and cost > self._execution.max_estimated_cost:
            return _fail(
                sql,
                ValidationStage.EXPLAIN,
                "cost_exceeded",
                f"the planner estimates a cost of {cost:,.0f}, over the ceiling of "
                f"{self._execution.max_estimated_cost:,.0f}. Narrow the query.",
                estimated_cost=cost,
                plan_summary=summary,
                tables=tables,
            )

        return ValidationResult(
            valid=True,
            sql=sql,
            estimated_cost=cost,
            plan_summary=summary,
            tables=tables,
        )


# --- helpers ---------------------------------------------------------------


def _fail(
    sql: str,
    stage: ValidationStage,
    error_type: str,
    message: str,
    *,
    identifier: str | None = None,
    suggestion: str | None = None,
    estimated_cost: float | None = None,
    plan_summary: str | None = None,
    tables: tuple[str, ...] = (),
) -> ValidationResult:
    return ValidationResult(
        valid=False,
        sql=sql,
        stage_failed=stage,
        error_type=error_type,
        message=message,
        identifier=identifier,
        suggestion=suggestion,
        estimated_cost=estimated_cost,
        plan_summary=plan_summary,
        tables=tables,
    )


_ERROR_TYPES: dict[str, str] = {
    "42P01": "table_not_found",
    "42703": "unknown_identifier",
    "42501": "permission_denied",
    "57014": "statement_timeout",
    "42601": "syntax_error",
}


def _from_database_error(sql: str, exc: psycopg.Error) -> ValidationResult:
    """Translate a driver error into the agent's error taxonomy.

    Only ``message_primary`` is surfaced. A raw ``str(exc)`` carries the
    statement position, hints, and context lines, and MCP.md section 6 forbids
    driver output in tool errors.
    """
    sqlstate = getattr(exc, "sqlstate", None) or ""
    error_type = _ERROR_TYPES.get(sqlstate, "explain_failed")

    diag = getattr(exc, "diag", None)
    message = getattr(diag, "message_primary", None) or "the planner rejected the query"

    return _fail(sql, ValidationStage.EXPLAIN, error_type, str(message))


def _first_line(text: str) -> str:
    """sqlglot renders parse errors over several lines with a caret diagram.

    The first line names the problem and its position, which is what the model
    can act on; the rest is a rendering of the input it already has.
    """
    line = text.strip().splitlines()[0] if text.strip() else "could not parse the query"
    return line.strip()


def _read_plan(row: tuple[Any, ...] | None) -> tuple[float | None, str | None]:
    """Pull total cost and the top node out of `EXPLAIN (FORMAT JSON)`."""
    if not row or not row[0]:
        return None, None

    payload = row[0]
    if not isinstance(payload, list) or not payload:
        return None, None

    plan = payload[0].get("Plan") if isinstance(payload[0], dict) else None
    if not isinstance(plan, dict):
        return None, None

    cost = plan.get("Total Cost")
    node = plan.get("Node Type")
    return (float(cost) if cost is not None else None, str(node) if node else None)


def _referenced_tables(statement: exp.Expression) -> tuple[str, ...]:
    """Distinct table names named in the query, in order of appearance."""
    seen: dict[str, None] = {}
    for table in statement.find_all(exp.Table):
        if table.name:
            seen.setdefault(table.name, None)
    return tuple(seen)


def _local_names(statement: exp.Expression) -> frozenset[str]:
    """Names defined *by the query itself* rather than by the schema.

    CTE names, derived-table aliases and output column aliases all look like
    identifiers and none of them exist in the catalog. Treating them as unknown
    would reject correct SQL -- the most damaging way for this stage to be
    wrong, since the agent cannot fix a complaint about a name it invented on
    purpose.
    """
    names: set[str] = set()

    for cte in statement.find_all(exp.CTE):
        if cte.alias:
            names.add(cte.alias.casefold())
        # Columns a CTE exposes are not schema columns either.
        for projection in cte.this.expressions if isinstance(cte.this, exp.Select) else []:
            if projection.alias_or_name:
                names.add(projection.alias_or_name.casefold())

    for subquery in statement.find_all(exp.Subquery):
        if subquery.alias:
            names.add(subquery.alias.casefold())

    for alias in statement.find_all(exp.Alias):
        if alias.alias:
            names.add(alias.alias.casefold())

    return frozenset(names)


def _resolve_alias(statement: exp.Expression, qualifier: str) -> str:
    """Map a table alias back to the real table it stands for."""
    for table in statement.find_all(exp.Table):
        if table.alias and table.alias.casefold() == qualifier.casefold():
            return table.name
    return qualifier
