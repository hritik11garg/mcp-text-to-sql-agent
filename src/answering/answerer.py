"""Retrieve, then generate. The two steps both callers must perform identically.

See the package docstring for why execution is deliberately excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.ports.llm import Usage
from generation.generator import SQLGenerator
from schema.retrieval import RetrievalResult

__all__ = [
    "Candidate",
    "ContextSource",
    "QuestionAnswerer",
]


@runtime_checkable
class ContextSource(Protocol):
    """Supplies the schema context for a question.

    A protocol rather than a concrete retriever because the eval harness has a
    ``full-schema`` baseline whose entire purpose is to answer *without*
    retrieving -- and that baseline exists to measure what retrieval is worth,
    so it cannot be expressed as "retrieval with a large k". Both shapes are a
    question in and a :class:`RetrievalResult` out, which is the only thing the
    generator needs.
    """

    def context(self, question: str) -> RetrievalResult: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    """Generated SQL, plus everything a caller needs to explain or bill it.

    ``retrieved`` is here rather than derived by the caller because Recall@k is
    computed from it and a second derivation is a second chance to disagree
    about what counts as retrieved.
    """

    sql: str
    retrieved: tuple[tuple[str, str], ...]
    """(table, column) pairs, columns only -- a table match is not a linkable element."""

    usage: Usage
    model: str
    """The model that actually answered, which a fallback chain can change mid-run."""

    context: RetrievalResult


class QuestionAnswerer:
    """Question in, candidate SQL out.

    Raises rather than returning a failure value -- see the package docstring.
    The exceptions are the components' own: :class:`RetrievalError`,
    :class:`UnanswerableQuestionError`, and :class:`LLMError` and its subclasses.
    None are caught here, deliberately: this class adds no error handling of its
    own because both callers need to distinguish the cases differently, and a
    layer that flattened them would have to be unwound by each.

    Args:
        source: Supplies schema context per question.
        generator: Question plus context in, SQL out.
        top_k: Elements to retrieve. ``None`` uses the source's own default.
    """

    def __init__(
        self,
        source: ContextSource,
        generator: SQLGenerator,
        *,
        top_k: int | None = None,
    ) -> None:
        self._source = source
        self._generator = generator
        self._top_k = top_k

    async def candidate(
        self,
        question: str,
        *,
        feedback: str | None = None,
        previous_sql: str | None = None,
    ) -> Candidate:
        """Retrieve context, generate SQL against it. The whole path, composed.

        This is what a request wants: one call, one answer, and nothing kept
        from the middle. It exists alongside the two phases below so that the
        composition is stated once and can be asserted -- a caller that
        sequences the phases itself is a caller that can sequence them wrongly.
        """
        return await self.generate(
            question,
            self.retrieve(question),
            feedback=feedback,
            previous_sql=previous_sql,
        )

    def retrieve(self, question: str) -> RetrievalResult:
        """Phase 1, exposed because a benchmark needs what a request discards.

        Recall@k is computed from the retrieved elements *whether or not the
        model went on to answer* -- a generation failure must not remove a
        retrieval data point, or the two metrics are measured over different
        question sets and the correlation between them is meaningless.

        So the eval calls the phases and keeps the intermediate; the API calls
        :meth:`candidate` and does not.
        """
        return self._source.context(question)

    async def generate(
        self,
        question: str,
        context: RetrievalResult,
        *,
        feedback: str | None = None,
        previous_sql: str | None = None,
    ) -> Candidate:
        """Phase 2. Takes the context rather than a question alone.

        ``feedback`` and ``previous_sql`` carry a validation or execution error
        back into the prompt. Unused by either caller today; the shape is here
        because the self-correction loop (Stage 4) must retry against the
        **same** context. Re-retrieving on a retry answers a subtly different
        question and makes the retry's effect unattributable.
        """
        generated = await self._generator.generate_detailed(
            question,
            context,
            feedback=feedback,
            previous_sql=previous_sql,
        )

        return Candidate(
            sql=generated.sql,
            retrieved=retrieved_columns(context),
            usage=generated.usage,
            model=generated.model,
            context=context,
        )


def retrieved_columns(context: RetrievalResult) -> tuple[tuple[str, str], ...]:
    """The (table, column) pairs Recall@k is computed over.

    One definition, because the eval derives this on the failure path and the
    answerer derives it on the success path, and two derivations are two
    chances to disagree about what counts as retrieved. Table-level matches are
    excluded: a table with no column named is not a linkable element.
    """
    return tuple((element.table, element.column) for element in context.elements if element.column)
