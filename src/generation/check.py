"""Prove the configured provider actually works, before anything depends on it.

    python -m generation.check
    python -m generation.check --model llama-3.1-8b-instant

One round trip, and it reports what matters for planning a run: which model
answered, how long it took, how many tokens it cost, and whether any of the
prompt was served from cache.

This exists because the alternative is discovering a bad key, a retired model
id, or a spent daily cap in the middle of a benchmark run -- where it looks
like a bug in the agent rather than a configuration problem. Model ids in
particular are not knowable in advance: providers rename and retire them, and
a wrong one fails on the first request rather than at startup.

Nothing here prints or logs the API key.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from adapters.llm.factory import build_llm_client
from core.exceptions import LLMError, TextToSQLError
from core.ports.llm import LLMClient, Message, Role
from core.settings import LLMSettings

PROBE = "Reply with exactly: OK"
"""Deliberately trivial.

The question is whether the round trip works, not whether the model is any
good -- quality is the eval harness's job. A tiny prompt also costs almost
nothing against a daily cap that may already be strained.
"""


async def probe(client: LLMClient) -> int:
    started = time.perf_counter()
    response = await client.complete(
        [Message(role=Role.USER, content=PROBE)],
        max_tokens=16,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    print(f"  model      {response.model}")
    print(f"  latency    {elapsed_ms:.0f} ms")
    print(f"  reply      {response.text.strip()[:60]!r}")
    print(
        f"  tokens     in={response.usage.input_tokens} "
        f"out={response.usage.output_tokens} "
        f"cached={response.usage.cache_read_tokens}"
    )

    if response.usage.cache_read_tokens == 0:
        # Not a failure. A one-shot probe has no prefix to hit, and several
        # providers never report the field at all.
        print("  note       no cached tokens -- expected for a single probe")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="override LLM_MODEL for this check")
    args = parser.parse_args(argv)

    try:
        settings = LLMSettings()
    except TextToSQLError as exc:
        print(f"configuration rejected: {exc}", file=sys.stderr)
        return 2

    if args.model:
        settings = settings.model_copy(update={"llm_model": args.model})

    print(f"provider     {settings.llm_provider}")
    print(f"base_url     {settings.llm_base_url or '(provider default)'}")
    print(f"model        {settings.llm_model}")
    if settings.llm_model_fallbacks:
        print(f"fallbacks    {', '.join(settings.llm_model_fallbacks)}")
    print(f"api key      {'set' if settings.llm_api_key else 'not set'}")
    print()

    try:
        client = build_llm_client(settings)
        return asyncio.run(probe(client))
    except LLMError as exc:
        print(f"provider call failed: {exc}", file=sys.stderr)
        return 1
    except TextToSQLError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
