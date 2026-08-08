"""What the recorded runs would have cost, at published prices.

**Token counts are measured; prices are not.** Every run writes
``input_tokens`` and ``output_tokens`` per question, and those numbers are
facts about work that happened. The prices below are a snapshot of published
list rates on one day, and they are the only part of this module that can
quietly become wrong.

That asymmetry is the reason this is code rather than a table in a document. A
hand-maintained cost table rots -- it is the drift risk the project has already
materialised several times, and nothing detects it. Here the perishable half is
one dated dictionary, the durable half is read from the artifacts, and
``python -m evals.cost`` regenerates the table. Repricing is an edit to
:data:`PRICES` and a command, not an editing pass over prose.

**Money is :class:`~decimal.Decimal`, not ``float``.** Sub-cent figures are the
normal case here -- a full 921-question run costs about seventeen cents on the
model that produced it -- and binary floating point is the wrong tool for
values that get rounded and summed for publication.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

PRICES_AS_OF: Final = "2026-08-08"
"""The day these rates were recorded. Print it next to every figure.

A cost table with no date is a claim that prices do not move, which is the one
thing everybody knows to be false. The vendor lists consulted carried their own
``lastUpdated`` of 2026-08-07; this is the day they were read and written down,
which is the date a reader can check a figure against.
"""

PRICING_BASIS: Final = "standard on-demand requests"
"""**Which tariff these rates are, and naming it is the cheapest correction here.**

Every price below is the ordinary synchronous per-request rate. Providers
publish several others for the same model, and the differences are not small:

- **Batch / asynchronous** tiers are commonly **half** the standard rate, and
  this workload is a perfect fit for one -- 921 independent questions with no
  latency requirement. A reader who cares about cost should assume these
  figures can be roughly halved by not caring about latency.
- **Cached input** is cheaper again where it is offered. This project builds a
  stable prompt prefix precisely to be cacheable and plumbs
  ``cache_read_tokens`` through, but has never observed it non-zero against a
  real provider, so no cache discount is claimed.
- **Priority / provisioned** tiers cost *more*.

None of those are modelled. Quoting a batch price for a run made synchronously
would understate what was spent; quoting a standard price at a reader who
batches overstates what they will pay. So the tariff is named rather than
implied.
"""

PER_MILLION: Final = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Price:
    """USD per one million tokens."""

    provider: str
    model: str
    input_usd: Decimal
    output_usd: Decimal

    def of(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            Decimal(input_tokens) * self.input_usd + Decimal(output_tokens) * self.output_usd
        ) / PER_MILLION


def _p(provider: str, model: str, inp: str, out: str) -> Price:
    return Price(provider, model, Decimal(inp), Decimal(out))


PRICES: Final[tuple[Price, ...]] = (
    # The model that actually answered every question in BENCHMARKS section 1.1.
    # Its row is the only one here that is not a hypothetical.
    _p("Groq", "openai/gpt-oss-120b", "0.15", "0.60"),
    # The rest of the configured fallback chain, in chain order. Worth pricing
    # because a spent daily cap moves the run onto these, which is how a blended
    # accuracy row happens -- and the blend has a cost as well as a score.
    _p("Groq", "qwen/qwen3.6-27b", "0.60", "3.00"),
    _p("Groq", "llama-3.3-70b-versatile", "0.59", "0.79"),
    _p("Groq", "llama-3.1-8b-instant", "0.05", "0.08"),
    # A cheaper sibling, as the practical floor for hosted inference.
    _p("Groq", "openai/gpt-oss-20b", "0.075", "0.30"),
    # Other providers, as a spread rather than a survey: one cheap, two mid,
    # two frontier. A longer table would rot faster and say no more.
    _p("DeepSeek", "DeepSeek V3.2", "0.28", "0.42"),
    _p("Google", "Gemini 3.1 Flash-Lite", "0.25", "1.50"),
    _p("OpenAI", "GPT-4.1 mini", "0.40", "1.60"),
    _p("Google", "Gemini 3 Pro", "2.00", "12.00"),
    _p("OpenAI", "GPT-4.1", "2.00", "8.00"),
    _p("Anthropic", "Claude Sonnet 5", "2.00", "10.00"),
    _p("Anthropic", "Claude Opus 5", "5.00", "25.00"),
    # Self-hosted. Zero marginal token cost, and deliberately in the table: it
    # is the honest floor, and it is what the electricity-and-hardware column
    # this table does not have would push back on.
    _p("local", "Ollama / vLLM (self-hosted)", "0", "0"),
)


@dataclass(frozen=True, slots=True)
class RunUsage:
    """One run's measured token spend."""

    run_id: str
    questions: int
    input_tokens: int
    output_tokens: int
    baseline: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def load_usage(results_root: Path) -> list[RunUsage]:
    """Read every run that left a ``summary.json``.

    Runs that did not finish far enough to write one are **absent**, and that
    absence is a real understatement rather than a rounding error: the
    2026-08-05 attempt answered 395 questions and left nothing behind. A caller
    publishing a total should say it is a lower bound.
    """
    runs: list[RunUsage] = []
    for directory in sorted(p for p in results_root.iterdir() if p.is_dir()):
        summary = directory / "summary.json"
        if not summary.is_file():
            continue
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt summary is not a reason to report no costs at all.
            continue
        manifest = data.get("manifest") or {}
        runs.append(
            RunUsage(
                run_id=directory.name,
                questions=int(data.get("total", 0)),
                input_tokens=int(data.get("input_tokens", 0)),
                output_tokens=int(data.get("output_tokens", 0)),
                baseline=str(manifest.get("baseline") or data.get("baseline") or "unknown"),
            )
        )
    return runs


