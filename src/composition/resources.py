"""Connections and components, built once per process and injected.

Each server is a long-lived process, so its dependencies are constructed at
startup and held. Two consequences worth stating.

**Failures happen at launch, not on the first call.** A missing
``DATABASE_URL`` or an unreachable database kills the process while the host is
starting it, which is where an operator is looking. Discovering it on the first
``tools/call`` instead means a tool error the agent tries to correct its way
out of, and it cannot.

**Both roles are opened where a server needs both, and they are not
interchangeable.** ``agent_meta`` is invisible to the read-only role, so the
catalog and the audit trail need the owner connection; everything that touches
*target* data uses the read-only one. A server that used the owner connection
for both would still work, and would have quietly removed the boundary that
every security claim in this project rests on.

**And that boundary is now proved rather than assumed.** Every containment
claim in docs/operations/SECURITY.md rests on ``DATABASE_RO_URL`` naming a role
that cannot write, and until :func:`assert_read_only` nothing checked. The
settings-level check compares the two DSN *strings*, which is not the same
question: ``postgresql://postgres@localhost/db`` and
``postgresql://postgres@127.0.0.1/db`` are different strings and the same
superuser. See that function for what is verified and why it is verified this
way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import Connection
from psycopg_pool import ConnectionPool

from adapters.embedding.factory import build_embedder
from core.dsn import libpq_dsn, redact_dsn
from core.exceptions import ConfigurationError
from core.settings import Settings
from schema.catalog import SchemaCatalog, load_catalog
from schema.retrieval import SchemaRetriever, build_retriever

logger = logging.getLogger(__name__)

_POOL_OPEN_TIMEOUT_SECONDS = 30.0
"""How long startup waits for the pool's first connection.

