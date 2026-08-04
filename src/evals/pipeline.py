"""The answerer seam: the pipeline the harness has been waiting for.

Everything the runner needs is a callable that turns a question into candidate
SQL, and a callable that runs SQL. This module builds both out of components
that already existed -- retrieval, generation, validation -- and it is where
the two facts about a *benchmark* corpus that none of those components knew
about get handled.

**A benchmark is many databases, and every component here is single-schema.**
Spider dev is 20 databases, each converted into its own PostgreSQL schema. The
retriever filters by ``dataset``, the catalog is loaded per ``dataset``, and
the validator holds one catalog. So a question about ``concert_singer`` must be
answered by components scoped to ``concert_singer`` and by nothing else -- a
single catalog spanning all 20 would offer the model 20 databases' worth of
tables and let it join across schemas that share a column name. The scope is
resolved per question and cached; see :class:`ScopeRegistry`.

**The seam is synchronous and generation is not.** The obvious bridge,
``asyncio.run`` per question, is wrong here: the OpenAI-compatible adapter
constructs its async client once and caches it, and that client binds to the
event loop it was first used in. A loop per question leaves the second question
talking to a closed loop. One loop is held for the life of the answerer.

**Gold SQL is not built here.** It arrives already verified, from
:mod:`evals.gold`. See that module for why re-deriving it would undo the
verification.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, Self

from psycopg import Connection

from answering import QuestionAnswerer, retrieved_columns
from core.exceptions import LLMError, RetrievalError
from core.ports.embedder import Embedder
from core.settings import ExecutionSettings, RetrievalSettings
from evals.dataset import Question
from evals.runner import Attempt, Rows
from generation.generator import SQLGenerator, UnanswerableQuestionError
from schema.catalog import SchemaCatalog, load_catalog
from schema.models import ForeignKey
from schema.retrieval import RetrievalResult, RetrievedElement, SchemaRetriever
from validation.validator import SQLValidator

logger = logging.getLogger(__name__)

MAX_RESULT_ROWS = 100_000
"""Rows one benchmark query may return before it is refused.

A bound is necessary -- a generated query is model output and can ask for a
cross join -- and it must **refuse rather than truncate**. Truncating both
sides to the same count looks safe and is not: two queries returning the same
rows in an unspecified order, cut at the same length, are cut in different
places, and the comparison then reports a value mismatch for a correct answer.
Refusing records an execution failure, which is what actually happened.

Set far above any Spider or BIRD result set. It is a runaway guard, not a
sampling policy.
"""

_ALL_ELEMENTS_SQL = """
    SELECT element_type, table_name, column_name, data_type, comment, serialized
    FROM agent_meta.schema_elements
    WHERE dataset = %s
    ORDER BY table_name, column_name NULLS FIRST
"""

_ALL_FOREIGN_KEYS_SQL = """
    SELECT from_table, from_column, to_table, to_column, constraint_name
    FROM agent_meta.foreign_keys
    WHERE dataset = %s
    ORDER BY from_table, from_column