def totals(runs: Iterable[RunUsage]) -> tuple[int, int]:
    input_tokens = output_tokens = 0
    for run in runs:
        input_tokens += run.input_tokens
        output_tokens += run.output_tokens
    return input_tokens, output_tokens


def usd(amount: Decimal) -> str:
    """Two decimals above a cent, four below -- because the interesting figures
    here are fractions of a cent and ``$0.00`` would erase the comparison."""
    return f"${amount:.4f}" if amount < Decimal("0.01") else f"${amount:.2f}"


def markdown_table(one_run: tuple[int, int], everything: tuple[int, int]) -> str:
    """The published table, regenerated rather than maintained.

    The caption is part of the output on purpose: a table copied into a
    document without its date and its tariff is a table that will be quoted
    against the wrong tariff on a later date.
    """
    lines = [
        f"*Standard on-demand rates as of {PRICES_AS_OF}. Batch tiers are commonly "
        f"half these rates and are not modelled.*",
        "",
        "| Provider | Model | Input $/1M | Output $/1M | One full split (921 q) "
        "| Everything recorded |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for price in PRICES:
        lines.append(
            f"| {price.provider} | `{price.model}` | {price.input_usd} | {price.output_usd} "
            f"| **{usd(price.of(*one_run))}** | {usd(price.of(*everything))} |"
        )
    return "\n".join(lines)


def main() -> int:  # pragma: no cover - thin CLI over the tested functions
    root = Path("results")
    if not root.is_dir():
        print("no results/ directory; nothing to price")
        return 1

    runs = load_usage(root)
    everything = totals(runs)
    full = next((r for r in runs if r.run_id == "spider-full-20260806"), None)
    one_run = (full.input_tokens, full.output_tokens) if full else everything

    print(f"prices as of {PRICES_AS_OF}, {PRICING_BASIS} -- verify before quoting")
    print("batch tiers are commonly half these rates and are not modelled\n")
    print(f"{'run':38} {'q':>5} {'in':>10} {'out':>9}  baseline")
    for run in runs:
        print(
            f"{run.run_id:38} {run.questions:>5} {run.input_tokens:>10,} "
            f"{run.output_tokens:>9,}  {run.baseline}"
        )
    print(f"\ntotal: {everything[0]:,} in / {everything[1]:,} out over {len(runs)} run(s)")
    if full is not None:
        ratio = Decimal(sum(everything)) / Decimal(full.input_tokens + full.output_tokens)
        print(f"re-run multiplier: {ratio:.2f}x one full reproduction\n")
    print(markdown_table(one_run, everything))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "PRICES",
    "PRICES_AS_OF",
    "PRICING_BASIS",
    "Price",
    "RunUsage",
    "load_usage",
    "markdown_table",
    "totals",
    "usd",
]
