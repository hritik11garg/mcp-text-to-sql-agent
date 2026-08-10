"""The answerer seam, without a database or a model.

The seam is where three components that were each built for a single schema
meet a corpus of twenty, so most of what can go wrong here is a *scoping*
mistake: the right SQL against the wrong database, or a validator holding one
catalog while the session points at another. Those produce plausible wrong
answers rather than errors, which is why the assertions below are about which
object was consulted, not only about what came back.

The LLM is the real ``FakeLLMClient`` and the scopes are a stub that records
what it was asked for -- neither is a mock of the code under test. See
docs/development/TESTING.md section 1.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from adapters.llm.fake import FakeLLMClient, text_response
from answering import ContextSource
from core.exceptions import LLMUnavailableError, RetrievalError
from core.ports.llm import LLMResponse, Usage
from evals.dataset import Question, Split
from evals.pipeline import (
    MAX_RESULT_ROWS,
    DatabaseScope,
    FullSchemaSource,
    PipelineAnswerer,
    RetrieverSource,
    SchemaScopedQueryRunner,
)
from generation.generator import SQLGenerator
from schema.catalog import SchemaCatalog
from schema.models import ForeignKey
from schema.retrieval import RetrievalResult, RetrievedElement
from validation.validator import ValidationResult

# --- doubles ---------------------------------------------------------------


class StubRetriever:
    """Records the searches it was asked for, and returns a fixed result."""

    def __init__(self, result: RetrievalResult) -> None:
        self._result = result
        self.searches: list[tuple[str, int | None]] = []

    def search(self, query: str, *, k: int | None = None, **_: Any) -> RetrievalResult:
        self.searches.append((query, k))
        return self._result


class StubValidator:
    def __init__(self, result: ValidationResult) -> None:
        self._result = result
        self.validated: list[str] = []

    def validate(self, sql: str) -> ValidationResult:
        self.validated.append(sql)
        return self._result


class StubScopes:
    """A scope resolver that hands out one prepared scope.

    ``activated`` is the assertion target for the search_path rule: a validator
    consulted without the session pointing at its schema would reject every
    correct query, and nothing about the result would say so.
    """

    def __init__(self, scope: DatabaseScope | None, *, error: Exception | None = None) -> None:
        self._scope = scope
        self._error = error
        self.asked: list[str] = []
        self.activated: list[str] = []

    def scope(self, db_id: str) -> DatabaseScope:
        self.asked.append(db_id)
        if self._error is not None:
            raise self._error
        assert self._scope is not None
        return self._scope

    def activate(self, scope: DatabaseScope) -> None:
        self.activated.append(scope.schema)


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]], *, describes: bool = True) -> None:
        self._rows = rows
        self.description = object() if describes else None
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed.append((statement, params))

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return self._rows[:size]

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeConnection:
    def __init__(self, rows: list[tuple[Any, ...]], *, describes: bool = True) -> None:
        self.cur = FakeCursor(rows, describes=describes)

    def transaction(self) -> FakeCursor:
        return self.cur  # doubles as a no-op context manager

    def cursor(self) -> FakeCursor:
        return self.cur


# --- fixtures --------------------------------------------------------------


def make_scope(
    *,
    retriever: StubRetriever | None = None,
    validator: StubValidator | None = None,
    full_schema: RetrievalResult | None = None,
    source: ContextSource | None = None,
    top_k: int = 10,
    schema: str = "spider_concert_singer",
) -> DatabaseScope:
    """A scope carrying the source its baseline resolved to.

    ``source`` defaults to the retriever, because that is what all three
    retrieving baselines build. A caller that wants the full-schema arm passes
    the source it expects -- which is the point of the change: the choice is
    made once, where the scope is built, and is visible in the object rather
    than recomputed from a flag on every question.
    """
    return DatabaseScope(
        db_id="concert_singer",
        schema=schema,
        dataset=schema,
        catalog=SchemaCatalog({"singer": frozenset({"name"})}),
        retriever=retriever,  # type: ignore[arg-type]
        validator=validator,  # type: ignore[arg-type]
        full_schema=full_schema or RetrievalResult(),
        source=source or RetrieverSource(retriever, top_k),  # type: ignore[arg-type]
    )


def element(table: str, column: str | None) -> RetrievedElement:
    return RetrievedElement(
        element_type="column" if column else "table",
        table=table,
        column=column,
        data_type="text",
        comment=None,
        serialized=f"{table}.{column}",
        score=0.9,
    )


@pytest.fixture
def make_answerer() -> Iterator[Callable[..., PipelineAnswerer]]:
    """Build answerers and close every one of them afterwards.

    The teardown is not tidiness. A :class:`PipelineAnswerer` owns an event loop
    for the life of a run, and an unclosed loop surfaces as a ``ResourceWarning``
    from whichever test happens to trigger the next garbage collection -- which
    is how this fixture came to exist rather than a plain helper.
    """
    built: list[PipelineAnswerer] = []

    def build(
        scopes: StubScopes,
        *,
        responses: list[LLMResponse] | None = None,
        llm: FakeLLMClient | None = None,
    ) -> PipelineAnswerer:
        client = llm or FakeLLMClient(
            responses if responses is not None else [text_response("SELECT 1")]
        )
        answerer = PipelineAnswerer(scopes, SQLGenerator(client))
        built.append(answerer)
        return answerer

    yield build

    for answerer in built:
        answerer.close()


def a_question(db_id: str = "concert_singer") -> Question:
    return Question(
        question_id="spider:dev:00001",
        question="How many singers are there?",
        gold_sql="SELECT count(*) FROM singer",
        dataset="spider",
        split=Split.DEV,
        db_id=db_id,
    )


# --- baselines -------------------------------------------------------------


class TestBaselines:
    def test_retrieval_only_searches_and_does_not_validate(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        retriever = StubRetriever(RetrievalResult(elements=(element("singer", "name"),)))
        scopes = StubScopes(make_scope(retriever=retriever, top_k=7))

        attempt = make_answerer(scopes)(a_question())

        assert attempt.sql == "SELECT 1"
        assert retriever.searches == [("How many singers are there?", 7)]
        assert attempt.validation_attempts == 0

    def test_full_schema_never_touches_the_retriever(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """The no-retrieval baseline answers "is retrieval helping at all?".

        If it quietly retrieved, the comparison would be between two identical
        configurations and the answer would always be "no difference".
        """
        retriever = StubRetriever(RetrievalResult())
        catalog = RetrievalResult(
            elements=(element("singer", "name"), element("singer", "age")),
            foreign_keys=(ForeignKey("singer", "id", "concert", "singer_id", "fk"),),
        )
        scopes = StubScopes(
            make_scope(
                retriever=retriever,
                full_schema=catalog,
                source=FullSchemaSource(catalog),
            )
        )

        attempt = make_answerer(scopes)(a_question())

        assert retriever.searches == []
        assert attempt.retrieved == (("singer", "name"), ("singer", "age"))

    def test_validation_baseline_activates_the_schema_before_explaining(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """`EXPLAIN` resolves bare table names through the *session's* search_path.

        Validating without pointing the session at this question's schema would
        reject every query for "relation does not exist" -- identically for
        correct and hallucinated SQL, so the invalid-query rate this baseline
        exists to measure would be pure wiring artifact.
        """
        validator = StubValidator(ValidationResult(valid=True, sql="SELECT 1"))
        scopes = StubScopes(
            make_scope(retriever=StubRetriever(RetrievalResult()), validator=validator)
        )

        attempt = make_answerer(scopes)(a_question())

        assert scopes.activated == ["spider_concert_singer"]
        assert validator.validated == ["SELECT 1"]
        assert attempt.sql == "SELECT 1"
        assert attempt.validation_attempts == 1

    def test_rejected_sql_is_not_handed_to_the_runner(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        validator = StubValidator(
            ValidationResult(
                valid=False,
                sql="SELECT nope",
                error_type="unknown_identifier",
                message='column "nope" does not exist',
                suggestion="name",
            )
        )
        scopes = StubScopes(
            make_scope(retriever=StubRetriever(RetrievalResult()), validator=validator)
        )

        attempt = make_answerer(scopes, responses=[text_response("SELECT nope")])(a_question())

        assert attempt.sql is None
        assert attempt.error_type == "unknown_identifier"
        assert "Nearest match: name" in attempt.error_message


# --- cost accounting -------------------------------------------------------


class TestCost:
    def test_tokens_and_the_answering_model_are_recorded(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """The model is read from the response, not from configuration.

        A fallback chain switches models mid-run when a free-tier cap is hit,
        and a run labelled with the configured model would be a blend of two
        with nothing saying so.
        """
        response = LLMResponse(
            text="SELECT 1",
            usage=Usage(input_tokens=120, output_tokens=8),
            model="llama-3.1-8b-instant",
        )
        scopes = StubScopes(make_scope(retriever=StubRetriever(RetrievalResult())))

        attempt = make_answerer(scopes, responses=[response])(a_question())

        assert (attempt.input_tokens, attempt.output_tokens) == (120, 8)
        assert attempt.answering_model == "llama-3.1-8b-instant"

    def test_a_rejected_query_still_reports_what_it_cost(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """A question that produced nothing still spent tokens.

        A cost table that omits the failures understates the bill by exactly
        the questions worth investigating.
        """
        response = LLMResponse(
            text="SELECT nope", usage=Usage(input_tokens=90, output_tokens=4), model="m"
        )
        validator = StubValidator(
            ValidationResult(valid=False, sql="SELECT nope", error_type="unknown_identifier")
        )
        scopes = StubScopes(
            make_scope(retriever=StubRetriever(RetrievalResult()), validator=validator)
        )

        attempt = make_answerer(scopes, responses=[response])(a_question())

        assert attempt.sql is None
        assert (attempt.input_tokens, attempt.output_tokens) == (90, 4)


# --- failures become results ----------------------------------------------


class TestFailuresAreResults:
    def test_an_unindexed_database_is_recorded_not_raised(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """One question that cannot be scoped is a data point; an aborted run is not."""
        scopes = StubScopes(None, error=RetrievalError("no schema elements indexed"))

        attempt = make_answerer(scopes)(a_question())

        assert attempt.sql is None
        assert attempt.error_type == "scope_unavailable"
        assert "no schema elements indexed" in attempt.error_message

    def test_a_question_naming_no_database_is_refused(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        scopes = StubScopes(None, error=ValueError("must name the database"))

        attempt = make_answerer(scopes)(a_question(db_id=""))

        assert attempt.error_type == "scope_unavailable"

    def test_a_provider_failure_is_recorded_with_its_type(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """A spent daily cap must cost the run its remaining questions, not its results."""

        class ExhaustedLLM(FakeLLMClient):
            async def complete(self, *args: Any, **kwargs: Any) -> LLMResponse:
                raise LLMUnavailableError("the model provider could not be reached")

        scopes = StubScopes(make_scope(retriever=StubRetriever(RetrievalResult())))

        attempt = make_answerer(scopes, llm=ExhaustedLLM())(a_question())

        assert attempt.error_type == "llm_failed"
        assert "LLMUnavailableError" in attempt.error_message

    def test_an_unanswerable_verdict_is_its_own_outcome(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """`CANNOT_ANSWER` is a retrieval finding wearing a generation costume.

        Recording it as a malformed answer would send the investigation to the
        prompt when the fix is to retrieve more schema.
        """
        scopes = StubScopes(make_scope(retriever=StubRetriever(RetrievalResult())))

        attempt = make_answerer(scopes, responses=[text_response("CANNOT_ANSWER")])(a_question())

        assert attempt.error_type == "unanswerable"
        assert attempt.sql is None

    def test_retrieved_elements_survive_a_generation_failure(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """Recall@k is measurable even when the model produced nothing.

        Dropping the retrieval record on a failed generation would make every
        failure look like a retrieval miss too.
        """
        retriever = StubRetriever(RetrievalResult(elements=(element("singer", "name"),)))
        scopes = StubScopes(make_scope(retriever=retriever))

        attempt = make_answerer(scopes, responses=[text_response("CANNOT_ANSWER")])(a_question())

        assert attempt.retrieved == (("singer", "name"),)


# --- the query runner ------------------------------------------------------


class TestQueryRunner:
    def test_the_schema_is_set_per_question_within_the_transaction(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        connection = FakeConnection([(1,), (2,)])
        runner = SchemaScopedQueryRunner(
            connection,  # type: ignore[arg-type]
            schema_for={"concert_singer": "spider_concert_singer"},
            statement_timeout_ms=30_000,
        )

        rows = runner("SELECT id FROM singer", db_id="concert_singer")

        assert rows == [[1], [2]]
        settings = [params for statement, params in connection.cur.executed if params]
        assert ("spider_concert_singer",) in settings
        assert ("30000",) in settings

    def test_an_unmapped_database_falls_back_to_the_prefixed_name(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """The schema map comes from verification; the prefix is the rule it followed.

        Deriving the name is what keeps a database absent from the map -- because
        it had no failures to report -- from being unrunnable.
        """
        connection = FakeConnection([])
        runner = SchemaScopedQueryRunner(
            connection,  # type: ignore[arg-type]
            schema_for={},
            statement_timeout_ms=1_000,
            prefix="spider_",
        )

        runner("SELECT 1", db_id="wta_1")

        assert ("spider_wta_1",) in [params for _, params in connection.cur.executed if params]

    def test_a_query_naming_no_database_is_refused(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        runner = SchemaScopedQueryRunner(
            FakeConnection([]),  # type: ignore[arg-type]
            schema_for={},
            statement_timeout_ms=1_000,
        )

        with pytest.raises(ValueError, match="must name the database"):
            runner("SELECT 1")

    def test_an_oversized_result_is_refused_rather_than_truncated(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """Truncating both sides to the same count is not safe.

        Two queries returning the same rows in an unspecified order, cut at the
        same length, are cut in *different places* -- and the comparison then
        reports a value mismatch for a correct answer. Refusing records what
        actually happened.
        """
        connection = FakeConnection([(n,) for n in range(12)])
        runner = SchemaScopedQueryRunner(
            connection,  # type: ignore[arg-type]
            schema_for={"db": "db"},
            statement_timeout_ms=1_000,
            max_rows=10,
        )

        with pytest.raises(ValueError, match="Refused rather than truncated"):
            runner("SELECT n FROM wide", db_id="db")

    def test_a_result_at_the_cap_is_returned(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        connection = FakeConnection([(n,) for n in range(10)])
        runner = SchemaScopedQueryRunner(
            connection,  # type: ignore[arg-type]
            schema_for={"db": "db"},
            statement_timeout_ms=1_000,
            max_rows=10,
        )

        assert len(runner("SELECT n FROM wide", db_id="db")) == 10

    def test_a_statement_returning_no_rows_yields_an_empty_result(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        connection = FakeConnection([], describes=False)
        runner = SchemaScopedQueryRunner(
            connection,  # type: ignore[arg-type]
            schema_for={"db": "db"},
            statement_timeout_ms=1_000,
        )

        assert runner("SELECT 1", db_id="db") == []

    def test_the_default_cap_is_far_above_any_benchmark_result(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """A runaway guard, not a sampling policy. Spider's largest result is tiny."""
        assert MAX_RESULT_ROWS >= 100_000


