"""The read-only role -- the outermost containment boundary

This is the layer that holds when prompt framing, AST validation, and EXPLAIN
have all failed. Everything above it reduces how often the boundary is tested;
this is what makes a failure survivable.

Two roles are created:

  sql_agent_ro     NOLOGIN group role carrying the privilege set
  sql_agent_login  LOGIN role, member of the above

Splitting them means the privilege set can be re-granted or audited without
touching credentials, and the password never appears in a GRANT statement.

Verified by negative tests in tests/security/test_readonly_role.py, which pass
only when the database *refuses*. See docs/architecture/DATABASE.md section 7
and docs/operations/SECURITY.md section 5.

Revision ID: 002
Revises: 001
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GROUP_ROLE = "sql_agent_ro"
LOGIN_ROLE = "sql_agent_login"


def _login_password() -> str:
    password = os.environ.get("SQL_AGENT_RO_PASSWORD")
    if not password:
        raise RuntimeError(
            "SQL_AGENT_RO_PASSWORD is not set. The read-only login role needs a "
            "password, and it must not be hardcoded in a migration."
        )
    return password


def upgrade() -> None:
    # --- roles -------------------------------------------------------------
    # CREATE ROLE is not idempotent, and a partially-applied migration must be
    # re-runnable.
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{GROUP_ROLE}') THEN
                CREATE ROLE {GROUP_ROLE} NOLOGIN;
            END IF;
        END $$
    """)

    # The password crosses the wire as a bound parameter to set_config, never
    # interpolated into SQL text; format(%L) then quotes it server-side. DDL
    # cannot take bind parameters directly, and string-formatting a credential
    # into a CREATE ROLE statement would put it in logs and in this file's
    # rendered SQL. The setting is transaction-local, so it does not outlive
    # the migration.
    bind = op.get_bind()
    bind.exec_driver_sql("SELECT set_config('t2sql.ro_password', %s, true)", (_login_password(),))
    op.execute(f"""
        DO $do$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LOGIN_ROLE}') THEN
                EXECUTE format(
                    'CREATE ROLE {LOGIN_ROLE} LOGIN PASSWORD %L',
                    current_setting('t2sql.ro_password')
                );
            ELSE
                EXECUTE format(
                    'ALTER ROLE {LOGIN_ROLE} PASSWORD %L',
                    current_setting('t2sql.ro_password')
                );
            END IF;
        END $do$
    """)

    op.execute(f"GRANT {GROUP_ROLE} TO {LOGIN_ROLE}")

    # --- strip defaults ----------------------------------------------------
    # PUBLIC gets CONNECT on every database and USAGE on public by default.
    op.execute("""
        DO $$
        BEGIN
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
        END $$
    """)
    op.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")

    # --- grant exactly what is needed, and nothing else --------------------
    op.execute(f"""
        DO $$
        BEGIN
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {GROUP_ROLE}', current_database());
        END $$
    """)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {GROUP_ROLE}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {GROUP_ROLE}")

    # A table added later is readable without re-granting. Note what is NOT
    # here: write access never becomes automatic.
    op.execute(f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT SELECT ON TABLES TO {GROUP_ROLE}
    """)

    # agent_meta is granted to nothing. Generated SQL cannot reach the audit
    # log, session history, or the embeddings.

    # --- per-role resource ceilings ---------------------------------------
    # Set on the ROLE, not just per-connection, so a caller that forgets to
    # apply them still gets the ceiling.
    op.execute(f"ALTER ROLE {GROUP_ROLE} SET statement_timeout = '30s'")
    op.execute(f"ALTER ROLE {GROUP_ROLE} SET idle_in_transaction_session_timeout = '10s'")
    op.execute(f"ALTER ROLE {GROUP_ROLE} SET work_mem = '32MB'")
    # Second, independent write barrier alongside the absent grants. Both must
    # fail for a write to land.
    op.execute(f"ALTER ROLE {GROUP_ROLE} SET default_transaction_read_only = on")

    # The login role inherits privileges from the group, but ALTER ROLE ... SET
    # is NOT inherited -- it applies to the role that logs in.
    op.execute(f"ALTER ROLE {LOGIN_ROLE} SET statement_timeout = '30s'")
    op.execute(f"ALTER ROLE {LOGIN_ROLE} SET idle_in_transaction_session_timeout = '10s'")
    op.execute(f"ALTER ROLE {LOGIN_ROLE} SET work_mem = '32MB'")
    op.execute(f"ALTER ROLE {LOGIN_ROLE} SET default_transaction_read_only = on")

    # NOTE on functions: pg_read_file, pg_ls_dir, and COPY ... TO PROGRAM are
    # restricted to superusers and to the pg_read_server_files /
    # pg_execute_server_program predefined roles by default. This migration
    # grants neither role and does not make sql_agent_ro a superuser, so they
    # are unreachable. The negative tests assert that rather than assuming it,
    # because running the application as a superuser would silently undo it.


def downgrade() -> None:
    op.execute(f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            REVOKE SELECT ON TABLES FROM {GROUP_ROLE}
    """)
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {GROUP_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {GROUP_ROLE}")
    op.execute(f"""
        DO $$
        BEGIN
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM {GROUP_ROLE}', current_database());
        END $$
    """)
    op.execute(f"DROP ROLE IF EXISTS {LOGIN_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {GROUP_ROLE}")
