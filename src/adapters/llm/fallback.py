"""An ordered chain of models, tried until one answers.

Free tiers enforce **per-model** daily token caps, so exhausting one model is
an ordinary operating condition rather than an incident -- and the same request
against a different model usually succeeds immediately. Without this, a spent
cap ends a benchmark run halfway through and the partial result is discarded.

This is itself an implementation of :class:`~core.ports.llm.LLMClient`, so
nothing upstream knows it exists. Composition, not a special case inside the
adapter: the fallback policy has nothing to do with speaking HTTP to a
provider, and putting it there would make the one class that does I/O also the
class that owns retry semantics.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from core.exceptions import LLMQuotaExceededError, LLMUnavailableError
from core.ports.llm import LLMClient, LLMResponse, Message, ToolSpec

logger = logging.getLogger(__name__)


class FallbackLLMClient:
    """Tries each client in order, moving on when one is out of quota.

    Only :class:`LLMQuotaExceededError` advances the chain. A malformed
    response or a bad request is **not** retried elsewhere: the same request
    would fail the same way against every model, and burning the whole chain to
    discover that wastes quota on models that were still usable.

    Args:
        clients: In preference order. The first is the one you actually want;
            the rest exist so a spent cap does not end the run.
    """

    def __init__(self, clients: Sequence[LLMClient]) -> None:
        if not clients:
            raise ValueError("a fallback chain needs at least one client")
        self._clients = tuple(clients)
        self._active = 0

    @property
    def model(self) -> str:
        """The model currently in use, not the one originally configured.

        Benchmark rows record the model that produced the answer. If this
        reported the head of the chain, a run that silently fell back would be
        attributed to a model that never saw the question.
        """
        return self._clients[self._active].model

    @property
    def supports_tool_calling(self) -> bool:
        """True only if **every** model in the chain supports it.

        The conservative reading is the correct one: a chain that advertises a
        capability its fallbacks lack would fail after the first exhaustion,
        which is precisely when nobody is watching.
        """
        return all(client.supports_tool_calling for client in self._clients)

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(client.model for client in self._clients)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
        timeout_ms: int | None = None,
    ) -> LLMResponse:
        exhausted: list[str] = []

        # Start from the last known-good client rather than the head. Once a
        # model's daily cap is spent it stays spent, and re-testing it on every
        # request would add a guaranteed-failing round trip to each one.
        for index in range(self._active, len(self._clients)):
            client = self._clients[index]
            try:
                response = await client.complete(
                    messages, tools=tools, max_tokens=max_tokens, timeout_ms=timeout_ms
                )
            except LLMQuotaExceededError:
                exhausted.append(client.model)
                logger.warning(
                    "model out of quota; advancing the fallback chain",
                    extra={
                        "exhausted_model": client.model,
                        "remaining": len(self._clients) - index - 1,
                    },
                )
                continue

            if index != self._active:
                logger.info(
                    "fallback model in use",
                    extra={"model": client.model, "skipped": exhausted},
                )
                self._active = index
            return response

        raise LLMUnavailableError(
            f"every model in the fallback chain is rate limited or out of quota: "
            f"{', '.join(exhausted) or 'none configured'}"
        )
