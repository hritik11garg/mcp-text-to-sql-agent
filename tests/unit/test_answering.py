"""The path the eval measures and the API serves must be one path.

These tests exist to pin that claim rather than to re-test retrieval or
generation, which have their own suites. The property that matters is
compositional: `candidate()` must do exactly what calling the two phases does,
because the eval calls the phases and the API calls the composition, and a
divergence between them would mean the benchmark number describes a system
nobody queries.
"""

from __future__ import annotations

import pytest

from adapters.llm.fake import FakeLLMClient, text_response
from answering import Candidate, QuestionAnswerer, retrieved_columns
from core.exceptions import RetrievalError
from generation.generator import SQLGenerator, UnanswerableQuestionError
from schema.retrieval import RetrievalResult, RetrievedElement

pytestmark = pytest.mark.unit


def element(table: str, column: str | None) -> RetrievedElement:
    return RetrievedElement(
        element_type="column" if column else "table",
        table=table,
        column=column,
        data_type="text",
        comment=None,
        score=0.9,
        serialized=f"{table}.{column}" if column else table,
    )


CONTEXT = RetrievalResult(
    elements=(element("orders", "total"), element("orders", None), element("customers", "region"))
)


class StubSource:
    """A context source that records what it was asked and how often."""

    def __init__(self, result: RetrievalResult | None = None, error: Exception | None = None):
        self.result = result if result is not None else CONTEXT
        self.error = error
        self.questions: list[str] = []

    def context(self, question: str) -> RetrievalResult:
        self.questions.append(question)
        if self.error is not None:
            raise self.error
        return self.result


def make(
    llm: FakeLLMClient, source: StubSource | None = None
) -> tuple[QuestionAnswerer, StubSource]:
    src = source or StubSource()
    return QuestionAnswerer(src, SQLGenerator(llm)), src


class TestTheSharedPath:
    async def test_it_returns_the_generated_sql_with_its_cost(self) -> None:
        answerer, _ = make(FakeLLMClient([text_response("SELECT 1")]))

        candidate = await answerer.candidate("how many orders")

        assert isinstance(candidate, Candidate)
        assert candidate.sql == "SELECT 1"
        assert candidate.context is CONTEXT

    async def test_composition_matches_the_phases(self) -> None:
        """The whole reason both entry points exist.

        The eval calls `retrieve` then `generate` because it needs the
        intermediate; the API calls `candidate`. If those two produced
        different SQL, the measured accuracy would not be the API's accuracy.
        """
        composed, _ = make(FakeLLMClient([text_response("SELECT 1")]))
        phased, _ = make(FakeLLMClient([text_response("SELECT 1")]))

        by_composition = await composed.candidate("q")
        by_phase = await phased.generate("q", phased.retrieve("q"))

        assert by_composition == by_phase

    async def test_the_question_reaches_retrieval_unmodified(self) -> None:
        answerer, source = make(FakeLLMClient([text_response("SELECT 1")]))

        await answerer.candidate("  revenue by region  ")

        assert source.questions == ["  revenue by region  "]

    async def test_retrieval_runs_exactly_once_per_answer(self) -> None:
        """A second retrieval per question would double the ANN cost and, worse,
        could return a different context than the one the SQL was written for."""
        answerer, source = make(FakeLLMClient([text_response("SELECT 1")]))

        await answerer.candidate("q")

        assert len(source.questions) == 1


class TestFailuresPropagate:
    """This module raises. See the package docstring: the eval flattens
    exceptions into a taxonomy, the API maps them to status codes, and a
    shared layer that pre-flattened them would have to be unwound by both."""

    async def test_a_retrieval_failure_is_not_swallowed(self) -> None:
        answerer, _ = make(
            FakeLLMClient([text_response("SELECT 1")]),
            StubSource(error=RetrievalError("catalog is empty")),
        )

        with pytest.raises(RetrievalError):
            await answerer.candidate("q")

    async def test_an_unanswerable_question_raises_with_its_cost_intact(self) -> None:
        """A refusal is a completed call. Losing its usage understates the bill
        by exactly the questions worth investigating."""
        answerer, _ = make(FakeLLMClient([text_response("CANNOT_ANSWER")]))

        with pytest.raises(UnanswerableQuestionError) as caught:
            await answerer.candidate("what is the weather")

        assert caught.value.usage is not None

    async def test_retrieval_failing_means_generation_is_never_called(self) -> None:
        """The LLM queue is empty, so a call would raise a different error --
        which is the assertion: no tokens are spent on a question whose schema
        context could not be built."""
        answerer, _ = make(FakeLLMClient([]), StubSource(error=RetrievalError("down")))

        with pytest.raises(RetrievalError):
            await answerer.candidate("q")


class TestRetrievedColumns:
    """One definition, because the eval derives this on the failure path and
    the answerer derives it on the success path."""

    def test_a_table_match_is_not_a_retrieved_element(self) -> None:
        # Recall@k is measured over columns; a table with no column named is
        # not something a question can be linked to.
        assert retrieved_columns(CONTEXT) == (("orders", "total"), ("customers", "region"))

    async def test_it_agrees_with_what_the_candidate_reports(self) -> None:
        """The two derivations must not be able to disagree.

        The eval computes recall from this helper on the failure path and from
        `Candidate.retrieved` on the success path, so a question that fails
        generation and one that succeeds have to count retrieval the same way.
        """
        answerer, _ = make(FakeLLMClient([text_response("SELECT 1")]))

        candidate = await answerer.candidate("q")

        assert candidate.retrieved == retrieved_columns(CONTEXT)

    def test_an_empty_context_retrieves_nothing(self) -> None:
        assert retrieved_columns(RetrievalResult()) == ()
