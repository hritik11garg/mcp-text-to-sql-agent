"""The archive and integrity boundary, asserted by trying to break it.

A benchmark archive is downloaded from the internet, extracted onto the machine
that runs the loader, and then parsed by SQLite. Every test here builds an
archive that should not be trusted and asserts the loader **refuses** it. A test
that passes because the malicious member happened to be written somewhere
harmless is not evidence of anything, so each one also checks that nothing
landed outside the destination.

OWASP mapping: path traversal is A01 (broken access control) reached through
A03 (injection into a filesystem path); the digest check is A08 (software and
data integrity failures); the size caps are availability controls.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import IO

import pytest

from benchmark.acquire import clear_directory, download, extract, resolve_member, sha256_file
from benchmark.sources import ArtifactLock
from core.exceptions import ArtifactIntegrityError, UnsafeArchiveError
from core.settings import BenchmarkSettings


def write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, payload in members.items():
            bundle.writestr(name, payload)
    return path


def write_zip_with_symlink(path: Path, name: str, target: str) -> Path:
    """A zip member carrying Unix symlink mode bits.

    ``ZipFile.writestr`` will not create one, so the external attributes are set
    by hand -- which is exactly how an attacker would build it.
    """
    with zipfile.ZipFile(path, "w") as bundle:
        info = zipfile.ZipInfo(name)
        info.create_system = 3  # Unix
        info.external_attr = (0o120777 << 16) | 0o200000
        bundle.writestr(info, target)
    return path


@pytest.fixture
def settings() -> BenchmarkSettings:
    return BenchmarkSettings()


class TestPathTraversal:
    """``ZipFile.extractall`` writes every one of these. That is the bug."""

    @pytest.mark.parametrize(
        "name",
        [
            "../escaped.txt",
            "../../escaped.txt",
            "nested/../../escaped.txt",
            "/absolute.txt",
            "..\\escaped.txt",
            "nested\\..\\..\\escaped.txt",
            "C:/windows/system32/escaped.txt",
        ],
    )
    def test_a_member_that_escapes_the_destination_is_refused(
        self, tmp_path: Path, settings: BenchmarkSettings, name: str
    ) -> None:
        archive = write_zip(tmp_path / "hostile.zip", {name: b"pwned"})
        destination = tmp_path / "out"

        with pytest.raises(UnsafeArchiveError):
            extract(archive, destination, settings=settings)

        # The refusal is only meaningful if the file is genuinely not there.
        assert list(tmp_path.rglob("escaped.txt")) == []

    def test_nothing_is_written_when_any_member_is_unsafe(
        self, tmp_path: Path, settings: BenchmarkSettings
    ) -> None:
        # Validation covers the whole archive before the first byte is written.
        # Extracting the safe members first would leave a partial tree that a
        # later run picks up as though the extraction had succeeded.
        archive = write_zip(tmp_path / "mixed.zip", {"good.txt": b"fine", "../bad.txt": b"pwned"})
        destination = tmp_path / "out"

        with pytest.raises(UnsafeArchiveError):
            extract(archive, destination, settings=settings)

        assert list(destination.rglob("*.txt")) == []

    def test_an_ordinary_nested_path_is_allowed(
        self, tmp_path: Path, settings: BenchmarkSettings
    ) -> None:
        archive = write_zip(tmp_path / "ok.zip", {"database/concert/concert.sqlite": b"data"})
        written = extract(archive, tmp_path / "out", settings=settings)

        assert written == [tmp_path / "out" / "database" / "concert" / "concert.sqlite"]
        assert written[0].read_bytes() == b"data"

    def test_resolve_member_rejects_a_path_that_climbs_out(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafeArchiveError, match="escapes"):
            resolve_member("../x", tmp_path)


class TestSymlinks:
    def test_a_symlink_member_is_refused(self, tmp_path: Path, settings: BenchmarkSettings) -> None:
        # A symlink is the second half of a two-step traversal: the archive
        # plants a link pointing outside, then writes "through" it with a
        # member whose own path looks perfectly safe.
        archive = write_zip_with_symlink(tmp_path / "link.zip", "escape", "/etc")

        with pytest.raises(UnsafeArchiveError, match="symlink"):
            extract(archive, tmp_path / "out", settings=settings)


class TestSizeLimits:
    def test_an_archive_with_too_many_members_is_refused(self, tmp_path: Path) -> None:
        archive = write_zip(tmp_path / "many.zip", {f"f{n}": b"x" for n in range(20)})
        limited = BenchmarkSettings(benchmark_max_archive_members=5)

        with pytest.raises(UnsafeArchiveError, match="members"):
            extract(archive, tmp_path / "out", settings=limited)

    def test_a_member_that_expands_past_its_budget_is_refused(self, tmp_path: Path) -> None:
        # The bomb case. `file_size` in the archive directory is attacker
        # controlled, so the cap is enforced against bytes actually written.
        archive = write_zip(tmp_path / "bomb.zip", {"big": b"\x00" * 100_000})
        limited = BenchmarkSettings(benchmark_max_member_bytes=1024)

        with pytest.raises(UnsafeArchiveError, match="budget"):
            extract(archive, tmp_path / "out", settings=limited)

        assert not (tmp_path / "out" / "big").exists()

    def test_the_archive_total_is_capped_across_members(self, tmp_path: Path) -> None:
        archive = write_zip(
            tmp_path / "cumulative.zip", {f"f{n}": b"\x00" * 4096 for n in range(10)}
        )
        limited = BenchmarkSettings(benchmark_max_archive_bytes=8192)

        with pytest.raises(UnsafeArchiveError):
            extract(archive, tmp_path / "out", settings=limited)


class TestDownloadLimits:
    def test_a_response_larger_than_the_cap_is_abandoned(self, tmp_path: Path) -> None:
        @contextmanager
        def endless(url: str) -> Iterator[IO[bytes]]:
            yield BytesIO(b"\x00" * 100_000)

        target = tmp_path / "big.zip"
        with pytest.raises(ArtifactIntegrityError, match="exceeded"):
            download("https://example.test/big.zip", target, max_bytes=1024, opener=endless)

        assert not target.exists()

    def test_an_interrupted_download_leaves_no_usable_file(self, tmp_path: Path) -> None:
        # A `.part` renamed only on success. Otherwise a later run that checks
        # "does the file exist" adopts a truncated archive.
        @contextmanager
        def broken(url: str) -> Iterator[IO[bytes]]:
            class Failing(BytesIO):
                def read(self, size: int = -1) -> bytes:
                    raise OSError("connection reset")

            yield Failing()

        target = tmp_path / "partial.zip"
        with pytest.raises(OSError, match="connection reset"):
            download("https://example.test/x.zip", target, max_bytes=1024, opener=broken)

        assert not target.exists()
        assert list(tmp_path.glob("*.part")) == []

    def test_a_non_https_url_is_refused_by_the_default_opener(self, tmp_path: Path) -> None:
        with pytest.raises(ArtifactIntegrityError, match="non-https"):
            download("http://example.test/x.zip", tmp_path / "x.zip", max_bytes=1024)


class TestIntegrity:
    def test_a_changed_archive_fails_the_lockfile_check(self, tmp_path: Path) -> None:
        original = tmp_path / "a.zip"
        original.write_bytes(b"the archive everything was measured against")
        digest, size = sha256_file(original)

        lock = ArtifactLock(path=tmp_path / "lock.json", entries={})
        lock.record("spider", filename="a.zip", digest=digest, size_bytes=size, source="test")

        replaced = tmp_path / "b.zip"
        replaced.write_bytes(b"a different archive entirely")
        other_digest, other_size = sha256_file(replaced)

        with pytest.raises(ArtifactIntegrityError, match="does not match"):
            lock.check("spider", digest=other_digest, size_bytes=other_size)

    def test_an_unrecorded_artifact_is_refused_rather_than_trusted(self, tmp_path: Path) -> None:
        lock = ArtifactLock(path=tmp_path / "lock.json", entries={})
        with pytest.raises(ArtifactIntegrityError, match="no digest is recorded"):
            lock.check("spider", digest="deadbeef", size_bytes=1)

    def test_recording_does_not_overwrite_an_existing_digest(self, tmp_path: Path) -> None:
        # The lockfile is append-only through this API. Silently updating it is
        # how a project stops noticing that its benchmark changed.
        lock = ArtifactLock(path=tmp_path / "lock.json", entries={})
        lock.record("spider", filename="a.zip", digest="first", size_bytes=1, source="test")
        lock.record("spider", filename="a.zip", digest="second", size_bytes=2, source="test")

        assert lock.entries["spider"].sha256 == "first"

    def test_a_lockfile_round_trips(self, tmp_path: Path) -> None:
        lock = ArtifactLock(path=tmp_path / "lock.json", entries={})
        lock.record("spider", filename="a.zip", digest="abc123", size_bytes=7, source="test")
        lock.save()

        reloaded = ArtifactLock.load(tmp_path / "lock.json")
        assert reloaded.entries["spider"].sha256 == "abc123"
        assert reloaded.entries["spider"].size_bytes == 7

    def test_a_missing_lockfile_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert ArtifactLock.load(tmp_path / "absent.json").entries == {}


class TestClearDirectory:
    def test_removes_a_previous_extraction(self, tmp_path: Path) -> None:
        target = tmp_path / "spider"
        (target / "database").mkdir(parents=True)
        (target / "database" / "x.sqlite").write_bytes(b"old")

        clear_directory(target)
        assert not target.exists()

    def test_refuses_a_path_that_is_not_a_directory(self, tmp_path: Path) -> None:
        file_path = tmp_path / "notadir"
        file_path.write_bytes(b"")
        with pytest.raises(UnsafeArchiveError, match="not a plain directory"):
            clear_directory(file_path)

    def test_a_missing_path_is_a_no_op(self, tmp_path: Path) -> None:
        clear_directory(tmp_path / "never-existed")
