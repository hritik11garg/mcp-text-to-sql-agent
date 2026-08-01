"""What a run records, and how a spent token budget stops being a lost run.

Two requirements shape this, and the second is the one that shaped the design.

**EVALUATION.md section 3 requires per-question artifacts.** Aggregate scores
without them cannot be debugged: "61% execution accuracy" tells you nothing
about *which* questions failed or why, and re-running to find out costs the
budget again.

**Free-tier models cap tokens per model per day.** A 200-question run spans
most of a daily budget, so hitting a cap at question 140 is an ordinary
operating condition rather than an incident. If that loses 140 questions of
work, the harness is unusable on the hardware this project actually runs on.

So every question is written as it completes, and a run resumes by reading what
is already on disk. The corollary is the check in :meth:`RunStore.resume`: a
resumed run must have the *same configuration*, because a result set that is
half one model and half another is not a measurement of either, and nothing
downstream would show it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
QUESTIONS_DIR = "questions"

MAX_PERSISTED_ROWS = 50
"""Rows of each result set written to disk per question.

Artifacts are for debugging a wrong verdict, and the first rows of a
disagreement are almost always enough to see it. The bound matters for two
reasons: a run over a few hundred questions would otherwise write a copy of a
large slice of the database, and those rows are **real data in a second store**
-- the same argument that keeps result values out of the audit log
(migrations/001) and out of the logs (``LOG_RESULT_VALUES``). `results/` is
gitignored, which is a guard against publishing them, not against having them.
"""


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Everything needed to reproduce a run, recorded before it starts.

    The fields are the ones EVALUATION.md section 3 names, plus
    :attr:`config_fingerprint`, which is what makes resumption safe rather than
    merely convenient.
    """

    run_id: str
    dataset: str
    split: str
    model: str
    retriever_model_version: str
    prompt_version: str
    commit: str
    seed: int = 0
    started_at: str = ""
    notes: str = ""

    @property
    def config_fingerprint(self) -> str:
        """Hash of everything whose change would invalidate a partial run.

        Deliberately excludes ``run_id``, ``started_at`` and ``notes``: those
        differ between the first attempt and the resume of the same run, and
        including them would refuse every resumption.

        Deliberately *includes* the commit. A code change mid-run is exactly
        the kind of thing that produces a result nobody can interpret, and it
        is the easiest one to do by accident -- fix a bug, re-run, and half the
        questions were answered by the old code.
        """
        material = json.dumps(
            {
                "dataset": self.dataset,
                "split": self.split,
                "model": self.model,
                "retriever_model_version": self.retriever_model_version,
                "prompt_version": self.prompt_version,
                "commit": self.commit,
                "seed": self.seed,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "config_fingerprint": self.config_fingerprint}


@dataclass(frozen=True, slots=True)
class QuestionArtifact:
    """One question's full record.

    Written whether the question succeeded or failed. A run that persists only
    successes cannot answer the question a failure analysis asks.
    """

    question_id: str
    question: str
    gold_sql: str
    generated_sql: str | None = None
    matched: bool | None = None
    verdict: str = ""
    failure_category: str = "none"
    validation_attempts: int = 0
    error_type: str | None = None
    error_message: str = ""
    recall_at_k: dict[int, float] = field(default_factory=dict)
    gold_element_count: int = 0
    unresolved_references: int = 0
    duration_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    answering_model: str = ""
    """Which model actually answered.

    Not the configured model: a fallback chain switches on a 429, so a run can
    span two models without anything else noticing. Recorded per question
    because that is the granularity at which it varies.
    """

    gold_rows: list[list[Any]] = field(default_factory=list)
    predicted_rows: list[list[Any]] = field(default_factory=list)
    rows_truncated: bool = False

    def bounded(self, limit: int = MAX_PERSISTED_ROWS) -> QuestionArtifact:
        """A copy with result sets clipped for storage."""
        truncated = len(self.gold_rows) > limit or len(self.predicted_rows) > limit
        return replace(
            self,
            gold_rows=self.gold_rows[:limit],
            predicted_rows=self.predicted_rows[:limit],
            rows_truncated=truncated,
        )


class RunStore:
    """One directory per run: a manifest, and a file per question.

    A file per question rather than one appended log, because appending is only
    crash-safe if every writer is careful and a partially written last line is
    indistinguishable from a question that failed. A whole-file write is atomic
    enough for this, and reading the directory *is* the resume state -- there
    is no separate progress file to fall out of step with reality.
    """

    def __init__(self, root: Path, manifest: RunManifest) -> None:
        self._root = root / manifest.run_id
        self._questions = self._root / QUESTIONS_DIR
        self._manifest = manifest

    @property
    def root(self) -> Path:
        return self._root

    @property
    def manifest(self) -> RunManifest:
        return self._manifest

    def start(self) -> None:
        """Create the run directory and write the manifest.

        Written *before* the first question, so a run interrupted at question 1
        still records what it was trying to do.
        """
        self._questions.mkdir(parents=True, exist_ok=True)
        path = self._root / MANIFEST_NAME
        if not path.exists():
            path.write_text(json.dumps(self._manifest.to_dict(), indent=2) + "\n", encoding="utf-8")

    def resume(self) -> frozenset[str]:
        """Question ids already recorded, after checking the config still matches.

        Raises:
            ValueError: the existing manifest describes a different
                configuration. Refusing is the point -- silently continuing
                would produce a results directory that is half one model and
                half another, and every number computed from it would be a
                weighted average of two things nobody meant to average.
        """
        path = self._root / MANIFEST_NAME
        if not path.exists():
            return frozenset()

        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("config_fingerprint") != self._manifest.config_fingerprint:
            raise ValueError(
                f"{self._root} holds a run with a different configuration "
                f"({existing.get('config_fingerprint')} vs "
                f"{self._manifest.config_fingerprint}). Resuming would mix two "
                f"configurations into one score. Use a new --run-id, or delete "
                f"the directory if the earlier run is not worth keeping."
            )

        done = frozenset(p.stem for p in self._questions.glob("*.json"))
        if done:
            logger.info("resuming %s: %d question(s) already recorded", self._root, len(done))
        return done

    def record(self, artifact: QuestionArtifact) -> None:
        path = self._questions / f"{artifact.question_id}.json"
        payload = asdict(artifact.bounded())
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")

    def artifacts(self) -> list[QuestionArtifact]:
        """Every recorded question, for aggregation and failure analysis."""
        loaded: list[QuestionArtifact] = []
        for path in sorted(self._questions.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["recall_at_k"] = {int(k): v for k, v in (data.get("recall_at_k") or {}).items()}
            loaded.append(QuestionArtifact(**data))
        return loaded

    def write_summary(self, summary: dict[str, Any]) -> Path:
        """The machine-readable half of EVALUATION.md section 3's requirement."""
        path = self._root / "summary.json"
        path.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        return path


def current_commit() -> str:
    """The commit under test, or ``unknown`` outside a repository.

    Failing softly rather than raising: a benchmark run from a tarball is
    unusual but not wrong, and refusing to run at all would be a poor trade for
    one provenance field. It is recorded as ``unknown`` so a reader can see the
    number is unattributable rather than assume it was never checked.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - dev tool, PATH is fine here
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip() or "unknown"


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


__all__ = [
    "MAX_PERSISTED_ROWS",
    "QuestionArtifact",
    "RunManifest",
    "RunStore",
    "current_commit",
    "new_run_id",
]
