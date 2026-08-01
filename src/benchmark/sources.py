"""What may be downloaded, and the digests that say it was not tampered with.

A benchmark archive is code-adjacent: it decides every number this project
reports. Two failure modes matter and they are different.

**A silently different version.** Spider and BIRD have both been re-released
with corrections. A run against ``dev.json`` from March and a run against
``dev.json`` from September are not comparable, and nothing about either run
says which one it used. This is the common case and it has nothing to do with
attackers.

**A tampered archive.** The download is a zip that gets extracted onto the
machine and a set of SQLite files that get parsed by a C library. Verifying the
digest before either happens is the only control that covers both.

Both are handled by the same mechanism: a lockfile of observed digests that is
committed to the repository. It works the way ``pip``'s hash-checking mode
does -- the first acquisition records what it saw, every later one must match.

**Why the digests are not hardcoded here.** They would be a fabrication.
Nobody involved in writing this file has downloaded these archives, and a
constant that claims to be the SHA-256 of Spider without ever having been
compared to one is worse than no constant at all: it fails every honest
download and gets "fixed" by pasting in whatever the failing run reported,
which is trust-on-first-use with extra steps and a false claim in the source.
So the first acquisition is explicit about being trust-on-first-use, it has to
be asked for, and what it records is committed and reviewable from then on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from core.exceptions import ArtifactIntegrityError


@dataclass(frozen=True, slots=True)
class Artifact:
    """One downloadable file, named by a key the CLI takes as an argument."""

    key: str
    filename: str
    homepage: str
    """Where a human goes to get it, and to read its license terms."""

    url: str | None = None
    """Direct download, when one exists.

    ``None`` for anything published behind a consent page or a Drive
    confirmation token. Those are fetched by hand and passed in with
    ``--archive``; the integrity check is identical either way, because it
    happens on the bytes on disk and not on the transport.
    """

    max_bytes: int = 16 * 1024**3
    note: str = ""


KNOWN_ARTIFACTS: dict[str, Artifact] = {
    "spider": Artifact(
        key="spider",
        filename="spider.zip",
        homepage="https://yale-lily.github.io/spider",
        url=None,
        note=(
            "Spider 1.0 -- NOT Spider 2.0, which is a different task: enterprise "
            "workflows whose expected output is CSV files, and which releases "
            "only a small amount of gold SQL. This harness computes execution "
            "accuracy and Recall@k *from* a reference query, so it has nothing "
            "to work with there. See DATASETS.md section 1. "
            "Distributed via Google Drive, which serves an interstitial rather "
            "than the file for automated clients. Download it in a browser and "
            "pass --archive."
        ),
    ),
    "bird-dev": Artifact(
        key="bird-dev",
        filename="bird_dev.zip",
        homepage="https://bird-bench.github.io/",
        url=None,
        note="Download the dev pack from the project page and pass --archive.",
    ),
    "bird-train": Artifact(
        key="bird-train",
        filename="bird_train.zip",
        homepage="https://bird-bench.github.io/",
        url=None,
        note="Large. Not needed to produce a dev or held-out number.",
    ),
}
"""The allowlist. There is deliberately no ``--url`` flag.

A URL that came from the command line is a URL an operator can be talked into,
and the thing on the other end of it gets extracted and parsed locally. Adding
a source means editing this dict in a reviewed commit.
"""


@dataclass(frozen=True, slots=True)
class LockEntry:
    """The digest of an artifact as it was on the day it was first acquired."""

    key: str
    filename: str
    sha256: str
    size_bytes: int
    recorded_at: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class ArtifactLock:
    """The committed record of every artifact this project has been run against."""

    path: Path
    entries: dict[str, LockEntry]

    @classmethod
    def load(cls, path: Path) -> ArtifactLock:
        """Read the lockfile. A missing file is an empty lock, not an error."""
        if not path.exists():
            return cls(path=path, entries={})

        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = {
            key: LockEntry(
                key=key,
                filename=str(value["filename"]),
                sha256=str(value["sha256"]),
                size_bytes=int(value["size_bytes"]),
                recorded_at=str(value["recorded_at"]),
                source=str(value.get("source", "")),
            )
            for key, value in raw.get("artifacts", {}).items()
        }
        return cls(path=path, entries=entries)

    def save(self) -> None:
        """Write the lockfile, sorted, so a diff shows only what changed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "_comment": (
                "Digests of the benchmark archives this project has been run against. "
                "Committed on purpose: a benchmark number is only comparable to another "
                "one computed from the same bytes. Recorded by `python -m benchmark.load "
                "acquire --trust-on-first-use`; never edit by hand to make a check pass."
            ),
            "artifacts": {
                key: {
                    "filename": entry.filename,
                    "sha256": entry.sha256,
                    "size_bytes": entry.size_bytes,
                    "recorded_at": entry.recorded_at,
                    "source": entry.source,
                }
                for key, entry in sorted(self.entries.items())
            },
        }
        self.path.write_text(json.dumps(body, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    def check(self, key: str, *, digest: str, size_bytes: int) -> None:
        """Compare an observed digest against the recorded one.

        Raises:
            ArtifactIntegrityError: There is a recorded digest and it differs.
                The message says what to do, because the correct action is
                never "update the lockfile" without knowing why it moved.
        """
        entry = self.entries.get(key)
        if entry is None:
            raise ArtifactIntegrityError(
                f"no digest is recorded for {key!r}. Re-run with "
                f"--trust-on-first-use to record the one you have, and commit "
                f"the lockfile so later runs are checked against it."
            )
        if entry.sha256 != digest:
            raise ArtifactIntegrityError(
                f"{key!r} does not match the recorded digest.\n"
                f"  recorded: {entry.sha256} ({entry.size_bytes} bytes, {entry.recorded_at})\n"
                f"  observed: {digest} ({size_bytes} bytes)\n"
                f"The benchmark has either been re-released or the download is "
                f"not what it claims to be. Every number already recorded came "
                f"from the first one; do not overwrite the lockfile to make this "
                f"pass without deciding which archive the project is measuring "
                f"against and re-running the affected benchmarks."
            )

    def record(self, key: str, *, filename: str, digest: str, size_bytes: int, source: str) -> None:
        """Register a first-seen digest. Does not overwrite an existing one."""
        if key in self.entries:
            return
        self.entries[key] = LockEntry(
            key=key,
            filename=filename,
            sha256=digest,
            size_bytes=size_bytes,
            recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
            source=source,
        )

    def with_entry(self, entry: LockEntry) -> ArtifactLock:
        """A copy with one entry replaced. Used by tests; the lock is otherwise append-only."""
        return replace(self, entries={**self.entries, entry.key: entry})


DEFAULT_LOCK_PATH = Path("data/artifacts.lock.json")
"""Committed despite living under the otherwise-ignored ``data/``.

The data is CC BY-SA and stays out of version control (DATASETS.md section 7);
the *statement of which data* is the reproducibility record and belongs in it.
"""


def resolve_artifact(key: str) -> Artifact:
    """Look up an allowlisted artifact.

    Raises:
        KeyError: with the valid keys listed, since the set is small and a typo
            is the likely cause.
    """
    try:
        return KNOWN_ARTIFACTS[key]
    except KeyError as exc:
        known = ", ".join(sorted(KNOWN_ARTIFACTS))
        raise KeyError(f"unknown artifact {key!r}; known artifacts are: {known}") from exc


__all__ = [
    "DEFAULT_LOCK_PATH",
    "KNOWN_ARTIFACTS",
    "Artifact",
    "ArtifactLock",
    "LockEntry",
    "resolve_artifact",
]