Bounded so a wedged database fails the process rather than hanging it. A
deployment that never finishes starting looks identical to one that is slow,
and only one of them will recover."""


@dataclass(slots=True)
class Resources:
    """Everything a server may need, opened on demand and cached.

    Lazy per *dependency* rather than lazy overall: a server calls only the
    accessors it needs, so ``validate_sql`` never opens an owner connection it
    would not use, and ``schema_search`` never touches the target schema. The
    caller triggers construction at startup by asking for what it needs -- see
    each server's ``build()``.
    """

    settings: Settings
    _owner: Connection[Any] | None = None
    _readonly: Connection[Any] | None = None
    _readonly_pool: ConnectionPool[Connection[Any]] | None = None
    _catalog: SchemaCatalog | None = None
    _retriever: SchemaRetriever | None = None

    @property
    def owner(self) -> Connection[Any]:
        """The owner connection. Reaches ``agent_meta``; can write."""
        if self._owner is None:
            self._owner = self._open(self.settings.database.database_url, "DATABASE_URL")
        return self._owner

    @property
    def readonly(self) -> Connection[Any]:
        """The ``SELECT``-only connection. The containment boundary.

        The assertion runs here, on first open, rather than in each
        entrypoint's startup sequence. Four MCP servers and an HTTP API is five
        places to remember, and the one that forgets is the one that ships. A
        component that holds this connection has, by construction, proved it.
        """
        if self._readonly is None:
            conn = self._open(self.settings.database.database_ro_url, "DATABASE_RO_URL")
            try:
                assert_read_only(conn)
                self._scope_schema(conn)
            except Exception:
                conn.close()
                raise
            self._readonly = conn
        return self._readonly

    def _scope_schema(self, conn: Connection[Any]) -> None:
        """Point a read-only session at the schema the catalog describes.

        Session-scoped (``false``), not transaction-scoped: the validator opens
        its own transaction, so a transaction-local setting would be reverted
        before the ``EXPLAIN`` inside it ran. Set as a bound parameter rather
        than composed into the statement -- it is configuration, but it is
        configuration that reaches SQL, and that is the category this project
        does not interpolate.

        Applied to every read-only session, pooled or not, because the
        validator holds one connection and the executor borrows another. A
        search path on only one of them means a query that validates against a
        schema it will not execute against.
        """
        conn.execute(
            "SELECT set_config('search_path', %s, false)",
            (self.settings.database.db_target_schema,),
        )

    @property
    def readonly_pool(self) -> ConnectionPool[Connection[Any]]:
        """A pool of ``SELECT``-only connections, for concurrent callers.

        The single :attr:`readonly` connection above is correct for an MCP
        server, which answers one ``tools/call`` at a time in one process. It
        is *wrong* the moment two HTTP requests arrive together: psycopg
        connections are not safe to share across concurrent use, and two
        requests would interleave on one session -- including the
        ``set_config`` calls that carry the statement timeout and the
        ``search_path``. That is not a slowdown, it is one request running
        under another's limits.

        **Every connection is asserted, not just the first.** ``configure``
        runs :func:`assert_read_only` on each new connection the pool opens.
        Asserting once at pool creation would be cheaper and would check the
        boundary on one connection while serving traffic on eight; the whole
        argument of ADR-033 is that the connection a request actually uses is
        the one that has to be proved. Three catalog queries against a bounded
        pool is not a cost worth trading that for.

        ``open=False`` then an explicit ``open(wait=True)``: a pool that opens
        in the background reports success before it has proved anything, so a
        deployment whose read-only role can write would start, serve, and fail
        on a request instead of at startup.
        """
        if self._readonly_pool is None:
            url = self.settings.database.database_ro_url
            pool: ConnectionPool[Connection[Any]] = ConnectionPool(
                conninfo=libpq_dsn(url),
                min_size=self.settings.api.api_pool_min_size,
                max_size=self.settings.api.api_pool_max_size,
                # Autocommit, for two reasons that happen to point the same way.
                #
                # `configure` must return the connection with no transaction
                # open -- psycopg_pool discards one that is `INTRANS` and
                # retries, so a pool whose configure function runs any query
                # never finishes opening. `assert_read_only` runs three.
                #
                # And it is what a borrowed connection should look like anyway.
                # Without it every checkout leaves an idle transaction behind
                # after the executor's `conn.transaction()` block, and the
                # read-only role carries an `idle_in_transaction_session_timeout`
                # that would then start killing pooled connections.
                kwargs={"autocommit": True},
                configure=self._prepare_pooled,
                open=False,
                timeout=max(1.0, self.settings.database.db_connect_timeout_ms / 1000),
                name="readonly",
            )
            try:
                pool.open(wait=True, timeout=_POOL_OPEN_TIMEOUT_SECONDS)
            except Exception as exc:
                pool.close()
                raise ConfigurationError(
                    f"could not open the read-only connection pool using "
                    f"DATABASE_RO_URL: {redact_dsn(str(exc)).strip()}"
                ) from None
            self._readonly_pool = pool
        return self._readonly_pool

    def _prepare_pooled(self, conn: Connection[Any]) -> None:
        """Every connection the pool opens: proved, then scoped.

        The proof is per connection rather than per pool on purpose -- ADR-033
        is about the connection a request actually uses, and a pool serving
        eight while one was checked is exactly the gap it closes.
        """
        assert_read_only(conn)
        self._scope_schema(conn)

    def _open(self, url: object, variable: str) -> Connection[Any]:
        return _connect(url, variable, timeout_ms=self.settings.database.db_connect_timeout_ms)

    @property
    def catalog(self) -> SchemaCatalog:
        """The identifier allowlist, read once at startup.

        A point-in-time snapshot, deliberately: it is consulted on every
        validation and every profile, and re-reading it per call would add a
        round trip to the hot path to track a schema that changes at migration
        frequency. A migration to the target database invalidates it until the
        server restarts, which is the same lifetime the indexer already has.
        """
        if self._catalog is None:
            self._catalog = load_catalog(self.owner, dataset=self.settings.retrieval.dataset)
            if self._catalog.is_empty:
                raise ConfigurationError(
                    f"no catalog rows for dataset {self.settings.retrieval.dataset!r}. "
                    f"Run the indexer before starting the MCP servers -- every tool that "
                    f"resolves an identifier would otherwise reject every identifier."
                )
            logger.info("catalog loaded: %d tables", len(self._catalog.tables))
        return self._catalog

    @property
    def retriever(self) -> SchemaRetriever:
        if self._retriever is None:
            embedder = build_embedder(self.settings.retrieval)
            self._retriever = build_retriever(self.owner, embedder, self.settings.retrieval)
        return self._retriever

    def close(self) -> None:
        # The pool first: it holds connections of its own, and closing the
        # single connections underneath it would not release them.
        if self._readonly_pool is not None:
            self._readonly_pool.close()
        for conn in (self._owner, self._readonly):
            if conn is not None:
                conn.close()


def _connect(url: object, variable: str, *, timeout_ms: int) -> Connection[Any]:
    """Open a connection, failing loudly and promptly on bad configuration.

    ``connect_timeout`` is applied because the default is *no timeout*: a
    server pointed at an unreachable host would otherwise hang at startup
    until the OS gave up on the TCP connection, and an MCP host launching it
    sees a subprocess that neither responds nor exits. ``DB_CONNECT_TIMEOUT_MS``
    has existed in settings since Stage 0 and was not being passed to anything.

    ``autocommit=True`` because every server here either reads or manages its
    own transaction explicitly -- the executor and the profiler both open one
    to scope ``SET LOCAL``, and an outer implicit transaction left open across
    calls would hold a snapshot for the life of the process.
    """
    if url is None:
        raise ConfigurationError(f"{variable} is required to run this MCP server")

    # `.env.example` ships DATABASE_URL in SQLAlchemy's `postgresql+psycopg://`
    # form because alembic needs it, and psycopg rejects that string outright.
    # Passing the raw value here meant a server following the shipped example
    # could not open its owner connection at all.
    dsn = libpq_dsn(url)
    try:
        # libpq takes whole seconds and treats 0 as "wait forever", so a
        # sub-second configured timeout must round up rather than down.
        return psycopg.connect(dsn, autocommit=True, connect_timeout=max(1, timeout_ms // 1000))
    except psycopg.Error as exc:
        # psycopg quotes the whole DSN back, password included. Re-raised as a
        # configuration error with the credential removed, because this is
        # startup and the message goes somewhere an operator is reading.
        raise ConfigurationError(
            f"could not connect using {variable}: {redact_dsn(str(exc)).strip()}"
        ) from None


# --- the containment boundary, verified ------------------------------------

_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
"""Excluded from both checks below: PostgreSQL grants the world read access to
these and nothing this project does can or should change that."""

_MAX_REPORTED = 5
"""How many offending objects an error names.

