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
is already on disk. Two corollaries, both in :meth:`RunStore.resume`.

A resumed run must have the *same configuration*, because a result set that is
half one model and half another is not a measurement of either, and nothing
downstream would show it.

And a question is resumed as done only if the system under test actually
answered it. The whole design above exists so that hitting a cap at question
140 costs nothing -- which it did not, because the questions the cap *failed*
were recorded like any other and never retried. A budget spent failing was
still a lost run; it just looked like a complete one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from evals.taxonomy import is_infrastructure

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

    baseline: str = ""
    """Which EVALUATION.md section 4 configuration produced this run.

    In the fingerprint, and it has to be: the baselines exist to be *compared*,
    so a results directory holding half of one and half of another is the one
    mixture nobody would ever mean to make. Defaulted to empty so a manifest
    written before baselines existed still fingerprints as it did.
    """

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

        And the baseline, for the same reason one step further out: `model`
        catches "answered by a different model", but two baselines share a model
        and differ in whether the schema was retrieved or handed over whole.
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
                "baseline": self.baseline,
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


_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9_-]")
"""Everything outside this set is replaced, **including the dot**.

Keeping `.` would be harmless on the evidence -- `..` cannot traverse without a
separator, and separators are already gone. But that is an argument a reader has
to reconstruct, and a rule whose safety depends on a second rule holding is the
kind that breaks when one of them is relaxed. Dropping the dot makes `..`
unrepresentable rather than merely inert, at the cost of `bird.dev.1` reading as
`bird-dev-1`.
"""

MAX_FILENAME_STEM = 80


