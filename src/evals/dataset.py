"""Questions, and the splits they belong to.

A thin model on purpose. Loading Spider and BIRD -- downloading them, verifying
checksums, converting SQLite to Postgres, and proving the conversion preserved
every gold result -- is a separate piece of work with its own failure modes.
This is the shape everything downstream agrees on, so the harness can be built
and tested before any of that exists.

**Splits are data, not a runtime shuffle.** EVALUATION.md section 2 requires
them committed as a file. A split computed at run time by seeding a shuffle is
reproducible only as long as nobody changes the question count, the sort order,
or the Python version -- and when it silently changes, the held-out set has
been trained against and no one can tell.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Split(StrEnum):
    """Per EVALUATION.md section 2. `HELD_OUT` is reported; `DEV` is iterated on."""

    TRAIN = "train"
    DEV = "dev"
    HELD_OUT = "held-out"
    SMOKE = "smoke"


@dataclass(frozen=True, slots=True)
class Question:
    """One benchmark question and its reference answer."""

    question_id: str
    question: str
    gold_sql: str
    dataset: str = "default"
    split: Split = Split.DEV
    db_id: str = ""
    """The benchmark's own database identifier, where it has one.

    Spider and BIRD ship many databases per split, and a question is only
    meaningful against its own. Carried through so the runner can refuse to
    evaluate a question against the wrong schema rather than scoring the
    resulting nonsense.
    """


def load_questions(path: Path, *, split: Split | None = None) -> list[Question]:
    """Read questions from JSONL, one object per line.

    JSONL rather than a single JSON array so a partially written file still
    yields the questions before the truncation, and so a large split does not
    have to be held in memory twice to parse.

    Raises:
        ValueError: a line is missing a required field, naming the line number.
            Failing loudly matters here -- a question silently dropped for a
            typo'd key changes the denominator of every score computed from it.
    """
    questions: list[Question] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number} is not valid JSON: {exc.msg}") from exc

        missing = {"question_id", "question", "gold_sql"} - raw.keys()
        if missing:
            raise ValueError(f"{path}:{number} is missing {sorted(missing)}")

        question = Question(
            question_id=str(raw["question_id"]),
            question=str(raw["question"]),
            gold_sql=str(raw["gold_sql"]),
            dataset=str(raw.get("dataset", "default")),
            split=Split(raw.get("split", Split.DEV)),
            db_id=str(raw.get("db_id", "")),
        )
        if split is None or question.split is split:
            questions.append(question)

    _assert_unique(questions, path)
    return questions


def _assert_unique(questions: Sequence[Question], path: Path) -> None:
    """Duplicate ids would collide as artifact filenames.

    Two questions writing to the same file means one silently overwrites the
    other, the run reports fewer questions than it was given, and the
    difference looks like questions that were skipped.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for question in questions:
        if question.question_id in seen:
            duplicates.add(question.question_id)
        seen.add(question.question_id)

    if duplicates:
        raise ValueError(f"{path} has duplicate question_id(s): {sorted(duplicates)}")


def write_questions(path: Path, questions: Sequence[Question]) -> None:
    """Write a split file. Used to commit splits, and by tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for question in questions:
            handle.write(
                json.dumps(
                    {
                        "question_id": question.question_id,
                        "question": question.question,
                        "gold_sql": question.gold_sql,
                        "dataset": question.dataset,
                        "split": question.split.value,
                        "db_id": question.db_id,
                    }
                )
                + "\n"
            )


def iter_batched(questions: Sequence[Question], size: int) -> Iterator[list[Question]]:
    """Fixed-size batches, for bounded parallelism."""
    for start in range(0, len(questions), size):
        yield list(questions[start : start + size])


__all__ = ["Question", "Split", "iter_batched", "load_questions", "write_questions"]
