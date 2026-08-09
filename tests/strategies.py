"""SQL-shaped Hypothesis strategies, shared by the property suites.

Here rather than in one test module because two suites need the same SQL: the
security suite asserts that no generated write is ever accepted, and the unit
suite asserts that everything the validator *does* accept can be row-limited.
Those two need the same grammar to be worth anything -- if the executor suite
generated a narrower set of SELECTs than the validator suite, the pair would
prove that two different languages are consistent with each other.

**These are not random-string fuzzers.** A strategy that emits arbitrary text
finds parse errors, which is a fact about ``sqlglot`` and not about this
project. What is generated instead is *plausible* SQL: real statements with the
incidental details varied -- keyword case, whitespace, comments, quoting,
statement nesting -- because those are the details a human writing test cases
holds constant without noticing.

That is the defect this project keeps rediscovering, stated in
docs/project/ENGINEERING_MATRIX.md section 38: **nobody chose easy inputs,
everybody chose convenient ones**, and convenient SQL is lower case with single
spaces and no comments.
"""

from __future__ import annotations

from typing import Final

from hypothesis import strategies as st

TABLES: Final = ("orders", "customers", "public.orders", "agent_meta.query_audit")
"""Table names the generated statements reference.

Includes a schema-qualified name and the audit table on purpose. ``agent_meta``
is the one schema the read-only role must never reach, so a generated
``DELETE FROM agent_meta.query_audit`` is the exact statement whose acceptance
would be worst -- generated SQL erasing the trail that records it.
"""

SEPARATORS: Final = (
    " ",
    "  ",
    "\n",
    "\t",
    "\r\n",
    " /* comment */ ",
    " -- trailing comment\n",
    "\n\n",
)
"""What may sit between two tokens.

The last three matter most. A comment is a place to hide a keyword from a
check that scans text rather than a tree, and the project's read-only stage
walks an AST specifically so that ``DELETE /* not really */ FROM t`` cannot be
smuggled past a substring search. Generating comments is how that claim gets
tested rather than asserted.
"""

CASINGS: Final = (
    str.lower,
    str.upper,
    lambda text: text,
    str.title,
)
"""Applied to the whole statement once it is assembled.

``str.upper`` is the one that earns its place: PostgreSQL folds *unquoted*
identifiers to lower case, so ``DELETE FROM ORDERS`` and ``delete from orders``
name the same table, and a check that compares against a lower-case constant
sees only one of them.
"""


# --- write statements ------------------------------------------------------

WRITE_STATEMENTS: Final = (
    ("INSERT INTO", "{table}", "(id)", "VALUES", "(1)"),
    ("UPDATE", "{table}", "SET", "id", "=", "1"),
    ("DELETE FROM", "{table}"),
    ("DROP TABLE", "{table}"),
    ("CREATE TABLE", "scratch", "(id int)"),
    ("ALTER TABLE", "{table}", "ADD COLUMN", "flag", "boolean"),
    ("TRUNCATE TABLE", "{table}"),
    ("GRANT SELECT ON", "{table}", "TO PUBLIC"),
    ("COPY", "{table}", "TO STDOUT"),
    ("VACUUM", "{table}"),
    ("CALL", "some_procedure()"),
    ("REFRESH MATERIALIZED VIEW", "{table}"),
    ("SET ROLE", "postgres"),
    ("CREATE INDEX", "idx", "ON", "{table}", "(id)"),
    ("COMMENT ON TABLE", "{table}", "IS", "'x'"),
)
"""Statements that change state, as token lists so separators can go between.

Split across the three ways this project can be wrong about a write:

- **Modelled DML** -- ``INSERT``/``UPDATE``/``DELETE`` -- caught by the node
  list, and the easy case.
- **Modelled DDL** -- ``DROP``/``CREATE``/``ALTER``/``TRUNCATE``/``GRANT`` --
  caught the same way, and the reason the node list is not just DML.
- **Unmodelled** -- ``VACUUM``, ``CALL``, ``REFRESH``, ``SET``, ``COMMENT``.
  These parse into ``exp.Command``, the node ``sqlglot`` uses for input it does
  not understand. They are the important ones: a future PostgreSQL statement
  nobody has heard of lands there too, and the validator's rule is that an
  opaque node is refused rather than assumed harmless.
"""

