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
from dataclasses import dataclass

from core.exceptions import LLMResponseError
from core.ports.llm import LLMClient, Usage
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

_THINK_RE = re.compile(r"^\s*<think\b.*</think>", re.DOTALL | re.IGNORECASE)
"""A leading reasoning block, which several open models put in `content`.

Greedy on purpose. Reasoning text routinely *discusses* its own tags -- the
sample that produced this contained the literal string ``</think>`` inside the
reasoning -- so matching to the **last** closing tag is what leaves the answer
rather than a fragment of the monologue.

Anchored at the start, because the only thing this is allowed to remove is a
prefix. A `<think>` appearing after SQL is not a reasoning block; it is a model
doing something this should not silently rewrite.
"""

_UNCLOSED_THINK_RE = re.compile(r"^\s*<think\b", re.IGNORECASE)


class UnanswerableQuestionError(LLMResponseError):
    """The model reported that the retrieved schema cannot answer the question.

    A distinct type because the correct response differs from every other
    failure: retrying generation will not help, but retrieving more schema
    might. See docs/ml/PROMPTS.md.

    **It carries the usage and the model**, because a refusal is a completed
    call. The first real run made this concrete: half of 150 questions came back
    ``CANNOT_ANSWER``, and with the cost attached only to the success path the
    reported token bill was roughly half of what had actually been spent. A cost
    figure that omits the refusals understates by exactly the questions worth
    investigating.
    """

    def __init__(self, message: str, *, usage: Usage | None = None, model: str = "") -> None:
        super().__init__(message)
        self.usage = usage or Usage()
        self.model = model


@dataclass(frozen=True, slots=True)
class Generated:
    """Cleaned SQL, plus what producing it cost and which model produced it.

    The model is recorded per call rather than read from configuration because
    a fallback chain switches models mid-run when a free-tier daily cap is hit
    (:class:`~adapters.llm.fallback.FallbackLLMClient`). A run labelled with the
    *configured* model would then be a blend of two, with nothing on the page
    saying so.
    """

    sql: str
    usage: Usage
    model: str


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
        generated = await self.generate_detailed(
            question,
            context,
            feedback=feedback,
            previous_sql=previous_sql,
            timeout_ms=timeout_ms,
        )
        return generated.sql

    async def generate_detailed(
        self,
        question: str,
        context: RetrievalResult,
        *,
        feedback: str | None = None,
        previous_sql: str | None = None,
        timeout_ms: int | None = None,
    ) -> Generated:
        """:meth:`generate`, with the cost and the answering model attached.

        A separate method rather than a changed return type: every caller that
        only wants SQL keeps getting SQL, and the one caller that has to report
        a token bill -- the eval harness -- asks for the rest explicitly. The
        two share one implementation, so they cannot disagree about what
        counts as a usable answer.
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
                "the model reported that the retrieved schema cannot answer this question",
                usage=response.usage,
                model=response.model,
            )

        return Generated(sql=sql, usage=response.usage, model=response.model)


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

    **A leading ``<think>`` block is formatting too**, and is stripped first.
    Several open-weight models -- Qwen, DeepSeek-R1 derivatives, and whatever a
    free tier substitutes when the primary is rate limited -- put their
    reasoning in the ``content`` field rather than a separate one, then emit the
    answer after ``</think>``. Left in place the whole monologue is submitted as
    a query, and it fails to execute.

    That is not hypothetical: it cost 27 of 150 questions in the first real
    benchmark run, every one of them with correct SQL sitting after the closing
    tag. It went unnoticed because the *configured* model does not do this --
    only the fallback the rate limiter switched to does, which is exactly the
    kind of difference a fallback chain is supposed to absorb and this one did
    not.

    An **unterminated** block returns empty rather than a fragment of reasoning.
    The model spent its whole budget thinking, and the caller already reports
    that case usefully; handing back half a monologue would send it to the
    validator instead.
    """
    cleaned = text.strip()

    # Before the fence check: a reasoning model emits ```sql *after* </think>,
    # so a fence match on the whole blob would never anchor.
    cleaned = _THINK_RE.sub("", cleaned, count=1).strip()
    if _UNCLOSED_THINK_RE.match(cleaned):
        return ""

    fenced = _FENCE_RE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()

    cleaned = _LEADING_LABEL_RE.sub("", cleaned, count=1)

    return cleaned.strip()
