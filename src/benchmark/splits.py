"""Which databases are trained on, iterated on, and reported from.

Split **by database, never by question** (DATASETS.md section 5). A
question-level split puts the same tables and columns in train and eval, so a
fine-tuned schema linker is scored on schemas it was trained to link. The
resulting Recall@k is high and means nothing.

**Assignment is a hash of the database name, not a shuffle.** A seeded shuffle
is reproducible only while the input list is unchanged: add one database, or
load Spider and BIRD in the other order, and every subsequent database can move
to a different split. That silently trains on what used to be held out, and
nothing downstream can detect it -- the split file looks just as deterministic
as it did before.

Hashing each name independently makes membership a property of the name alone.
Adding databases never moves the ones already assigned, so a split file
regenerated a year later is a superset of the old one rather than a
rearrangement of it. The price is that proportions are approximate at small
corpus sizes, which is the cheaper mistake: a 68/12/20 split is a fine split,
and a held-out set that quietly absorbed three training databases is not a
split at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from evals.dataset import Question, Split

_BUCKETS = 10_000


@dataclass(frozen=True, slots=True)
class SplitPolicy:
    """The proportions, and the seed that pins them to particular databases."""

    train: float = 0.70
    dev: float = 0.15
    held_out: float = 0.15
    seed: str = "t2sql-v1"

    smoke: float = 0.025
    """Smoke is a *sub-band of dev*, expressed as a fraction of the whole corpus.

    It was originally a count -- "the five lowest-bucket dev databases" -- which
    reads as stable and is not. Rank within a set is a property of the set, so
    adding a database with a lower bucket displaces one that was already in
    smoke, and the per-commit regression check silently starts measuring
    different databases. A test caught it; the fix is that membership of every
    split, smoke included, depends on nothing but the database's own name.

    At ~200 databases this is ~5, which is what DATASETS.md section 5 asks for.
    """

    def __post_init__(self) -> None:
        total = self.train + self.dev + self.held_out
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"split proportions must sum to 1.0, got {total}")
        if min(self.train, self.dev, self.held_out) < 0:
            raise ValueError("split proportions must be non-negative")
        if not 0.0 <= self.smoke <= self.dev:
            raise ValueError(
                f"smoke ({self.smoke}) must be between 0 and the dev share ({self.dev}) -- "
                f"it is carved out of dev, not added alongside it"
            )


def bucket(db_id: str, *, seed: str) -> int:
    """A database's stable position in ``[0, _BUCKETS)``.

    blake2b rather than :func:`hash`, which is randomised per process by
    ``PYTHONHASHSEED`` and would put a database in a different split on every
    run -- the exact failure this module exists to prevent, arriving through the
    one function that looks like it cannot fail.
    """
    digest = hashlib.blake2b(f"{seed}:{db_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % _BUCKETS


def assign(db_ids: Iterable[str], *, policy: SplitPolicy | None = None) -> dict[str, Split]:
    """Map each database to a split.

    The bands are laid end to end over ``[0, _BUCKETS)`` and a database's
    bucket decides which one it lands in. ``SMOKE`` occupies the *start of the
    dev band* -- never held-out, which would mean the per-commit regression
    check touches the set reserved for reported numbers, and never train, whose
    schemas the retriever has been fitted to.

    Every boundary is a constant, so membership depends only on the database's
    own name. That is the whole point: no ranking, no counting, nothing that
    could make one database's split depend on which others were loaded.
    """
    rules = policy or SplitPolicy()
    smoke_edge = rules.train * _BUCKETS + rules.smoke * _BUCKETS
    train_edge = rules.train * _BUCKETS
    dev_edge = train_edge + rules.dev * _BUCKETS

    assignment: dict[str, Split] = {}
    for db_id in sorted(set(db_ids)):
        position = bucket(db_id, seed=rules.seed)
        if position < train_edge:
            assignment[db_id] = Split.TRAIN
        elif position < smoke_edge:
            assignment[db_id] = Split.SMOKE
        elif position < dev_edge:
            assignment[db_id] = Split.DEV
        else:
            assignment[db_id] = Split.HELD_OUT

    return assignment


def apply(questions: Sequence[Question], assignment: dict[str, Split]) -> list[Question]:
    """Stamp each question with its database's split.

    Raises:
        KeyError: A question names a database the assignment does not cover.
            Defaulting to ``DEV`` would be the convenient choice and would put
            unassigned databases into the set people iterate against, which is
            how a held-out database ends up being tuned on.
    """
    stamped: list[Question] = []
    for question in questions:
        if question.db_id not in assignment:
            raise KeyError(
                f"question {question.question_id!r} names database "
                f"{question.db_id!r}, which has no split assignment"
            )
        stamped.append(replace(question, split=assignment[question.db_id]))
    return stamped


def summarise(
    assignment: dict[str, Split], questions: Sequence[Question]
) -> dict[str, dict[str, int]]:
    """Databases and questions per split -- the table DATASETS.md section 5 holds."""
    tally: dict[str, dict[str, int]] = {
        split.value: {"databases": 0, "questions": 0} for split in Split
    }
    for split in assignment.values():
        tally[split.value]["databases"] += 1
    for question in questions:
        assigned = assignment.get(question.db_id)
        if assigned is not None:
            tally[assigned.value]["questions"] += 1
    return tally


__all__ = ["SplitPolicy", "apply", "assign", "bucket", "summarise"]