Enough to make the misconfiguration obvious, few enough that the message stays
a message. A role granted INSERT on a whole schema would otherwise produce an
error the length of the schema.
"""

_SYSTEM_PREFIX = "^pg_"
"""Also excluded: every ``pg_toast*`` and ``pg_temp*``.

A session's own temp schema is writable by definition and is not a breach of
the boundary -- it is invisible to every other session and gone when this one
ends.

Passed as a *parameter* rather than interpolated, so both queries below are
literal strings with no substitution in them at all. Nothing here is
caller-controlled either way, but a module that composes SQL by formatting is a
module the next reader has to check, and every SQL string in this project that
looks safe by construction should be safe by construction.
"""

_WRITABLE_RELATIONS = """
    SELECT n.nspname, c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND n.nspname <> ALL(%s)
      AND n.nspname !~ %s
      AND (
        has_table_privilege(c.oid, 'INSERT')
        OR has_table_privilege(c.oid, 'UPDATE')
        OR has_table_privilege(c.oid, 'DELETE')
        OR has_table_privilege(c.oid, 'TRUNCATE')
      )
    ORDER BY n.nspname, c.relname
    LIMIT %s
"""

_CREATABLE_SCHEMAS = """
    SELECT n.nspname
    FROM pg_namespace n
    WHERE n.nspname <> ALL(%s)
      AND n.nspname !~ %s
      AND has_schema_privilege(n.nspname, 'CREATE')
    ORDER BY n.nspname
    LIMIT %s
"""

_ROLE_ATTRIBUTES = """
    SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls
    FROM pg_roles
    WHERE rolname = current_user
