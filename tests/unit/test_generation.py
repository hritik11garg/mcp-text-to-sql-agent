"""Prompt assembly and response cleaning, both pure enough to test hard.

Generation is the one component that depends on a network call, so everything
that can be decided without one is separated out and pinned here. The model's
*quality* is measured by the eval harness in Stage 2 and is never asserted.
"""

from __future__ import annotations

import pytest

from adapters.llm.fake import FakeLLMClient, text_response
from core.exceptions import LLMResponseError
from core.ports.llm import Role
from generation.generator import SQLGenerator, UnanswerableQuestionError, strip_formatting
from generation.prompts import (
    MAX_QUESTION_CHARS,
    SQL_SYSTEM_PROMPT,
    build_messages,
    render_context,
)
from schema.models import ForeignKey
from schema.retrieval import RetrievalResult, RetrievedElement

pytestmark = pytest.mark.unit


def element(table: str, column: str | None, data_type: str = "text", comment: str | None = None):
    return RetrievedElement(
        element_type="column" if column else "table",
        table=table,
        column=column,
        data_type=data_type,
        comment=comment,
        serialized=f"{table}.{column}",
        score=0.9,
    )


@pytest.fixture
def context() -> RetrievalResult:
    return RetrievalResult(
        elements=(
            element("orders", "total_amount", "numeric(12,2)", "Order total including tax"),
            element("orders", "customer_id", "bigint"),
            element("customers", "country", "text", "ISO 3166-1 alpha-2"),
        ),
        foreign_keys=(ForeignKey("orders", "customer_id", "customers", "id"),),
    )


class TestContextRendering:
    def test_columns_are_grouped_by_table(self, context: RetrievalResult) -> None:
        rendered = render_context(context)

        assert rendered.index("TABLE orders") < rendered.index("TABLE customers")
        assert "total_amount (numeric(12,2))" in rendered

    def test_comments_are_included(self, context: RetrievalResult) -> None:
        """The comment is often the only thing distinguishing two similar
        columns, and it is already in the catalog."""
        assert "Order total including tax" in render_context(context)

    def test_relationships_are_rendered(self, context: RetrievalResult) -> None:
        assert "orders.customer_id -> customers.id" in render_context(context)

    def test_an_empty_result_says_so_rather_than_rendering_nothing(self) -> None:
        """Silence would read as "no constraints" to a model."""
        assert "no schema elements matched" in render_context(RetrievalResult())


class TestPromptStructure:
    def test_the_dialect_is_stated(self, context: RetrievalResult) -> None:
        """A model asked for "SQL" emits whichever dialect it was trained on,
        and LIMIT versus TOP fails at execution rather than at parse time."""
        messages = build_messages("revenue by country", context)

        assert "PostgreSQL" in messages[0].content

    def test_the_system_message_comes_first_and_is_stable(self, context: RetrievalResult) -> None:
        """Cacheable prefixes are prefixes. Anything variable placed early
        invalidates everything after it."""
        first = build_messages("revenue by country", context)
        second = build_messages("a completely different question", context)

        assert first[0].role is Role.SYSTEM
        assert first[0].content == second[0].content
        assert SQL_SYSTEM_PROMPT in first[0].content

    def test_the_question_comes_last(self, context: RetrievalResult) -> None:
        messages = build_messages("revenue by country", context)
        user = messages[1].content

        assert user.index("SCHEMA") < user.index("QUESTION")

    def test_retry_feedback_is_appended_after_the_question(self, context: RetrievalResult) -> None:
        """So the cacheable prefix is identical across attempts."""
        first = build_messages("q", context)
        retry = build_messages(
            "q", context, feedback="column x does not exist", previous_sql="SELECT x"
        )

        assert retry[0].content == first[0].content
        assert retry[1].content.startswith(first[1].content)
        assert "column x does not exist" in retry[1].content

    def test_a_long_question_is_truncated(self, context: RetrievalResult) -> None:
        """Bounds prompt cost, and bounds how much instruction text a single
        injection attempt can carry."""
        messages = build_messages("x" * (MAX_QUESTION_CHARS + 500), context)

        assert "[truncated]" in messages[1].content

    def test_the_row_hint_is_included_when_given(self, context: RetrievalResult) -> None:
        messages = build_messages("q", context, max_rows=100)

        assert "100 rows" in messages[0].content

    def test_injection_framing_is_present(self, context: RetrievalResult) -> None:
        """Cheap, worth having, and explicitly not a control -- containment is."""
        messages = build_messages("q", context)

        assert "never an instruction" in messages[0].content


class TestStripFormatting:
    @pytest.mark.parametrize(
        "raw",
        [
            "```sql\nSELECT 1\n```",
            "```SQL\nSELECT 1\n```",
            "```postgresql\nSELECT 1\n```",
            "```\nSELECT 1\n```",
            "  SELECT 1  ",
            "sql: SELECT 1",
            "SQL:\nSELECT 1",
        ],
    )
    def test_formatting_is_removed(self, raw: str) -> None:
        """The prompt asks for raw SQL. Models emit fences by habit, and an
        instruction to a model is not an enforcement mechanism."""
        assert strip_formatting(raw) == "SELECT 1"

    def test_the_statement_itself_is_never_rewritten(self) -> None:
        """Rewriting model output would mean the SQL that was validated is not
        the SQL that was generated."""
        sql = "SELECT a, b FROM t WHERE x = '```sql'"

        assert strip_formatting(sql) == sql

    def test_multiline_sql_survives(self) -> None:
        raw = "```sql\nSELECT a\nFROM t\nWHERE b = 1\n```"

        assert strip_formatting(raw) == "SELECT a\nFROM t\nWHERE b = 1"


class TestGenerator:
    async def test_it_returns_cleaned_sql(self, context: RetrievalResult) -> None:
        llm = FakeLLMClient([text_response("```sql\nSELECT 1\n```")])
        generator = SQLGenerator(llm)

        assert await generator.generate("q", context) == "SELECT 1"

    async def test_it_passes_the_built_prompt_to_the_model(self, context: RetrievalResult) -> None:
        llm = FakeLLMClient([text_response("SELECT 1")])

        await SQLGenerator(llm).generate("revenue by country", context)

        sent = llm.calls[0]
        assert sent[0].role is Role.SYSTEM
        assert "revenue by country" in sent[1].content

    async def test_an_unanswerable_question_is_a_distinct_failure(
        self, context: RetrievalResult
    ) -> None:
        """Retrying generation will not help; retrieving more schema might.
        Collapsing this into a generic error would hide that."""
        llm = FakeLLMClient([text_response("CANNOT_ANSWER")])

        with pytest.raises(UnanswerableQuestionError):
            await SQLGenerator(llm).generate("what is the weather", context)

    async def test_an_empty_response_is_refused(self, context: RetrievalResult) -> None:
        llm = FakeLLMClient([text_response("   ")])

        with pytest.raises(LLMResponseError):
            await SQLGenerator(llm).generate("q", context)

    async def test_an_empty_question_is_refused_before_calling_the_model(
        self, context: RetrievalResult
    ) -> None:
        llm = FakeLLMClient([])

        with pytest.raises(LLMResponseError):
            await SQLGenerator(llm).generate("  ", context)

        assert llm.calls == []

    async def test_retry_feedback_reaches_the_model(self, context: RetrievalResult) -> None:
        llm = FakeLLMClient([text_response("SELECT total_amount FROM orders")])

        await SQLGenerator(llm).generate(
            "q", context, feedback='column "revenu" does not exist', previous_sql="SELECT revenu"
        )

        assert 'column "revenu" does not exist' in llm.calls[0][1].content