# --- lifecycle -------------------------------------------------------------


class TestLifecycle:
    def test_one_event_loop_serves_every_question(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        """`asyncio.run` per question would work exactly once.

        The OpenAI-compatible adapter caches its async client, and that client
        binds to the loop it first ran in -- so the second question would talk
        to a closed loop. This asserts the loop survives repeated calls.
        """
        scopes = StubScopes(make_scope(retriever=StubRetriever(RetrievalResult())))
        answerer = make_answerer(
            scopes, responses=[text_response("SELECT 1"), text_response("SELECT 2")]
        )

        first = answerer(a_question())
        second = answerer(a_question())
        answerer.close()

        assert (first.sql, second.sql) == ("SELECT 1", "SELECT 2")

    def test_closing_is_idempotent(self, make_answerer: Callable[..., PipelineAnswerer]) -> None:
        answerer = make_answerer(StubScopes(make_scope(retriever=StubRetriever(RetrievalResult()))))

        answerer.close()
        answerer.close()

    def test_it_works_as_a_context_manager(
        self, make_answerer: Callable[..., PipelineAnswerer]
    ) -> None:
        scopes = StubScopes(make_scope(retriever=StubRetriever(RetrievalResult())))

        with make_answerer(scopes) as answerer:
            assert answerer(a_question()).sql == "SELECT 1"
