"""Pricing measured token counts, and the ways that goes wrong quietly.

The arithmetic is trivial. What is not trivial is that this module publishes
money: a figure that is silently wrong here ends up in a document, and a cost
table is exactly the kind of thing nobody re-derives before quoting.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from evals.cost import (
    PRICES,
    PRICES_AS_OF,
    PRICING_BASIS,
    Price,
    load_usage,
    markdown_table,
    totals,
    usd,
)


def write_run(root: Path, name: str, *, total: int, inp: int, out: int, baseline: str) -> None:
    directory = root / name
    (directory / "questions").mkdir(parents=True)
    (directory / "summary.json").write_text(
        json.dumps(
            {
                "total": total,
                "input_tokens": inp,
                "output_tokens": out,
                "manifest": {"baseline": baseline},
            }
        ),
        encoding="utf-8",
    )


class TestPricing:
    def test_it_prices_per_million_tokens(self) -> None:
        price = Price("x", "m", Decimal("1.00"), Decimal("2.00"))
        assert price.of(1_000_000, 1_000_000) == Decimal("3.00")

    def test_a_free_model_costs_nothing(self) -> None:
        price = Price("local", "m", Decimal(0), Decimal(0))
        assert price.of(10**9, 10**9) == Decimal(0)

    def test_input_and_output_are_not_interchangeable(self) -> None:
        """Output is dearer than input on every hosted model here, so swapping
        the two understates a generation-heavy workload."""
        price = Price("x", "m", Decimal("0.15"), Decimal("0.60"))
        assert price.of(1_000_000, 0) != price.of(0, 1_000_000)

    def test_the_real_run_costs_what_the_published_rates_say(self) -> None:
        """The one row on this table that is not a hypothetical.

        455k in / 169k out is the measured spend of the completed full-split
        run, and `openai/gpt-oss-120b` on Groq is the model that produced it.
        """
        groq = next(p for p in PRICES if p.model == "openai/gpt-oss-120b")
        cost = groq.of(454_607, 168_560)

        assert Decimal("0.16") < cost < Decimal("0.18")

    def test_money_is_decimal_not_float(self) -> None:
        """Sub-cent values summed and rounded for publication; binary floating
        point is the wrong representation and the error compounds silently."""
        assert isinstance(PRICES[0].of(1, 1), Decimal)


class TestTheTableCannotSilentlyGoStale:
    def test_the_snapshot_date_is_recorded(self) -> None:
        """A cost table with no date claims prices do not move."""
        assert PRICES_AS_OF

    def test_the_tariff_is_named_not_implied(self) -> None:
        """Batch tiers are commonly half the standard rate, so a table that
        does not say which tariff it quotes is off by up to 2x in a direction
        the reader cannot see."""
        assert "standard" in PRICING_BASIS

    def test_the_generated_table_carries_its_own_date_and_tariff(self) -> None:
        """A table copied into a document without them will be quoted against
        the wrong tariff on a later date."""
        caption = markdown_table((1, 1), (1, 1)).splitlines()[0]

        assert PRICES_AS_OF in caption
        assert "atch" in caption  # batch tiers named as unmodelled

    def test_every_price_names_its_provider(self) -> None:
        """The same model id costs different amounts on different hosts, so a
        bare model name is not enough to reprice or to check."""
        assert all(p.provider for p in PRICES)

    def test_the_model_that_produced_the_numbers_is_present(self) -> None:
        """If this row is ever dropped, every remaining figure is a
        hypothetical and the section stops being able to say what was spent."""
        assert any(p.model == "openai/gpt-oss-120b" for p in PRICES)

    def test_the_configured_fallback_chain_is_priced(self) -> None:
        """A spent daily cap moves the run onto these, which is how a blended
        accuracy row happens — and a blend has a cost as well as a score."""
        priced = {p.model for p in PRICES}
        assert {"qwen/qwen3.6-27b", "llama-3.3-70b-versatile"} <= priced


class TestFormatting:
    @pytest.mark.parametrize(
        ("amount", "expected"),
        [(Decimal("1.234"), "$1.23"), (Decimal("0.169"), "$0.17"), (Decimal("12"), "$12.00")],
    )
    def test_it_shows_cents_above_a_cent(self, amount: Decimal, expected: str) -> None:
        assert usd(amount) == expected

    def test_it_keeps_four_places_below_a_cent(self) -> None:
        """`$0.00` would erase the comparison this table exists to make — the
        cheapest rows here really are fractions of a cent."""
        assert usd(Decimal("0.0043")) == "$0.0043"


class TestLoadingRuns:
    def test_it_reads_every_run_that_finished(self, tmp_path: Path) -> None:
        write_run(tmp_path, "a", total=10, inp=100, out=50, baseline="retrieval-only")
        write_run(tmp_path, "b", total=20, inp=200, out=90, baseline="with-validation")

        runs = load_usage(tmp_path)

        assert [r.run_id for r in runs] == ["a", "b"]
        assert totals(runs) == (300, 140)

    def test_a_run_with_no_summary_is_skipped_not_guessed(self, tmp_path: Path) -> None:
        """The 2026-08-05 attempt answered 395 questions and left no summary.

        Inventing a number for it would be worse than omitting it — but a
        caller publishing the total has to say it is a lower bound, and that is
        prose rather than something this function can enforce.
        """
        (tmp_path / "abandoned").mkdir()

        assert load_usage(tmp_path) == []

    def test_a_corrupt_summary_does_not_take_the_whole_report_down(self, tmp_path: Path) -> None:
        write_run(tmp_path, "good", total=1, inp=10, out=5, baseline="retrieval-only")
        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / "summary.json").write_text("{not json", encoding="utf-8")

        assert [r.run_id for r in load_usage(tmp_path)] == ["good"]

    def test_a_summary_missing_its_token_counts_reads_as_zero(self, tmp_path: Path) -> None:
        directory = tmp_path / "old"
        directory.mkdir()
        (directory / "summary.json").write_text('{"total": 5}', encoding="utf-8")

        assert load_usage(tmp_path)[0].total_tokens == 0

    def test_totals_of_nothing_is_zero_rather_than_an_error(self) -> None:
        assert totals([]) == (0, 0)


class TestTheGeneratedTable:
    def test_it_regenerates_rather_than_being_maintained(self) -> None:
        table = markdown_table((454_607, 168_560), (767_985, 408_931))

        assert "| Provider | Model |" in table
        assert "openai/gpt-oss-120b" in table
        assert len(table.splitlines()) == len(PRICES) + 4  # caption, blank, header, sep

    def test_the_cheapest_row_is_not_rounded_away(self) -> None:
        """A self-hosted row at $0 and a 5-cent row must stay distinguishable."""
        table = markdown_table((454_607, 168_560), (767_985, 408_931))

        assert "$0.0000" in table
