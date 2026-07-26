"""A scripted LLM, for tests.

This is not a mock. It is a real implementation of the :class:`LLMClient` port,
which is why the agent's retry, decomposition, and self-correction logic can be
tested deterministically without a network call, an API key, or any knowledge
of a vendor SDK's internals.

Model *quality* is measured by the eval harness, never asserted in tests. See
docs/development/TESTING.md section 1.
"""

from __future__ import annotations

from collections.abc import Sequence

from core.exceptions import LLMResponseError
from core.ports.llm import LLMResponse, Message, ToolSpec, Usage


class FakeLLMClient:
    """Returns queued responses in order, and records what it was asked.

    Example:
        >>> llm = FakeLLMClient([LLMResponse(text="SELECT 1")])
        >>> llm.calls
        []
    """

    def __init__(
        self,
        responses: Sequence[LLMResponse] | None = None,
        *,
        model: str = "fake-model",
        supports_tool_calling: bool = True,
    ) -> None:
        self._responses = list(responses or [])
        self._model = model
        self._supports_tool_calling = supports_tool_calling
        self.calls: list[tuple[Message, ...]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_tool_calling(self) -> bool:
        return self._supports_tool_calling

    def queue(self, *responses: LLMResponse) -> None:
        """Append responses to be returned by subsequent calls."""
        self._responses.extend(responses)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
        timeout_ms: int | None = None,
    ) -> LLMResponse:
        self.calls.append(tuple(messages))
        if not self._responses:
            raise LLMResponseError(
                f"FakeLLMClient exhausted after {len(self.calls)} call(s) -- "
                "queue more responses, or the code under test is looping"
            )
        return self._responses.pop(0)


def text_response(text: str, *, model: str = "fake-model") -> LLMResponse:
    """Convenience builder for the common case."""
    return LLMResponse(text=text, usage=Usage(input_tokens=1, output_tokens=1), model=model)
