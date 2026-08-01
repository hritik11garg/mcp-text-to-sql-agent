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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import Connection

from adapters.embedding.factory import build_embedder
from core.dsn import libpq_dsn, redact_dsn
from core.exceptions import ConfigurationError
from core.settings import Settings
from schema.catalog import SchemaCatalog, load_catalog
from schema.retrieval import SchemaRetriever, build_retriever

logger = logging.getLogger(__name__)


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
        """The ``SELECT``-only connection. The containment boundary."""
        if self._readonly is None:
            self._readonly = self._open(self.settings.database.database_ro_url, "DATABASE_RO_URL")
        return self._readonly

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


__all__ = ["Resources"]
