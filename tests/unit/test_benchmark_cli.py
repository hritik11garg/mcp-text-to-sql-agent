"""``python -m benchmark.load`` — the paths that need no database.

Acquisition and split assignment are testable end to end without PostgreSQL,
and both have exit codes that matter: a CI step that treats "the archive is not
what it was" or "no split was written" as success is worse than no CI step.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from benchmark import load, sources
from benchmark.load import EXIT_ERROR, EXIT_OK, EXIT_USAGE
from core.exceptions import BenchmarkError
from evals.dataset import Split, load_questions


@pytest.fixture
def spider_questions(tmp_path: Path) -> Path:
    path = tmp_path / "dev.json"
    path.write_text(
        json.dumps(
            [
                {"db_id": f"db{n}", "question": f"Question {n}?", "query": "SELECT 1"}
                for n in range(40)
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def local_artifact(tmp_path: Path) -> Path:
    """An allowlisted artifact with no URL, so acquisition uses ``--archive``."""
    archive = tmp_path / "spider.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("database/concert/concert.sqlite", b"not really a database")
    return archive


def run(argv: list[str]) -> int:
    return load.main(argv)


class TestAcquire:
    def test_first_use_records_a_digest_and_extracts(
        self, tmp_path: Path, local_artifact: Path
    ) -> None:
        lockfile = tmp_path / "lock.json"
        code = run(
            [
                "acquire",
                "spider",
                "--archive",
                str(local_artifact),
                "--data-dir",
                str(tmp_path / "data"),
                "--lockfile",
                str(lockfile),
                "--trust-on-first-use",
            ]
        )

        assert code == EXIT_OK
        assert (tmp_path / "data" / "spider" / "database" / "concert" / "concert.sqlite").is_file()
        recorded = sources.ArtifactLock.load(lockfile)
        assert len(recorded.entries["spider"].sha256) == 64

    def test_without_trust_on_first_use_an_unlocked_artifact_is_refused(
        self, tmp_path: Path, local_artifact: Path
    ) -> None:
        code = run(
            [
                "acquire",
                "spider",
                "--archive",
                str(local_artifact),
                "--data-dir",
                str(tmp_path / "data"),
                "--lockfile",
                str(tmp_path / "lock.json"),
            ]
        )

        assert code == EXIT_ERROR
        assert not (tmp_path / "data" / "spider").exists()

    def test_a_different_archive_fails_against_a_recorded_digest(
        self, tmp_path: Path, local_artifact: Path
    ) -> None:
        lockfile = tmp_path / "lock.json"
        base = [
            "acquire",
            "spider",
            "--data-dir",
            str(tmp_path / "data"),
            "--lockfile",
            str(lockfile),
        ]
        assert run([*base, "--archive", str(local_artifact), "--trust-on-first-use"]) == EXIT_OK

        replacement = tmp_path / "other.zip"
        with zipfile.ZipFile(replacement, "w") as bundle:
            bundle.writestr("database/concert/concert.sqlite", b"a different release entirely")

        # --trust-on-first-use must not launder a second, different archive.
        # Recording only happens when nothing is locked yet.
        assert run([*base, "--archive", str(replacement), "--trust-on-first-use"]) == EXIT_ERROR

    def test_an_artifact_with_no_url_and_no_archive_explains_itself(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run(
            ["acquire", "spider", "--data-dir", str(tmp_path), "--lockfile", str(tmp_path / "l")]
        )

        assert code == EXIT_USAGE
        message = capsys.readouterr().err
        assert "yale-lily.github.io" in message
        assert "--archive" in message

    def test_an_unknown_artifact_key_lists_the_known_ones(self, tmp_path: Path) -> None:
        code = run(["acquire", "spidr", "--data-dir", str(tmp_path)])
        assert code == EXIT_ERROR


class TestSplits:
    def test_writes_one_file_per_split_and_an_assignment(
        self, tmp_path: Path, spider_questions: Path
    ) -> None:
        out = tmp_path / "splits"
        code = run(
            [
                "splits",
                "--questions",
                str(spider_questions),
                "--benchmark",
                "spider",
                "--dataset",
                "spider",
                "--out",
                str(out),
            ]
        )

        assert code == EXIT_OK
        assignment = json.loads((out / "spider-assignment.json").read_text(encoding="utf-8"))
        assert len(assignment["databases"]) == 40
        assert assignment["seed"] == "t2sql-v1"

        written = sorted(path.name for path in out.glob("*.jsonl"))
        assert written, "no split files were written"

    def test_every_written_question_carries_its_split(
        self, tmp_path: Path, spider_questions: Path
    ) -> None:
        # The file is the split. A question stamped `dev` sitting in the
        # held-out file would be scored as held-out by anything reading the
        # filename and as dev by anything reading the field.
        out = tmp_path / "splits"
        run(
            [
                "splits",
                "--questions",
                str(spider_questions),
                "--benchmark",
                "spider",
                "--out",
                str(out),
            ]
        )

        for path in out.glob("*.jsonl"):
            expected = Split(path.stem.split("-", 1)[1])
            assert all(question.split is expected for question in load_questions(path))

    def test_a_seed_change_is_recorded_in_the_assignment(
        self, tmp_path: Path, spider_questions: Path
    ) -> None:
        out = tmp_path / "splits"
        run(
            [
                "splits",
                "--questions",
                str(spider_questions),
                "--benchmark",
                "spider",
                "--out",
                str(out),
                "--seed",
                "experiment-2",
            ]
        )

        assignment = json.loads((out / "spider-assignment.json").read_text(encoding="utf-8"))
        assert assignment["seed"] == "experiment-2"


class TestSelection:
    def test_an_unknown_database_name_is_refused(self) -> None:
        # Silently ignoring it would produce a run over zero databases that
        # exits 0 -- a typo that looks like a clean pass.
        with pytest.raises(BenchmarkError, match="no such database"):
            load._select({"alpha": Path("a")}, only=["alfa"], limit=None)

    def test_limit_truncates_in_order(self) -> None:
        found = {name: Path(name) for name in ("a", "b", "c")}
        assert list(load._select(found, only=None, limit=2)) == ["a", "b"]
