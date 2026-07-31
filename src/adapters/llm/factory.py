"""Selects and builds the configured LLM adapter.

The only place in the codebase that knows which providers exist. Everything
else depends on the :class:`LLMClient` port. See ADR-014.
"""

from __future__ import annotations

from core.exceptions import ConfigurationError
from core.ports.llm import LLMClient
from core.settings import LLMProvider, LLMSettings

from .fake import FakeLLMClient
from .openai_compatible import OpenAICompatibleClient


def build_llm_client(settings: LLMSettings) -> LLMClient:
    """Construct the adapter named by ``LLM_PROVIDER``.

    Raises:
        ConfigurationError: the provider is known but not yet implemented, or
            the settings are inconsistent with it.
    """
    match settings.llm_provider:
        case LLMProvider.FAKE:
            return FakeLLMClient(model=settings.llm_model or "fake-model")

        case LLMProvider.OPENAI_COMPATIBLE:
            return OpenAICompatibleClient(
                model=settings.llm_model,
                base_url=settings.llm_base_url,
                api_key=(
                    settings.llm_api_key.get_secret_value()
                    if settings.llm_api_key is not None
                    else None
                ),
                timeout_ms=settings.llm_timeout_ms,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
            )

        case LLMProvider.ANTHROPIC:
            # Stage 1: second adapter, only useful with an Anthropic key.
            raise ConfigurationError(
                "anthropic adapter is not implemented yet (Stage 1). "
                "Set LLM_PROVIDER=fake to run without a provider."
            )
