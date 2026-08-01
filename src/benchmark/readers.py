"""Turning each benchmark's own question format into :class:`evals.dataset.Question`.

Two readers rather than one tolerant one. Spider and BIRD name the same field
differently -- ``query`` against ``SQL`` -- and a reader that accepts either
cannot tell "this is a BIRD file" from "this is a Spider file with a typo'd
key". Each reader states what it expects and says which file and which record
disappointed it, because a question dropped for a missing key silently changes
the denominator of every score computed from the file.

**Question ids.** BIRD ships one; Spider does not. Spider's is synthesised from
the file name and the record's position, which is stable for a given release --
and the release is pinned by the digest in ``data/artifacts.lock.json``, so an
id cannot silently come to mean a different question without the lockfile check
failing first.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.exceptions import BenchmarkError
from evals.dataset import Question

logger = logging.getLogger(__name__)


def _records(path: Path) -> list[dict[str, Any]]:
    """Load a benchmark question file, which is a single large JSON array."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read {path}: {exc}") from exc

    if not isinstance(payload, list):
        raise BenchmarkError(f"{path} should hold a JSON array of questions")
    return [record for record in payload if isinstance(record, dict)]


def _require(record: dict[str, Any], key: str, *, path: Path, index: int) -> str:
    value = record.get(key)
    if value is None or not str(value).strip():
        raise BenchmarkError(f"{path}[{index}] has no {key!r}")
    return str(value)


def read_spider(path: Path, *, dataset: str = "spider") -> list[Question]:
    """Read ``train_spider.json`` / ``dev.json``.

    Spider records carry ``db_id``, ``question`` and ``query`` (the gold SQL),
    alongside parsed forms this project does not use -- ``sql`` holds Spider's
    own AST, which exists for its evaluation script and would be a second,
    disagreeing source of truth for what the question asks.
    """
    questions = [
        Question(
            question_id=f"{dataset}:{path.stem}:{index:05d}",
            question=_require(record, "question", path=path, index=index),
            gold_sql=_require(record, "query", path=path, index=index),
            dataset=dataset,
            db_id=_require(record, "db_id", path=path, index=index),
        )
        for index, record in enumerate(_records(path))
    ]
    logger.info("read %d Spider questions from %s", len(questions), path.name)
    return questions


def read_bird(path: Path, *, dataset: str = "bird") -> list[Question]:
    """Read BIRD's ``dev.json`` / ``train.json``.

    BIRD's gold SQL is under ``SQL``. Its ``evidence`` field -- a human-written
    hint naming the columns involved -- is **not** carried into the question.
    Feeding it to the generator would measure the model plus an oracle, and the
    published BIRD numbers this project would be compared against are reported
    both ways; conflating them is the easiest way to publish an accuracy that
    looks like a breakthrough and is a leak.
    """
    questions = []
    for index, record in enumerate(_records(path)):
        raw_id = record.get("question_id")
        question_id = f"{dataset}:{raw_id}" if raw_id is not None else f"{dataset}:{index:05d}"
        questions.append(
            Question(
                question_id=question_id,
                question=_require(record, "question", path=path, index=index),
                gold_sql=_require(record, "SQL", path=path, index=index),
                dataset=dataset,
                db_id=_require(record, "db_id", path=path, index=index),
            )
        )
    logger.info("read %d BIRD questions from %s", len(questions), path.name)
    return questions


READERS = {"spider": read_spider, "bird": read_bird}


def find_databases(root: Path) -> dict[str, Path]:
    """Locate every ``<db_id>/<db_id>.sqlite`` under a benchmark's database folder.

    Both benchmarks use that layout. Matching on the *directory* name rather
    than globbing for ``*.sqlite`` matters: some database folders ship more than
    one file, and picking whichever the filesystem returned first would convert
    a different database than the questions refer to.
    """
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        for suffix in (".sqlite", ".sqlite3", ".db"):
            candidate = entry / f"{entry.name}{suffix}"
            if candidate.is_file():
                found[entry.name] = candidate
                break
        else:
            logger.debug("no database file for %s", entry.name)
    return found


__all__ = ["READERS", "find_databases", "read_bird", "read_spider"]