"""


class Baseline(StrEnum):
    """The configurations EVALUATION.md section 4 requires, as answerers.

    Each is a different answerer over the same orchestration -- which is the
    property the runner was designed for, and the reason none of these needed a
    flag inside it.
    """

    FULL_SCHEMA = "full-schema"
    """No retrieval: every table and column of the question's database goes
    into the prompt. Answers "is retrieval helping, or just saving tokens?".
    Only viable because a Spider schema is small; it is the baseline that stops
    being runnable first as schemas grow, which is itself a finding."""

    RETRIEVAL_ONLY = "retrieval-only"
    """Retrieve, generate, execute. No validation tier."""

    WITH_VALIDATION = "with-validation"
    """Retrieval plus validation, and **no retry**. Self-correction is Stage 4
    and is a separate baseline precisely so the two contributions can be told
    apart: validation alone cannot raise accuracy, it can only convert an
    execution failure into a refusal that names its reason. If accuracy moves
    between this and `retrieval-only`, something other than validation did it."""


class Scopes(Protocol):
    """What the answerer needs from a scope resolver, and nothing more.

    A protocol rather than the concrete registry because the registry's whole
    job is opening connections and reading a catalog. Depending on the
    behaviour instead of the class is what lets the answerer's branching --
    which baseline retrieves, which validates, what happens when a database was
    never indexed -- be tested without a database at all.
    """

    def scope(self, db_id: str) -> DatabaseScope: ...

    def activate(self, scope: DatabaseScope) -> None: ...


@dataclass(frozen=True, slots=True)
class DatabaseScope:
    """Every component that must agree on *which* database a question is about.

    Bundled rather than passed around separately because the failure mode is a
    mismatch between two of them: a catalog for one database and a search_path
    for another produces SQL that validates and then reads the wrong tables.
    """

    db_id: str
    schema: str
    dataset: str
    catalog: SchemaCatalog
    retriever: SchemaRetriever | None
    validator: SQLValidator | None
    full_schema: RetrievalResult


class ScopeRegistry:
    """Builds and caches a :class:`DatabaseScope` per ``db_id``.

    Lazy per database, because a run limited to one database should not pay for
    twenty catalogs, and eager within one -- the catalog and the full-schema
    context are read once and held for every question that names it.

    Args:
        owner: Reaches ``agent_meta``. The catalog and the retriever need it;
            the read-only role has no privileges there at all.
        readonly: Used for ``EXPLAIN``. The containment boundary is not
            relaxed for a benchmark run.
        prefix: Schema-name prefix used at conversion time. ``db_id`` alone is
            unique within a benchmark, not across two.
    """

    def __init__(
        self,
        owner: Connection[Any],
        readonly: Connection[Any],
        *,
        settings: RetrievalSettings,
        execution: ExecutionSettings,
        embedder: Embedder | None,
        needs_retrieval: bool,
        needs_validation: bool,
        prefix: str = "",
    ) -> None:
        self._owner = owner
        self._readonly = readonly
        self._settings = settings
        self._execution = execution
        self._embedder = embedder
        self._needs_retrieval = needs_retrieval
        self._needs_validation = needs_validation
        self._prefix = prefix
        self._scopes: dict[str, DatabaseScope] = {}

    def scope(self, db_id: str) -> DatabaseScope:
        """The components for one database, built on first use.

        Raises:
            ValueError: ``db_id`` is blank. A question with no database cannot
                be answered against a corpus of twenty, and silently picking
                one is the wrong way to be wrong.
            RetrievalError: nothing is indexed for that database.
        """
        if not db_id:
            raise ValueError("a benchmark question must name the database it is about")

        cached = self._scopes.get(db_id)
        if cached is not None:
            return cached

        dataset = self._dataset_for(db_id)
        catalog = load_catalog(self._owner, dataset=dataset)
        if catalog.is_empty:
            raise RetrievalError(
                f"no schema elements indexed for {db_id!r} (dataset {dataset!r}). "
                f"Run `python -m benchmark.load index` over the converted schemas "
                f"before evaluating -- without a catalog every identifier resolves "
                f"to nothing and every question fails for the same uninformative reason."
            )

        scope = DatabaseScope(
            db_id=db_id,
            schema=dataset,
            dataset=dataset,
            catalog=catalog,
            retriever=self._build_retriever(dataset) if self._needs_retrieval else None,
            validator=self._build_validator(catalog) if self._needs_validation else None,
            # Read only for the baseline that uses it. The other baselines would
            # pay a full-catalog read per database for a value nothing looks at.
            full_schema=(
                RetrievalResult()
                if self._needs_retrieval
                else read_full_schema(self._owner, dataset)
            ),
        )
        self._scopes[db_id] = scope
        return scope

    def activate(self, scope: DatabaseScope) -> None:
        """Point the read-only session at this database's schema.

        Needed because ``EXPLAIN`` runs the candidate SQL's identifiers through
        the *session's* ``search_path``, and the candidate names bare tables.
        Without this every validation of every question fails with "relation
        does not exist" -- and it would fail identically for correct SQL and
        for hallucinated SQL, so the validation baseline would report a
        catastrophic invalid-query rate that is entirely an artifact of wiring.

        Session-scoped rather than transaction-scoped: the validator opens its
        own transaction, so a ``true`` here would be reverted before ``EXPLAIN``
        ran inside it.
        """
        self._readonly.execute("SELECT set_config('search_path', %s, false)", (scope.schema,))

    def _dataset_for(self, db_id: str) -> str:
        """The catalog namespace, which is also the PostgreSQL schema name.

        One name, not two. The conversion already chose a schema name for this
        database and validated it as an identifier; inventing a second naming
        scheme for the catalog would create exactly one bug, and it would be the
        kind where retrieval quietly returns another database's columns.
        """
        return f"{self._prefix}{db_id}" if self._prefix else db_id

    def _build_retriever(self, dataset: str) -> SchemaRetriever:
        """One retriever per database, because ``dataset`` is fixed at construction.

        That is the property doing the work rather than an inconvenience: the
        retriever filters every search on ``(dataset, model_version)`` so two
        vector spaces cannot mix, and here the same filter is what stops a
        question about one Spider database from being answered with another's
        columns.
        """
        if self._embedder is None:  # pragma: no cover - guarded at construction
            raise RetrievalError("retrieval was requested without an embedder")
        return SchemaRetriever(
            self._owner,
            self._embedder,
            dataset=dataset,
            default_k=self._settings.retrieval_top_k,
            ef_search=self._settings.hnsw_ef_search,
        )

    def _build_validator(self, catalog: SchemaCatalog) -> SQLValidator:
        return SQLValidator(self._readonly, catalog, self._execution)


def read_full_schema(connection: Connection[Any], dataset: str) -> RetrievalResult:
    """Every catalog element for one database, unranked.

    The ``full-schema`` baseline: no embedder, no vector search, no ``k``. It
    reads the same rows retrieval ranks, which is what makes the comparison
    between the two a measurement of *ranking* rather than of two different
    corpora.
    """
    with connection.cursor() as cur:
        cur.execute(_ALL_ELEMENTS_SQL, (dataset,))
        elements = tuple(
            RetrievedElement(
                element_type=row[0],
                table=row[1],
                column=row[2],
                data_type=row[3],
                comment=row[4],
                serialized=row[5],
                score=1.0,
            )
            for row in cur.fetchall()
        )
        cur.execute(_ALL_FOREIGN_KEYS_SQL, (dataset,))
        edges = tuple(
            ForeignKey(
                from_table=row[0],
                from_column=row[1],
                to_table=row[2],
                to_column=row[3],
                constraint_name=row[4],
            )
            for row in cur.fetchall()
        )

    return RetrievalResult(elements=elements, foreign_keys=edges)


class SchemaScopedQueryRunner:
    """Executes benchmark SQL against the schema its question belongs to.

    **Gold and predicted go through this same object**, which is the runner's
    one non-negotiable rule: two different execution paths return
    ``Decimal('1')`` on one side and ``1.0`` on the other, and a correct answer
    is scored as a value mismatch.

    Not :class:`~execution.executor.SQLExecutor`, and the difference is
    deliberate. That class re-validates and injects a row limit, both of which
    are containment controls for *model output*. Applying them to gold would
    mean a reference query rejected by the cost ceiling is recorded as a gold
    error, which it is not, and a gold result trimmed to 500 rows is compared
    against a prediction trimmed to a different 500. What this does share is the
    read-only role and a statement timeout -- the controls that are about the
    database rather than about the query's authorship.
    """

    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema_for: dict[str, str],
        statement_timeout_ms: int,
        max_rows: int = MAX_RESULT_ROWS,
        prefix: str = "",
    ) -> None:
        self._conn = connection
        self._schema_for = schema_for
        self._timeout_ms = statement_timeout_ms
        self._max_rows = max_rows
        self._prefix = prefix

    def __call__(self, sql: str, *, db_id: str = "") -> Rows:
        """Run one statement in one database's schema.

        Raises:
            ValueError: no database was named, or the result exceeded
                :data:`MAX_RESULT_ROWS`.
            psycopg.Error: the database refused the statement. The runner
                converts this into a recorded failure rather than an abort.
        """
        if not db_id:
            raise ValueError("a benchmark query must name the database it runs against")

        schema = self._schema_for.get(db_id) or f"{self._prefix}{db_id}"

        with self._conn.transaction(), self._conn.cursor() as cur:
            # set_config with a bound parameter, not a composed SET: the value
            # derives from a benchmark-supplied db_id. `true` scopes both to
            # this transaction, so one question cannot leak its search_path
            # into the next.
            cur.execute("SELECT set_config('search_path', %s, true)", (schema,))
            cur.execute(
                "SELECT set_config('statement_timeout', %s, true)", (str(self._timeout_ms),)
            )
            cur.execute(sql)
            if cur.description is None:
                return []
            fetched = cur.fetchmany(self._max_rows + 1)

        if len(fetched) > self._max_rows:
            raise ValueError(
                f"the query returned more than {self._max_rows:,} rows. Refused rather "
                f"than truncated -- a truncated comparison scores correct answers wrong."
            )
        return [list(row) for row in fetched]


@dataclass(frozen=True, slots=True)
class _ScopeContext:
    """Adapts a :class:`DatabaseScope` to the shared answerer's ``ContextSource``.

    The baseline decision -- retrieve, or hand over the whole schema -- lives
    here because it is an *eval* concept. The answering path only needs a
    question in and a context out, and keeping the baseline out of it is what
    stops the API growing a `full-schema` mode it has no use for.
    """

    scope: DatabaseScope
    baseline: Baseline
    top_k: int | None

    def context(self, question: str) -> RetrievalResult:
        if self.baseline is Baseline.FULL_SCHEMA:
            return self.scope.full_schema
        if self.scope.retriever is None:  # pragma: no cover - guarded at construction
            raise RetrievalError("retrieval baseline selected without a retriever")
        return self.scope.retriever.search(question, k=self.top_k)


class PipelineAnswerer:
    """Retrieval, generation and optionally validation, per question.

    Implements the runner's ``Answerer`` protocol: it returns an
    :class:`~evals.runner.Attempt` rather than raising, because a failure to
    produce SQL is a *result* for a benchmark and belongs in the taxonomy.

    Retrieval and generation themselves are delegated to
    :class:`~answering.QuestionAnswerer`, which the HTTP API also uses. That is
    the point: if the two composed those steps even slightly differently, the
    measured accuracy would describe a system nobody queries. What stays here
    is everything a benchmark needs and a request does not -- the baseline, the
    per-database scope, and flattening every failure into a category.

    Args:
        scopes: Resolves the per-database components.
        generator: Question plus schema context in, SQL out.
        baseline: Which of the EVALUATION.md section 4 configurations this is.
        top_k: Elements retrieved per question. Ignored by ``full-schema``.
    """

    def __init__(
        self,
        scopes: Scopes,
        generator: SQLGenerator,
        *,
        baseline: Baseline,
        top_k: int | None = None,
    ) -> None:
        self._scopes = scopes
        self._generator = generator
        self._baseline = baseline
        self._top_k = top_k
        # One loop for the whole run. See the module docstring: the async LLM
        # client caches a connection pool bound to the loop it first ran in, so
        # asyncio.run per question would work exactly once.
        self._loop = asyncio.new_event_loop()

    def __call__(self, question: Question) -> Attempt:
        try:
            scope = self._scopes.scope(question.db_id)
        except (RetrievalError, ValueError) as exc:
            return Attempt(error_type="scope_unavailable", error_message=str(exc))

        answerer = QuestionAnswerer(
            _ScopeContext(scope, self._baseline, self._top_k),
            self._generator,
        )

        # The phases are called separately rather than via `candidate()`,
        # because every failure below still has to report what was retrieved:
        # recall is a data point whether or not the model went on to answer,
        # and dropping it on failure would measure recall and accuracy over
        # different question sets.
        try:
            context = answerer.retrieve(question.question)
        except RetrievalError as exc:
            return Attempt(error_type="retrieval_failed", error_message=str(exc))

        retrieved = retrieved_columns(context)

        try:
            candidate = self._loop.run_until_complete(answerer.generate(question.question, context))
        except UnanswerableQuestionError as exc:
            # The model said the schema cannot answer this. A distinct outcome
            # from a malformed answer: retrying generation will not help, and
            # recording it as one would hide a retrieval failure as a model one.
            #
            # The cost still counts. A refusal is a completed call, and on the
            # first real run these were half the questions -- so dropping their
            # tokens halved the reported bill.
            return Attempt(
                error_type="unanswerable",
                error_message=str(exc),
                retrieved=retrieved,
                input_tokens=exc.usage.input_tokens,
                output_tokens=exc.usage.output_tokens,
                answering_model=exc.model,
            )
        except LLMError as exc:
            return Attempt(
                error_type="llm_failed",
                error_message=f"{type(exc).__name__}: {exc}",
                retrieved=retrieved,
            )

        def answered(sql: str | None, **rest: Any) -> Attempt:
            """One place that assembles an Attempt from a completed generation.

            The token counts and the answering model belong on *every* outcome
            past this point, including a rejected one -- a question that cost
            tokens and produced nothing still cost tokens, and a cost table
            that omits the failures understates the bill by exactly the
            questions worth investigating.
            """
            return Attempt(
                sql=sql,
                retrieved=retrieved,
                input_tokens=candidate.usage.input_tokens,
                output_tokens=candidate.usage.output_tokens,
                answering_model=candidate.model,
                **rest,
            )

        if scope.validator is None:
            return answered(candidate.sql)

        self._scopes.activate(scope)
        result = scope.validator.validate(candidate.sql)
        if not result.valid:
            # A `None` sql is what stops the runner executing it, which is the
            # entire behavioural difference this baseline measures. The text is
            # not lost -- the artifact keeps the error and the feedback, and a
            # failure analysis reads those.
            return answered(
                None,
                error_type=result.error_type,
                error_message=result.feedback,
                validation_attempts=1,
            )

        return answered(candidate.sql, validation_attempts=1)

    def _context(self, scope: DatabaseScope, question: str) -> RetrievalResult:
        if self._baseline is Baseline.FULL_SCHEMA:
            return scope.full_schema
        if scope.retriever is None:  # pragma: no cover - guarded at construction
            raise RetrievalError("retrieval baseline selected without a retriever")
        return scope.retriever.search(question, k=self._top_k)

    def close(self) -> None:
        """Release the event loop. Idempotent."""
        if not self._loop.is_closed():
            self._loop.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "MAX_RESULT_ROWS",
    "Baseline",
    "DatabaseScope",
    "PipelineAnswerer",
    "SchemaScopedQueryRunner",
    "ScopeRegistry",
    "Scopes",
    "read_full_schema",
]
