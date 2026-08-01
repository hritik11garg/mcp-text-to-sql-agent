"""Recall@k: did retrieval surface the columns the answer actually needed?

The metric that bounds everything downstream. A column the retriever never
returns cannot appear in correct SQL, so execution accuracy has a ceiling set
here — which is why EVALUATION.md section 1.2 measures it separately rather
than inferring it from end-to-end scores.

The whole problem is the denominator: *which* schema elements did the gold
query need? That means parsing the reference SQL and resolving every column
reference back to its table through whatever aliases the query used. Some
references cannot be resolved — an unqualified column where several tables are
in scope is genuinely ambiguous without a schema to consult.

**Unresolved references are counted and reported, never dropped.** Silently
excluding them computes recall over the easy subset and reports it as recall,
which flatters the number by exactly the amount that is hardest to retrieve.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DEFAULT_K_VALUES: tuple[int, ...] = (1, 5, 10, 20)
"""Reported at each of these, per EVALUATION.md section 1.2."""


@dataclass(frozen=True, slots=True)
class GoldElements:
    """The schema elements a reference query referenced.

    ``tables`` and ``columns`` are what recall is measured against.
    ``unresolved`` is what could not be attributed to a table, and it exists so
    a caller can see the denominator it did not get to use.
    """

    tables: frozenset[str] = frozenset()
    columns: frozenset[tuple[str, str]] = frozenset()
    unresolved: frozenset[str] = frozenset()
    parse_failed: bool = False

    @property
    def is_usable(self) -> bool:
        """Whether this query can contribute to a recall number at all.

        A query that failed to parse, or whose every column is unresolved,
        has no denominator. Scoring it as 0 would be wrong -- the retriever was
        never asked -- and scoring it as 1 would be worse.
        """
        return not self.parse_failed and bool(self.columns or self.tables)


@dataclass(frozen=True, slots=True)
class RecallResult:
    """Recall at each k, plus what it was computed over."""

    at_k: dict[int, float] = field(default_factory=dict)
    gold_size: int = 0
    unresolved_count: int = 0
    skipped: bool = False
    """True when the gold query yielded no usable denominator."""


def extract_gold_elements(gold_sql: str, *, dialect: str = "postgres") -> GoldElements:
    """Every table and column the reference query names.

    Alias resolution is done per query scope: ``FROM orders o JOIN customers c``
    binds ``o`` and ``c``, so ``o.total`` resolves to ``orders.total``. An
    unqualified column resolves only when exactly one table is in scope --
    with two, ``id`` genuinely could be either, and guessing would put a
    fabricated element in the denominator.
    """
    try:
        parsed = sqlglot.parse_one(gold_sql, read=dialect)
    except Exception:
        return GoldElements(parse_failed=True)

    # sqlglot types `parse_one` as returning the wider `Expr` base, which has
    # no `.find_all`. Narrowing by *refusing* rather than casting means an
    # unexpected node type is reported as an unusable gold query instead of
    # crashing the run -- the same pattern the validator uses.
    if not isinstance(parsed, exp.Expression):
        return GoldElements(parse_failed=True)
    statement = parsed

    tables: set[str] = set()
    columns: set[tuple[str, str]] = set()
    unresolved: set[str] = set()

    # CTE names are query-local, not schema objects. Collecting them lets a
    # reference to a CTE be excluded rather than counted as a missing table --
    # the retriever cannot return something the schema does not contain.
    cte_names = {
        cte.alias_or_name.casefold() for cte in statement.find_all(exp.CTE) if cte.alias_or_name
    }

    for scope in _select_scopes(statement):
        aliases = _aliases_in_scope(scope, cte_names)
        tables.update(aliases.values())

        for column in scope.find_all(exp.Column):
            if _belongs_to_inner_scope(column, scope):
                continue
            name = column.name.casefold()
            qualifier = (column.table or "").casefold()

            if qualifier:
                table = aliases.get(qualifier, qualifier)
                if table not in cte_names:
                    columns.add((table, name))
            elif len(set(aliases.values())) == 1:
                columns.add((next(iter(aliases.values())), name))
            else:
                unresolved.add(name)

    return GoldElements(
        tables=frozenset(tables),
        columns=frozenset(columns),
        unresolved=frozenset(unresolved),
    )


def _select_scopes(statement: exp.Expression) -> list[exp.Select]:
    return list(statement.find_all(exp.Select))


def _belongs_to_inner_scope(column: exp.Column, scope: exp.Select) -> bool:
    """Whether this column is really a nested SELECT's, reached by find_all.

    ``find_all`` descends through subqueries, so without this every inner
    column would also be attributed to the outer scope's aliases -- which is
    how a denominator acquires elements the query never referenced there.
    """
    node = column.parent
    while node is not None and node is not scope:
        if isinstance(node, exp.Select):
            return True
        node = node.parent
    return False


def _aliases_in_scope(scope: exp.Select, cte_names: set[str]) -> dict[str, str]:
    """Map every alias visible in one SELECT to the real table it names.

    A table with no alias maps to itself, so ``FROM orders`` still resolves
    ``orders.id``. CTE references are dropped: they are not schema objects and
    including them would ask the retriever for something that does not exist.
    """
    aliases: dict[str, str] = {}

    for table in scope.find_all(exp.Table):
        if _belongs_to_inner_scope_table(table, scope):
            continue
        name = table.name.casefold()
        if not name or name in cte_names:
            continue
        aliases[name] = name
        if table.alias:
            aliases[table.alias.casefold()] = name

    return aliases


def _belongs_to_inner_scope_table(table: exp.Table, scope: exp.Select) -> bool:
    node = table.parent
    while node is not None and node is not scope:
        if isinstance(node, exp.Select):
            return True
        node = node.parent
    return False


def compute_recall(
    gold: GoldElements,
    retrieved: Sequence[tuple[str, str | None]],
    *,
    k_values: Iterable[int] = DEFAULT_K_VALUES,
) -> RecallResult:
    """Fraction of the gold elements present in the retriever's top-k.

    Args:
        gold: What the reference query referenced.
        retrieved: ``(table, column)`` pairs **in rank order**. ``column`` is
            ``None`` for a table-level element.

    Ranking matters: the sequence is truncated at each k, so passing an
    unordered collection produces a number that means nothing.

    A gold column is credited when the retriever returned that column, **or**
    when it returned the table as a table-level element -- retrieving
    ``customers`` puts every one of its columns in the prompt, so counting the
    column as missed would understate what the model was actually shown.
    """
    if not gold.is_usable:
        return RecallResult(skipped=True, unresolved_count=len(gold.unresolved))

    targets = set(gold.columns) | {(table, None) for table in gold.tables}
    at_k: dict[int, float] = {}

    for k in k_values:
        window = retrieved[:k]
        seen_columns = {(t.casefold(), c.casefold()) for t, c in window if c is not None}
        seen_tables = {t.casefold() for t, _ in window}

        found = sum(
            1
            for table, column in targets
            if (column is None and table in seen_tables)
            or (column is not None and ((table, column) in seen_columns or table in seen_tables))
        )
        at_k[k] = round(found / len(targets), 4) if targets else 0.0

    return RecallResult(
        at_k=at_k,
        gold_size=len(targets),
        unresolved_count=len(gold.unresolved),
    )


def aggregate(results: Sequence[RecallResult]) -> dict[str, float | int]:
    """Mean recall at each k across a run, with the skipped count beside it.

    The skipped count is part of the result rather than a footnote: a mean over
    60 of 200 questions is a different claim from a mean over 200, and a table
    row carrying only the first number cannot be told apart from the second.
    """
    scored = [r for r in results if not r.skipped]
    summary: dict[str, float | int] = {
        "questions": len(results),
        "scored": len(scored),
        "skipped": len(results) - len(scored),
        "unresolved_references": sum(r.unresolved_count for r in results),
    }
    if not scored:
        return summary

    for k in sorted({k for r in scored for k in r.at_k}):
        values = [r.at_k[k] for r in scored if k in r.at_k]
        summary[f"recall@{k}"] = round(sum(values) / len(values), 4)
    return summary


__all__ = [
    "DEFAULT_K_VALUES",
    "GoldElements",
    "RecallResult",
    "aggregate",
    "compute_recall",
    "extract_gold_elements",
]
