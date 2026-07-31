"""Question in, candidate SQL out.

Deliberately small. It builds a prompt, calls the port, and cleans up the
answer -- it does **not** validate, execute, or retry. Those belong to
components that already exist and are separately testable, and folding them in
here would make the one piece that depends on a network call also the piece
that owns the control flow.

The output of this module is untrusted. It is the text of an untrusted model
responding to an untrusted question over untrusted schema comments, and it goes
straight to the validator.
"""

from __future__ import annotations

import logging
import re

from core.exceptions import LLMResponseError
from core.ports.llm import LLMClient
from generation.prompts import build_messages
from schema.retrieval import RetrievalResult

logger = logging.getLogger(__name__)

CANNOT_ANSWER = "CANNOT_ANSWER"
"""What the model is told to emit when the schema cannot answer the question.

An explicit refusal is worth more than a guess: a guessed query returns rows,
and rows that answer the wrong question are far more expensive than an honest
"I cannot".
"""

_FENCE_RE = re.compile(
    r"^\s*```(?:sql|postgresql|postgres)?\s*\n(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE
)
_LEADING_LABEL_RE = re.compile(r"^\s*(?:sql|query)\s*:\s*", re.IGNORECASE)


class UnanswerableQuestionError(LLMResponseError):
    """The model reported that the retrieved schema cannot answer the question.

    A distinct type because the correct response differs from every other
    failure: retrying generation will not help, but retrieving more schema
    might. See docs/ml/PROMPTS.md.
    """


class SQLGenerator:
    """Builds the prompt, calls the model, returns cleaned SQL.

    Args:
        llm: Any implementation of the port, including the fake.
        dialect: Stated in the prompt. A model asked for "SQL" emits whichever
            dialect its training favoured.
        max_rows: Mentioned to the model. The binding limit is applied to the
            AST at execution -- this is a hint, not a control.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        dialect: str = "PostgreSQL",
        max_rows: int | None = None,
    ) -> None:
        self._llm = llm
        self._dialect = dialect
        self._max_rows = max_rows

    async def generate(
        self,
        question: str,
        context: RetrievalResult,
        *,
        feedback: str | None = None,
        previous_sql: str | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        """Produce one candidate query.

        Args:
            feedback: A validation failure from a previous attempt, appended
                after the question so the cacheable prefix stays identical
                across retries.

        Raises:
            UnanswerableQuestionError: the model reported the schema is insufficient.
            LLMResponseError: the model returned nothing usable.
        """
        if not question.strip():
            raise LLMResponseError("the question is empty")

        messages = build_messages(
            question,
            context,
            dialect=self._dialect,
            max_rows=self._max_rows,
            feedback=feedback,
            previous_sql=previous_sql,
        )

        response = await self._llm.complete(messages, timeout_ms=timeout_ms)
        sql = strip_formatting(response.text)

        logger.info(
            "sql generated",
            extra={
                "model": response.model,
                "tables_offered": len(context.tables),
                "is_retry": feedback is not None,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": response.usage.cache_read_tokens,
            },
        )

        if not sql:
            if response.truncated:
                # Almost always a reasoning model with too small a budget: the
                # allowance was spent thinking and none was left to answer with.
                # Saying so beats "empty response", which sends the reader
                # looking for a bug in the prompt.
                raise LLMResponseError(
                    f"model {response.model!r} hit the token limit before producing any "
                    f"SQL ({response.usage.output_tokens} output tokens, all of them "
                    f"reasoning). Raise LLM_MAX_TOKENS."
                )
            raise LLMResponseError("the model returned an empty response")
        if sql.upper().startswith(CANNOT_ANSWER):
            raise UnanswerableQuestionError(
                "the model reported that the retrieved schema cannot answer this question"
            )

        return sql


def strip_formatting(text: str) -> str:
    """Recover raw SQL from what a model actually returns.

    The prompt asks for no markdown. Models emit fenced blocks anyway, out of
    habit, and a fence is not valid SQL -- so this repairs it rather than
    relying on the instruction being followed. That is the same position taken
    everywhere else in this project: an instruction to a model is not an
    enforcement mechanism.

    Only *formatting* is removed. Nothing here alters the statement itself;
    rewriting model output would mean the SQL that was validated is not the SQL
    that was generated.
    """
    cleaned = text.strip()

    fenced = _FENCE_RE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()

    cleaned = _LEADING_LABEL_RE.sub("", cleaned, count=1)

    return cleaned.strip()