def artifact_filename(question_id: str) -> str:
    """A legal, unique filename for one question's artifact.

    **A question id is benchmark-supplied data, not a path component.** Spider's
    ids are synthesised as ``spider:dev:00000``, and a colon is not a legal
    filename character on Windows -- it is the drive separator and the alternate
    data stream marker, so ``open()`` fails with ``Invalid argument`` and the
    run dies at question one. That is how this was found: the first real corpus
    to reach this code aborted immediately.

    The security half is the same fact from the other side. BIRD ships its own
    ids, and a corpus is a file an operator downloaded. An id of
    ``../../../../etc/cron.d/x`` used verbatim as a path component writes
    outside the results directory entirely *(CWE-22, path traversal;
    Integrity)*. Substituting every character outside
    :data:`_UNSAFE_IN_FILENAME`'s set removes the separators and the ``..``
    both -- the same position :mod:`benchmark.acquire` already takes for
    archive members.

    **The hash suffix is what makes the substitution safe.** Sanitising alone is
    lossy: ``a:b`` and ``a/b`` both become ``a-b``, so two questions would write
    to one file, the run would report fewer questions than it was given, and the
    difference would look like questions that were skipped. Eight hex characters
    of the *original* id restore uniqueness, and truncating the readable stem
    stays safe for the same reason.

    Names are not reversible, so :meth:`RunStore.resume` reads ids from inside
    the files instead of off their names.
    """
    stem = _UNSAFE_IN_FILENAME.sub("-", question_id)[:MAX_FILENAME_STEM]
    digest = hashlib.sha256(question_id.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}.json"


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
        """Question ids already *answered*, after checking the config still matches.

        Answered, not recorded -- and the difference is what makes a run
        survive the daily token cap this module's docstring is about.

        An artifact whose ``error_type`` is an infrastructure failure records
        that the system under test was never asked: the provider was out of
        budget, the schema was not indexed, the harness itself broke. Treating
        that file as "done" retires the question permanently, so a run spanning
        several daily budgets converges on a directory where most questions
        were never attempted and nothing says so. The first full-split attempt
        did exactly that with 308 of them.

        These questions are therefore re-answered on the next run, and the
        record is overwritten in place -- :func:`artifact_filename` is
        deterministic, so a retry lands on the file it is replacing.

        The consequence to know about: a question failing for a *durable*
        infrastructure reason is retried on every resume and will never retire.
        That is the intended reading. A database that is still not indexed is a
        deployment fault the operator should see repeatedly, not a question the
        harness should quietly give up on.

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

        # Read the id out of each file rather than off its name. The filename
        # is sanitised (see `artifact_filename`) and therefore not reversible,
        # and inferring an id from a name that has been through a substitution
        # is how a resume comes to believe the wrong question is done.
        done: set[str] = set()
        retrying = 0
        for path in self._questions.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                question_id = str(record["question_id"])
                error_type = record.get("error_type")
                # Coerced before the membership test below, which is a
                # `frozenset` lookup and raises `TypeError` on an unhashable
                # value. A truncated or hand-edited artifact holding a list
                # would otherwise abort the whole resume -- the one operation
                # that exists to survive a bad situation, failing on a bad
                # situation.
                if not isinstance(error_type, str | None):
                    raise TypeError(f"error_type is {type(error_type).__name__}")
            except (OSError, ValueError, KeyError, TypeError):
                # A file written mid-crash. Re-answering that question is the
                # cheap, correct response; refusing the whole resume is not.
                logger.warning("ignoring unreadable artifact %s", path.name)
                continue

            # Keyed on the raw `error_type` rather than the recorded
            # `failure_category`, because the category is derived and an
            # artifact may have been written by an older taxonomy. The error
            # type is what the component actually reported.
            if is_infrastructure(error_type):
                retrying += 1
                continue

            done.add(question_id)

        if done or retrying:
            logger.info(
                "resuming %s: %d question(s) answered, %d to re-attempt after "
                "infrastructure failure",
                self._root,
                len(done),
                retrying,
            )
        return frozenset(done)

    def record(self, artifact: QuestionArtifact) -> None:
        path = self._questions / artifact_filename(artifact.question_id)
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


_GIT_COMMANDS: Final = frozenset(
    {
        ("rev-parse", "--short", "HEAD"),
        ("status", "--porcelain"),
    }
)


def _git(*args: str) -> str | None:
    """Run one of the fixed git commands, or ``None`` if it is unavailable.

    Restricted to an allowlist rather than accepting any argument sequence.
    Nothing here takes a caller's value today, so the allowlist defends against
    a future edit rather than a current caller -- which is the only moment a
    subprocess argument built from request data would ever be introduced, and
    the moment it is cheapest to refuse.
    """
    if args not in _GIT_COMMANDS:
        raise ValueError(f"refusing to run an unlisted git command: {args!r}")

    try:
        result = subprocess.run(  # noqa: S603 - argv is allowlisted above, never caller data
            ["git", *args],  # noqa: S607 - dev tool, PATH is fine here
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def current_commit() -> str:
    """The commit under test, or ``unknown`` outside a repository.

    Suffixed ``-dirty`` when the working tree has uncommitted changes, and that
    suffix is the load-bearing part. A run made while a fix is still uncommitted
    records the commit *before* the code that produced the number -- so a bare
    hash silently names a tree that reproduces something else. Every run in
    BENCHMARKS.md section 1 was made this way, which is how the gap was found.

    ``-dirty`` cannot say what the changes were, only that the hash is a lower
    bound rather than an answer. That is the honest amount of information, and
    it is the difference between a reader re-running it and a reader knowing
    they cannot.

    Failing softly rather than raising: a benchmark run from a tarball is
    unusual but not wrong, and refusing to run at all would be a poor trade for
    one provenance field. It is recorded as ``unknown`` so a reader can see the
    number is unattributable rather than assume it was never checked.
    """
    head = _git("rev-parse", "--short", "HEAD")
    if head is None or not head.strip():
        return "unknown"

    commit = head.strip()

    # A failed status check must not silently downgrade to "clean" -- an
    # unmarked hash is the exact claim this function exists to stop making.
    status = _git("status", "--porcelain")
    if status is None:
        return f"{commit}-unverified"
    return f"{commit}-dirty" if status.strip() else commit


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
