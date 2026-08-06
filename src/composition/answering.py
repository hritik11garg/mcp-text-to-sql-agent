"""Building the answering path for a serving process.

Separate from :mod:`composition.resources` because that module is about
*connections* -- what is open, in what role, proved to be what it claims. This
one is about the components those connections are handed to, and the two have
different reasons to change.

The `_RetrieverContext` adapter below is the only interesting piece, and it is
three lines because it should be. :class:`~answering.ContextSource` asks for
``context(question)``; :class:`~schema.retrieval.SchemaRetriever` offers
``search(question, k=...)``. Adapting at the composition root rather than
renaming either is deliberate: the retriever's ``k`` is a real parameter that
the MCP server and the eval both pass, and the protocol deliberately has no
``k`` because the ``full-schema`` baseline has nothing to put in it.
"""

from __future__ import annotations

from dataclasses import dataclass

from adapters.llm.factory import build_llm_client
from answering import QuestionAnswerer
from composition.resources import Resources
from execution.executor import AuditLog, PoolConnectionSource, SQLExecutor
from generation.generator import SQLGenerator
from schema.retrieval import RetrievalResult, SchemaRetriever
from validation.validator import SQLValidator


@dataclass(frozen=True, slots=True)
class _RetrieverContext:
    """Adapts a retriever to the :class:`~answering.ContextSource` protocol."""

    retriever: SchemaRetriever
    top_k: int | None = None

    def context(self, question: str) -> RetrievalResult:
        return self.retriever.search(question, k=self.top_k)


def build_answerer(resources: Resources) -> QuestionAnswerer:
    """Retrieve-then-generate, over the configured retriever and provider.

    ``top_k`` is left to the retriever's own default rather than passed here.
    That default is ``RETRIEVAL_TOP_K``, and a second place to set it is a
    second place for the API and the eval to disagree -- which is worth 30
    accuracy points, measured, and is the reason `src/answering/` exists at all.
    """
    return QuestionAnswerer(
        _RetrieverContext(resources.retriever),
        SQLGenerator(
            build_llm_client(resources.settings.llm),
            max_rows=resources.settings.execution.max_rows_default,
        ),
    )


def build_executor(resources: Resources) -> SQLExecutor:
    """The executor a *serving* process needs: pooled, validated, audited.

    Three connections are in play and none of them is interchangeable.

    - The **pool** runs the query. Read-only, one connection per concurrent
      request, each one proved by ``assert_read_only`` as the pool opens it.
    - The **validator** holds a single read-only connection for ``EXPLAIN``.
      Planning is cheap and serialising it costs less than a second pool; if
      that stops being true it is a pool, not a redesign.
    - The **audit** runs as owner, on its own connection, so that generated SQL
      cannot reach the trail recording it and a rolled-back query still leaves
      a row.

    ``db_role`` comes from settings rather than being left to default. It was
    left to default in the first draft, and the first real request wrote an
    audit row whose "which privilege ran this" column was empty -- the one
    field that makes the trail answer the question an incident asks of it. The
    ``execute_sql`` server passes a hardcoded ``"sql_agent_ro"``; this reads
    ``DB_READONLY_ROLE``, which is the same value where the default is unchanged
    and the correct one where an operator renamed the role.
    """
    return SQLExecutor(
        PoolConnectionSource(resources.readonly_pool),
        SQLValidator(resources.readonly, resources.catalog, resources.settings.execution),
        resources.settings.execution,
        audit=AuditLog(resources.owner, db_role=resources.settings.database.db_readonly_role),
    )


__all__ = ["build_answerer", "build_executor"]
