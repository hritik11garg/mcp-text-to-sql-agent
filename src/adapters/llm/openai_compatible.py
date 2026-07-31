"""One adapter for every provider that speaks `/chat/completions`.

Groq, OpenRouter, Cerebras, Gemini's compatibility endpoint, Ollama and
LM Studio all expose the same shape, so they differ by ``base_url`` and nothing
else. Writing a class per vendor would be duplication in the costume of
flexibility -- see ADR-014.

The SDK is imported lazily and never escapes this module. Everything upstream
depends on :class:`~core.ports.llm.LLMClient`, which is what makes
``FakeLLMClient`` a drop-in and keeps the agent loop out of a vendor's hands.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from core.exceptions import LLMResponseError, LLMUnavailableError
from core.ports.llm import LLMResponse, Message, ToolCall, ToolSpec, Usage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4_096


class OpenAICompatibleClient:
    """Talks to any OpenAI-shaped chat completions endpoint.

    Args:
        model: Provider-specific model id, e.g. ``llama-3.3-70b-versatile``.
        base_url: Endpoint root. Validated for SSRF in
            :mod:`core.settings` *before* it reaches here -- that check is the
            control, not this class.
        api_key: Sent as a bearer token. Never logged, never included in an
            error message.
        temperature: 0.0 by default. SQL generation is not a creative task,
            and a non-deterministic generator makes the eval harness measure
            sampling noise as well as model quality.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_ms: int = 60_000,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
        supports_tool_calling: bool = True,
    ) -> None:
        if not model:
            raise LLMResponseError("a model id is required")

        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._timeout_s = timeout_ms / 1000
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._supports_tool_calling = supports_tool_calling
        self._client: AsyncOpenAI | None = None

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_tool_calling(self) -> bool:
        """Declared, not detected.

        Several free-tier models advertise tool calling and then ignore the
        tools. The agent must be able to be told to use prompt-based structured
        output instead, which is why this is configuration rather than a probe.
        """
        return self._supports_tool_calling

    def _load(self) -> AsyncOpenAI:
        """Import and construct on first use.

        Lazy so that importing this module -- which the factory does
        unconditionally -- does not require the SDK to be installed.
        """
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - packaging failure
                raise LLMUnavailableError(
                    "the 'openai' package is required for LLM_PROVIDER=openai_compatible"
                ) from exc

            self._client = AsyncOpenAI(
                api_key=self._api_key or "not-required",
                base_url=self._base_url,
                timeout=self._timeout_s,
                max_retries=2,
            )
        return self._client

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
        timeout_ms: int | None = None,
    ) -> LLMResponse:
        client = self._load()

        request: dict[str, Any] = {
            "model": self._model,
            "messages": [_to_wire(message) for message in messages],
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": self._temperature,
        }
        if tools and self._supports_tool_calling:
            request["tools"] = [_tool_to_wire(tool) for tool in tools]

        if timeout_ms is not None:
            client = client.with_options(timeout=timeout_ms / 1000)

        try:
            response = await client.chat.completions.create(**request)
        except Exception as exc:
            # The SDK's exception hierarchy is a vendor detail. Callers get a
            # domain error; the class name is enough to diagnose without
            # forwarding a message that may quote the request -- and the
            # request carries the API key.
            raise LLMUnavailableError(
                f"the model provider could not be reached ({type(exc).__name__})"
            ) from exc

        return _from_wire(response, fallback_model=self._model)


def _to_wire(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": str(message.role), "content": message.content}
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _tool_to_wire(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _from_wire(response: Any, *, fallback_model: str) -> LLMResponse:
    """Translate a provider response, tolerating the parts that vary.

    Compatibility endpoints agree on the shape and disagree on the details --
    some omit ``usage``, some omit cached-token counts, some return ``None``
    for content when a tool call is present. Every access here is defensive
    because the failure mode otherwise is an ``AttributeError`` deep in the
    agent loop that reads like a bug in this project.
    """
    choices = getattr(response, "choices", None)
    if not choices:
        raise LLMResponseError("the provider returned no choices")

    message = choices[0].message
    text = getattr(message, "content", None) or ""

    return LLMResponse(
        text=text,
        tool_calls=_read_tool_calls(message),
        usage=_read_usage(response),
        model=getattr(response, "model", None) or fallback_model,
    )


def _read_tool_calls(message: Any) -> tuple[ToolCall, ...]:
    raw = getattr(message, "tool_calls", None) or ()
    calls: list[ToolCall] = []

    for item in raw:
        function = getattr(item, "function", None)
        if function is None:
            continue
        arguments = getattr(function, "arguments", "") or "{}"
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            # A model emitting malformed JSON is an ordinary event, not an
            # exceptional one, so it gets a domain error the agent can retry.
            raise LLMResponseError(
                f"the model returned unparseable arguments for tool {function.name!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMResponseError(f"tool arguments must be an object, got {type(parsed).__name__}")
        calls.append(
            ToolCall(
                id=getattr(item, "id", "") or "",
                name=function.name,
                arguments=parsed,
            )
        )

    return tuple(calls)


def _read_usage(response: Any) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()

    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) if details is not None else 0

    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cache_read_tokens=cached or 0,
    )
