"""The LLM port.

The agent depends on this protocol and never on a vendor SDK, so the provider
is a runtime choice rather than a compile-time dependency.

See ADR-014 in docs/architecture/DECISIONS.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool offered to the model.

    ``description`` is not documentation -- it is the model's only signal for
    when to call the tool, which makes it a prompt. See docs/ml/PROMPTS.md.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    """Zero across repeated requests means prompt caching is silently broken."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    tool_calls: Sequence[ToolCall] = field(default_factory=tuple)
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    truncated: bool = False
    """The provider stopped at the token limit rather than at a natural end.

    Worth a field of its own because on a *reasoning* model this is the
    difference between a bug and a budget. Such models spend output tokens on
    internal reasoning before emitting any content, so a limit set too low
    returns an empty string having consumed the entire allowance -- with no
    error anywhere. Measured on Groq: ``openai/gpt-oss-120b`` needs 45 output
    tokens to answer "reply with OK", 43 of which are reasoning.
    """

    @property
    def wants_tool_call(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class LLMClient(Protocol):
    """What the agent needs from a language model. Nothing more.

    Kept deliberately small: every method here must be implementable by every
    adapter, including :class:`~adapters.llm.fake.FakeLLMClient`. A method that
    only one provider supports belongs on that adapter, not on the port.
    """

    @property
    def model(self) -> str:
        """Model identifier, recorded on spans and in benchmark rows."""
        ...

    @property
    def supports_tool_calling(self) -> bool:
        """Whether native tool calling is available.

        Free-tier and local models frequently lack it, so the agent must be
        able to fall back to prompt-based structured output rather than
        assuming the capability exists.
        """
        ...

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
        timeout_ms: int | None = None,
    ) -> LLMResponse:
        """Produce a single completion.

        Raises:
            LLMUnavailableError: provider unreachable, or failed after retries.
            LLMResponseError: responded, but the response was unusable.
        """
        ...
