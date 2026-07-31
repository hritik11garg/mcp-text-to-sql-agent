"""FakeLLMClient must satisfy the port, so tests never touch a real provider."""

from __future__ import annotations

import pytest

from adapters.llm.fake import FakeLLMClient, text_response
from core.exceptions import ConfigurationError, LLMResponseError
from core.ports.llm import LLMClient, Message, Role
from core.settings import LLMProvider, LLMSettings

pytestmark = pytest.mark.unit


def test_fake_satisfies_the_llm_client_port() -> None:
    assert isinstance(FakeLLMClient(), LLMClient)


async def test_queued_responses_are_returned_in_order() -> None:
    llm = FakeLLMClient([text_response("first"), text_response("second")])
    messages = [Message(role=Role.USER, content="hi")]

    assert (await llm.complete(messages)).text == "first"
    assert (await llm.complete(messages)).text == "second"


async def test_exhaustion_raises_rather_than_hanging() -> None:
    """A silent empty response would let a runaway loop look like success."""
    llm = FakeLLMClient()
    with pytest.raises(LLMResponseError, match="exhausted"):
        await llm.complete([Message(role=Role.USER, content="hi")])


async def test_calls_are_recorded_for_assertions() -> None:
    llm = FakeLLMClient([text_response("ok")])
    await llm.complete([Message(role=Role.USER, content="what is revenue?")])

    assert len(llm.calls) == 1
    assert llm.calls[0][0].content == "what is revenue?"


def test_factory_builds_the_fake_provider() -> None:
    from adapters.llm.factory import build_llm_client

    client = build_llm_client(LLMSettings(llm_provider=LLMProvider.FAKE))
    assert isinstance(client, LLMClient)


def test_factory_builds_the_openai_compatible_adapter() -> None:
    """Groq, OpenRouter, Ollama and the rest differ by base_url alone, so one
    adapter covers them and the factory is the only place that knows."""
    from adapters.llm.factory import build_llm_client
    from adapters.llm.openai_compatible import OpenAICompatibleClient

    settings = LLMSettings(
        llm_provider=LLMProvider.OPENAI_COMPATIBLE,
        llm_base_url="https://api.groq.com/openai/v1",
        llm_model="llama-3.3-70b-versatile",
        llm_api_key="k",  # not a real credential
    )
    client = build_llm_client(settings)

    assert isinstance(client, OpenAICompatibleClient)
    assert client.model == "llama-3.3-70b-versatile"


def test_factory_reports_unimplemented_adapters_clearly() -> None:
    from adapters.llm.factory import build_llm_client

    settings = LLMSettings(
        llm_provider=LLMProvider.ANTHROPIC,
        llm_model="m",
        llm_api_key="k",  # not a real credential
    )
    with pytest.raises(ConfigurationError, match="not implemented yet"):
        build_llm_client(settings)