"""


def assert_read_only(connection: Connection[Any]) -> None:
    """Prove this connection cannot write, and refuse to start if it can.

    Asks PostgreSQL what the current role *would* be permitted to do rather
    than attempting a write and checking that it failed. Three reasons, and the
    first is the one that matters:

    1. **A probe that succeeds has already done the damage.** The failure mode
       this guards against is "the read-only URL points at a writable role" --
       exactly the case where a test INSERT would be accepted. A check whose
       negative result is a mutation of the operator's database is not a check
       worth running at startup.
    2. It needs no table to aim at. An empty target schema would make a write
       probe pass for the wrong reason.
    3. ``has_table_privilege`` accounts for role inheritance, ``PUBLIC``
       grants, column-level grants and superuser bypass in one answer, which is
       four rules this code would otherwise have to reimplement and keep
       correct across PostgreSQL versions.

    A superuser is caught twice over -- the privilege functions return true for
    everything it asks about, and ``rolsuper`` is reported explicitly so the
    error says *why* rather than listing every table in the database.

    There is deliberately no setting to skip this. A deployment that needs the
    model's generated SQL to run as a role which can write is not a deployment
    this project's threat model describes, and an environment variable that
    turned the boundary off would be found by the first person tired of reading
    the error.

    Raises:
        ConfigurationError: if the role holds any write privilege, may create
            objects, or carries a role attribute that bypasses grants entirely.
    """
    with connection.cursor() as cur:
        cur.execute(_ROLE_ATTRIBUTES)
        attributes = cur.fetchone()
        if attributes is not None:
            _reject_role_attributes(attributes)

        excluded = (list(_SYSTEM_SCHEMAS), _SYSTEM_PREFIX)

        cur.execute(_WRITABLE_RELATIONS, (*excluded, _MAX_REPORTED))
        writable = [f"{schema}.{table}" for schema, table in cur.fetchall()]

        cur.execute(_CREATABLE_SCHEMAS, (*excluded, _MAX_REPORTED))
        creatable = [str(row[0]) for row in cur.fetchall()]

        cur.execute("SELECT current_setting('default_transaction_read_only')")
        setting = cur.fetchone()
        second_barrier = str(setting[0]) if setting else "unknown"

    if writable:
        raise ConfigurationError(
            f"DATABASE_RO_URL can write. The role it connects as holds "
            f"INSERT, UPDATE, DELETE or TRUNCATE on: {', '.join(writable)}. "
            f"Generated SQL runs under this role, so it is the boundary that "
            f"makes every other control a defence in depth rather than the "
            f"only one. Point DATABASE_RO_URL at the login role migration 002 "
            f"creates, or revoke the grants."
        )

    if creatable:
        raise ConfigurationError(
            f"DATABASE_RO_URL can create objects in: {', '.join(creatable)}. "
            f"CREATE is a write: a role that can add a table can add a "
            f"trigger, and the read-only guarantee is only as good as the "
            f"objects it cannot introduce. REVOKE CREATE ON SCHEMA from this "
            f"role, including the default grant PostgreSQL gives PUBLIC on "
            f"'public' in versions before 15."
        )

    # Migration 002 sets `default_transaction_read_only` on the role as a
    # second, independent barrier and says "both must fail for a write to
    # land". Reported rather than required: with the grants above verified
    # absent, a write cannot land whatever this says, so failing on it would
    # reject a correctly configured deployment that reached the same place by
    # another route. Reported at all because a claim of two barriers should not
    # rest on a startup check that only ever looked at one.
    logger.info(
        "read-only role verified: no write privilege on any user schema "
        "(default_transaction_read_only=%s)",
        second_barrier,
    )


def _reject_role_attributes(row: tuple[Any, ...]) -> None:
    """Fail on attributes that make the grant checks meaningless.

    Each of these is a bypass rather than a permission, so finding one means
    the privilege queries above would answer about a rule the role does not
    have to obey. Named individually because "your role is too powerful" sends
    an operator looking in the wrong place.
    """
    superuser, createdb, createrole, bypassrls = row
    problems = [
        name
        for name, held in (
            ("SUPERUSER (bypasses every grant, including all of the above)", superuser),
            ("CREATEDB", createdb),
            ("CREATEROLE (can grant itself anything)", createrole),
            ("BYPASSRLS (defeats row-level security)", bypassrls),
        )
        if held
    ]
    if problems:
        raise ConfigurationError(
            f"DATABASE_RO_URL connects as a role with: {'; '.join(problems)}. "
            f"The read-only boundary is enforced by grants, and these "
            f"attributes are exemptions from grants."
        )


__all__ = ["Resources", "assert_read_only"]
