"""Make the schema_elements uniqueness constraint work for table rows

``UNIQUE (dataset, table_name, column_name, model_version)`` from migration 001
does not do what it was written to do.

A *table* element has no column, so ``column_name`` is NULL. Under the SQL
standard -- and PostgreSQL's default -- two NULLs are never equal, so two rows
with identical ``(dataset, table_name, NULL, model_version)`` do **not**
conflict. The constraint silently allows unlimited duplicate table rows, and
``INSERT ... ON CONFLICT`` never fires for them, so a re-index appends instead
of updating. Retrieval would then return the same table N times and crowd real
candidates out of top-k.

Column rows were always fine; only table rows were affected. The bug was latent
because nothing wrote to this table until now.

``NULLS NOT DISTINCT`` (PostgreSQL 15+, and this project targets 16) makes NULL
compare equal for uniqueness, which is the behaviour the original constraint
was assumed to have.

Revision ID: 003
Revises: 002
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "schema_elements_unique"
COLUMNS = "(dataset, table_name, column_name, model_version)"


def upgrade() -> None:
    # Any duplicate table rows written before this point would block the new
    # constraint. Nothing populates the catalog yet, so there are none -- but
    # dropping first and adding second means the failure, if it ever happens,
    # is an explicit constraint violation rather than a silent skip.
    op.execute(f"ALTER TABLE agent_meta.schema_elements DROP CONSTRAINT {CONSTRAINT}")
    op.execute(
        f"ALTER TABLE agent_meta.schema_elements "
        f"ADD CONSTRAINT {CONSTRAINT} UNIQUE NULLS NOT DISTINCT {COLUMNS}"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE agent_meta.schema_elements DROP CONSTRAINT {CONSTRAINT}")
    op.execute(
        f"ALTER TABLE agent_meta.schema_elements ADD CONSTRAINT {CONSTRAINT} UNIQUE {COLUMNS}"
    )
