"""The reference answers, as the conversion left them.

A split file holds the benchmark's own SQL, which for Spider and BIRD is
SQLite. The eval runs against PostgreSQL. Something has to bridge those, and
where that bridging happens decides whether a score can be trusted:

**The harness does not transpile.** It reads a file that
``benchmark.load verify`` wrote, containing the exact statement each gold query
became *and the outcome of comparing its results against SQLite*. Re-deriving
the PostgreSQL form here would mean an edit to the transpiler changed every
reference answer with nothing re-checking it against the original engine --
which is the one thing verification exists to prevent. Running what was
verified is the whole point of having verified it.

**Unscoreable questions are dropped here, loudly, and counted.** A gold query
that cannot run on PostgreSQL, or whose ``LIMIT`` cut a tie so several answers
are equally correct, has no score to give. Leaving it in the denominator does
not make a run more honest -- it makes every accuracy figure lower by an amount
that has nothing to do with the model. The count and the reasons travel with
the summary so the exclusion is reported rather than merely performed.

The file is the interface, deliberately: :mod:`evals` does not import
:mod:`benchmark`. The harness is meant to run against any corpus that can
produce this shape, and a direct import would make the loader a dependency of
every eval.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from evals.dataset import Question

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GoldEntry:
    """One question's verified reference answer."""

    question_id: str
    sql: str
    outcome: str
    scoreable: bool
    db_id: str = ""
    schema: str = ""


@dataclass(frozen=True, slots=True)
class Applied:
    """The result of resolving a split against a verification run."""

    questions: list[Question] = field(default_factory=list)
    """Scoreable questions, carrying PostgreSQL gold SQL."""

    excluded: dict[str, int] = field(default_factory=dict)
    """Counts by outcome, for the questions that were dropped."""

    schemas: dict[str, str] = field(default_factory=dict)
    """``db_id`` to the PostgreSQL schema it was converted into."""

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "scoreable": len(self.questions),
            "excluded": self.excluded_total,
            "excluded_by_outcome": dict(sorted(self.excluded.items())),
        }


def load_verified_gold(path: Path) -> dict[str, GoldEntry]:
    """Read the JSONL ``benchmark.load verify --emit-gold`` produced.

    Raises:
        ValueError: a line is malformed or a ``question_id`` repeats, naming
            the line. A duplicate would silently pick one of two reference
            answers, and which one depends on file order.
    """
    entries: dict[str, GoldEntry] = {}

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number} is not valid JSON: {exc.msg}") from exc

        missing = {"question_id", "outcome", "scoreable"} - raw.keys()
        if missing:
            raise ValueError(f"{path}:{number} is missing {sorted(missing)}")

        entry = GoldEntry(
            question_id=str(raw["question_id"]),
            sql=str(raw.get("sql", "")),
            outcome=str(raw["outcome"]),
            scoreable=bool(raw["scoreable"]),
            db_id=str(raw.get("db_id", "")),
            schema=str(raw.get("schema", "")),
        )
        if entry.scoreable and not entry.sql.strip():
            # Scoreable means "ran on both engines and the results were
            # compared", so a blank statement is a contradiction in the file
            # rather than a missing optional field.
            raise ValueError(
                f"{path}:{number} marks {entry.question_id} scoreable but carries no SQL"
            )
        if entry.question_id in entries:
            raise ValueError(f"{path}:{number} repeats question_id {entry.question_id!r}")

        entries[entry.question_id] = entry

    return entries


def apply_verified_gold(
    questions: Sequence[Question],
    gold: Mapping[str, GoldEntry],
) -> Applied:
    """Swap in verified gold SQL and drop what cannot be scored.

    Raises:
        ValueError: a question in the split has no entry, listing examples.
            This is the failure that must not be tolerated: an unverified
            question is one nobody has checked the conversion against, and
            scoring against it would report a number over data of unknown
            fidelity. Verify the whole split, or narrow the split.
    """
    kept: list[Question] = []
    excluded: dict[str, int] = {}
    schemas: dict[str, str] = {}
    unverified: list[str] = []

    for question in questions:
        entry = gold.get(question.question_id)
        if entry is None:
            unverified.append(question.question_id)
            continue

        if entry.schema:
            schemas.setdefault(entry.db_id or question.db_id, entry.schema)

        if not entry.scoreable:
            excluded[entry.outcome] = excluded.get(entry.outcome, 0) + 1
            continue

        kept.append(replace(question, gold_sql=entry.sql))

    if unverified:
        raise ValueError(
            f"{len(unverified)} question(s) in this split were never verified, "
            f"so the conversion behind them is unchecked: {_sample(unverified)}. "
            f"Re-run `benchmark.load verify --emit-gold` over the whole split."
        )

    logger.info(
        "verified gold applied: %d scoreable, %d excluded (%s)",
        len(kept),
        sum(excluded.values()),
        ", ".join(f"{outcome} {count}" for outcome, count in sorted(excluded.items())) or "none",
    )
    return Applied(questions=kept, excluded=excluded, schemas=schemas)


def _sample(ids: Sequence[str], limit: int = 5) -> str:
    return ", ".join(ids[:limit]) + (" ..." if len(ids) > limit else "")


__all__ = ["Applied", "GoldEntry", "apply_verified_gold", "load_verified_gold"]
