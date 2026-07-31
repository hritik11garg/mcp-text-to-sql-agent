"""The fallback chain, and the settings format that feeds it.

Free tiers cap tokens per model per day, so exhausting one is an ordinary
operating condition. These tests pin the behaviour that turns it from a failed
run into a logged switch.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from adapters.llm.fake import FakeLLMClient, text_response
from adapters.llm.fallback import FallbackLLMClient
from core.exceptions import LLMQuotaExceededError, LLMResponseError, LLMUnavailableError
from core.ports.llm import LLMResponse, Message, ToolSpec
from core.settings import LLMProvider, LLMSettings

pytestmark = pytest.mark.unit


class ExhaustedClient:
    """A model whose daily allowance is spent. A legal port implementation."""

    def __init__(self, model: str) -> None:
        self._model = model
        self.calls = 0

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_tool_calling(self) -> bool:
        return True

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
        timeout_ms: int | None = None,
    ) -> LLMResponse:
        self.calls += 1
        raise LLMQuotaExceededError(f"model {self._model!r} is out of quota")


class TestFallbackChain:
    async def test_the_first_healthy_model_answers(self) -> None:
        primary = FakeLLMClient([text_response("SELECT 1")], model="primary")
        spare = FakeLLMClient([text_response("SELECT 2")], model="spare")

        chain = FallbackLLMClient([primary, spare])
        response = await chain.complete([])

        assert response.text == "SELECT 1"
        assert spare.calls == []

    async def test_an_exhausted_model_advances_the_chain(self) -> None:
        spent = ExhaustedClient("spent")
        spare = FakeLLMClient([text_response("SELECT 2")], model="spare")

        response = await FallbackLLMClient([spent, spare]).complete([])

        assert response.text == "SELECT 2"

    async def test_it_does_not_retry_an_exhausted_model_on_later_calls(self) -> None:
        """A spent daily cap stays spent. Re-testing it every request would add
        a guaranteed-failing round trip to each one."""
        spent = ExhaustedClient("spent")
        spare = FakeLLMClient([text_response("SELECT 1"), text_response("SELECT 2")], model="spare")
        chain = FallbackLLMClient([spent, spare])

        await chain.complete([])
        await chain.complete([])

        assert spent.calls == 1

    async def test_the_reported_model_is_the_one_that_answered(self) -> None:
        """Benchmark rows record the model that produced the answer. Reporting
        the head of the chain would attribute a run to a model that never saw
        the question."""
        chain = FallbackLLMClient(
            [ExhaustedClient("spent"), FakeLLMClient([text_response("x")], model="spare")]
        )
        await chain.complete([])

        assert chain.model == "spare"

    async def test_a_non_quota_failure_does_not_burn_the_chain(self) -> None:
        """A malformed response fails identically on every model. Advancing
        would spend quota on models that were still usable."""
        broken = FakeLLMClient([], model="broken")  # raises LLMResponseError
        spare = FakeLLMClient([text_response("SELECT 2")], model="spare")

        with pytest.raises(LLMResponseError):
            await FallbackLLMClient([broken, spare]).complete([])

        assert spare.calls == []

    async def test_exhausting_every_model_names_them(self) -> None:
        chain = FallbackLLMClient([ExhaustedClient("a"), ExhaustedClient("b")])

        with pytest.raises(LLMUnavailableError, match="a, b"):
            await chain.complete([])

    def test_tool_calling_requires_every_model_to_support_it(self) -> None:
        """Advertising a capability the fallbacks lack fails after the first
        exhaustion -- exactly when nobody is watching."""
        chain = FallbackLLMClient(
            [
                FakeLLMClient(model="a", supports_tool_calling=True),
                FakeLLMClient(model="b", supports_tool_calling=False),
            ]
        )

        assert chain.supports_tool_calling is False

    def test_an_empty_chain_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            FallbackLLMClient([])


class TestCommaSeparatedSettings:
    """List settings are written the way .env files are actually written.

    Without `NoDecode`, pydantic-settings JSON-decodes complex fields at the
    source, so `a,b` fails with a parse error before any validator sees it.
    """

    def test_a_comma_separated_list_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MODEL_FALLBACKS", "model-a, model-b ,model-c")

        settings = LLMSettings(llm_provider=LLMProvider.FAKE)

        assert settings.llm_model_fallbacks == ("model-a", "model-b", "model-c")

    def test_a_single_value_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MODEL_FALLBACKS", "only-one")

        assert LLMSettings(llm_provider=LLMProvider.FAKE).llm_model_fallbacks == ("only-one",)

    def test_empty_means_no_fallbacks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MODEL_FALLBACKS", "")

        assert LLMSettings(llm_provider=LLMProvider.FAKE).llm_model_fallbacks == ()

    def test_the_factory_wraps_only_when_there_is_something_to_fall_back_to(self) -> None:
        from adapters.llm.factory import build_llm_client
        from adapters.llm.openai_compatible import OpenAICompatibleClient

        base = {
            "llm_provider": LLMProvider.OPENAI_COMPATIBLE,
            "llm_base_url": "https://api.groq.com/openai/v1",
            "llm_api_key": "k",  # not a real credential
        }

        alone = build_llm_client(LLMSettings(llm_model="a", **base))
        chained = build_llm_client(
            LLMSettings(llm_model="a", llm_model_fallbacks=("b", "c"), **base)
        )

        assert isinstance(alone, OpenAICompatibleClient)
        assert isinstance(chained, FallbackLLMClient)
        assert chained.models == ("a", "b", "c")


class TestTruncationIsDistinguishable:
    async def test_a_truncated_empty_response_names_the_cause(self) -> None:
        """A reasoning model given too small a budget spends it all on
        reasoning and returns nothing. "Empty response" sends the reader
        hunting for a prompt bug; this says which knob to turn.

        Measured on Groq: openai/gpt-oss-120b needs 45 output tokens to answer
        "reply with OK", 43 of them reasoning.
        """
        from generation.generator import SQLGenerator
        from schema.retrieval import RetrievalResult

        llm = FakeLLMClient([LLMResponse(text="", truncated=True, model="reasoner")])

        with pytest.raises(LLMResponseError, match="LLM_MAX_TOKENS"):
            await SQLGenerator(llm).generate("q", RetrievalResult())

    async def test_an_untruncated_empty_response_is_reported_plainly(self) -> None:
        from generation.generator import SQLGenerator
        from schema.retrieval import RetrievalResult

        llm = FakeLLMClient([LLMResponse(text="", truncated=False)])

        with pytest.raises(LLMResponseError, match="empty response"):
            await SQLGenerator(llm).generate("q", RetrievalResult())
