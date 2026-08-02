"""Prompt assembly and response cleaning, both pure enough to test hard.

Generation is the one component that depends on a network call, so everything
that can be decided without one is separated out and pinned here. The model's
*quality* is measured by the eval harness in Stage 2 and is never asserted.
"""

from __future__ import annotations

import pytest

from adapters.llm.fake import FakeLLMClient, text_response
from core.exceptions import LLMResponseError
from core.ports.llm import LLMResponse, Role, Usage
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


class TestRefusalCarriesItsCost:
    """A refusal is a completed call, and it is billed like one.

    Found on the first real run: half of 150 questions came back
    `CANNOT_ANSWER`, and because the cost was attached only to the success path
    the reported token bill was roughly half of what had been spent.
    """

    async def test_the_usage_is_attached(self) -> None:
        llm = FakeLLMClient(
            [
                LLMResponse(
                    text="CANNOT_ANSWER",
                    usage=Usage(input_tokens=340, output_tokens=6),
                    model="llama-3.1-8b-instant",
                )
            ]
        )

        with pytest.raises(UnanswerableQuestionError) as caught:
            await SQLGenerator(llm).generate("who?", RetrievalResult())

        assert caught.value.usage.input_tokens == 340
        assert caught.value.usage.output_tokens == 6

    async def test_the_answering_model_is_attached(self) -> None:
        """Which model refused matters when a fallback chain is in play --
        a weaker model refuses more, and that is a finding rather than noise."""
        llm = FakeLLMClient(
            [LLMResponse(text="CANNOT_ANSWER", usage=Usage(), model="llama-3.1-8b-instant")]
        )

        with pytest.raises(UnanswerableQuestionError) as caught:
            await SQLGenerator(llm).generate("who?", RetrievalResult())

        assert caught.value.model == "llama-3.1-8b-instant"

    async def test_it_is_still_the_distinct_error_type(self) -> None:
        # The reason it is separate: retrieving more schema helps, retrying
        # generation does not. Adding fields must not blur that.
        llm = FakeLLMClient([text_response("CANNOT_ANSWER")])

        with pytest.raises(UnanswerableQuestionError):
            await SQLGenerator(llm).generate("who?", RetrievalResult())


class TestReasoningBlocks:
    """Several open models put their reasoning in `content` and the answer after it.

    Cost 27 of 150 questions in the first real benchmark run -- every one with
    correct SQL sitting after the closing tag. The configured model does not do
    this; the fallback the rate limiter switched to does.
    """

    def test_the_answer_after_the_block_survives(self) -> None:
        text = "<think>\nThe user wants a count.\nI will use cars_data.\n</think>\n\nSELECT 1"

        assert strip_formatting(text) == "SELECT 1"

    def test_a_block_that_discusses_its_own_tag_still_resolves(self) -> None:
        """Reasoning text routinely quotes its own tags. Matching the *first*
        closing tag would return a fragment of the monologue as SQL."""
        text = '<think>I must end with "</think>" then answer.</think>\nSELECT 2'

        assert strip_formatting(text) == "SELECT 2"

    def test_a_fence_after_the_block_is_still_removed(self) -> None:
        # The reason the think strip runs first: a fence match on the whole
        # blob would never anchor at the start.
        text = "<think>reasoning</think>\n```sql\nSELECT 3\n```"

        assert strip_formatting(text) == "SELECT 3"

    def test_an_unterminated_block_yields_nothing(self) -> None:
        """The budget went on thinking. Half a monologue is not SQL, and the
        caller already reports the truncation case usefully."""
        assert strip_formatting("<think>I am still thinking and the budget ran") == ""

    def test_sql_containing_the_word_think_is_untouched(self) -> None:
        assert strip_formatting("SELECT think FROM notes") == "SELECT think FROM notes"

    def test_a_trailing_tag_is_not_treated_as_a_block(self) -> None:
        """Only a *prefix* may be removed. A model doing something else here is
        not something to silently rewrite."""
        text = "SELECT 1 <think>afterthought</think>"

        assert strip_formatting(text) == text

    def test_the_case_of_the_tag_does_not_matter(self) -> None:
        assert strip_formatting("<THINK>x</THINK>\nSELECT 4") == "SELECT 4"