DML_STATEMENTS: Final = WRITE_STATEMENTS[:3]
"""The three that are legal inside a data-modifying CTE.

``WITH gone AS (DELETE FROM orders RETURNING id) SELECT * FROM gone`` parses
with a ``Select`` at the root and deletes every row. Only these three (and
``MERGE``) can appear there, so the CTE property is generated from this
narrower list -- wrapping ``VACUUM`` in a CTE is a parse error, which would
make the test pass for the wrong reason.
"""


# --- read-only statements --------------------------------------------------

SELECT_STATEMENTS: Final = (
    ("SELECT", "*", "FROM", "{table}"),
    ("SELECT", "count(*)", "FROM", "{table}"),
    ("SELECT", "id, id", "FROM", "{table}", "WHERE", "id > 0"),
    ("SELECT", "a.id", "FROM", "{table}", "a", "JOIN", "{table}", "b", "ON", "a.id = b.id"),
    ("WITH", "r", "AS", "(SELECT", "*", "FROM", "{table}", ")", "SELECT", "*", "FROM", "r"),
    ("SELECT", "*", "FROM", "{table}", "UNION", "SELECT", "*", "FROM", "{table}"),
    ("SELECT", "*", "FROM", "{table}", "EXCEPT", "SELECT", "*", "FROM", "{table}"),
    ("SELECT", "*", "FROM", "{table}", "INTERSECT", "SELECT", "*", "FROM", "{table}"),
    ("SELECT", "*", "FROM", "{table}", "WHERE", "id", "IN", "(SELECT", "id", "FROM", "{table})"),
    ("SELECT", "*", "FROM", "(SELECT", "id", "FROM", "{table}", ")", "t"),
    ("SELECT", "id", "FROM", "{table}", "GROUP BY", "id", "HAVING", "count(*) > 1"),
    ("SELECT", "id", "FROM", "{table}", "ORDER BY", "id", "DESC"),
)
"""Statements that must *not* be refused, which is the harder half.

A validator that rejects everything satisfies "no write is ever accepted"
perfectly. The set operations and the CTE are here because they are where an
over-eager read-only check goes wrong: ``UNION`` parses with a ``Union`` root
rather than a ``Select`` one, and a CTE puts a nested query where a naive root
check does not look.

**Two-word keywords are single tokens throughout** -- ``ORDER BY``, not
``ORDER`` then ``BY``. Splitting them let a generated comment land in the
middle, and ``ORDER /* c */ BY id`` is legal PostgreSQL that ``sqlglot``
cannot parse. That is a real false rejection and it is *upstream* of this
project; generating it here would have made this suite fail for somebody
else's reason, every run, until somebody deleted the suite. Recorded in
docs/development/TESTING.md section 12 rather than chased.
"""


# --- assembly --------------------------------------------------------------


@st.composite
def statement(draw: st.DrawFn, templates: tuple[tuple[str, ...], ...]) -> str:
    """One statement from ``templates``, with its incidentals varied.

    The table name is substituted before the case transform, so an upper-cased
    example carries an upper-cased identifier -- which is the case that matters,
    since that is the one a lower-case constant fails to match.
    """
    tokens = draw(st.sampled_from(templates))
    table = draw(st.sampled_from(TABLES))
    casing = draw(st.sampled_from(CASINGS))

    parts: list[str] = []
    for index, token in enumerate(tokens):
        if index:
            parts.append(draw(st.sampled_from(SEPARATORS)))
        parts.append(token.replace("{table}", table))

    return casing("".join(parts))


def writes() -> st.SearchStrategy[str]:
    """Any state-changing statement."""
    return statement(WRITE_STATEMENTS)


def dml() -> st.SearchStrategy[str]:
    """Only the writes that are legal inside a CTE."""
    return statement(DML_STATEMENTS)


def selects() -> st.SearchStrategy[str]:
    """Any statement that is genuinely read-only."""
    return statement(SELECT_STATEMENTS)


__all__ = [
    "CASINGS",
    "DML_STATEMENTS",
    "SELECT_STATEMENTS",
    "SEPARATORS",
    "TABLES",
    "WRITE_STATEMENTS",
    "dml",
    "selects",
    "statement",
    "writes",
]
