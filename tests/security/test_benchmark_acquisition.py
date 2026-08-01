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

import os
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import IO

import pytest

from benchmark.acquire import (
    clear_directory,
    download,
    extract,
    resolve_member,
    sha256_file,
    unrepresentable_reason,
)
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
        result = extract(archive, tmp_path / "out", settings=settings)

        assert result.written == [tmp_path / "out" / "database" / "concert" / "concert.sqlite"]
        assert result.written[0].read_bytes() == b"data"
        assert result.skipped == []

    def test_resolve_member_rejects_a_path_that_climbs_out(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafeArchiveError, match="escapes"):
            resolve_member("../x", tmp_path)

    def test_a_drive_letter_is_still_refused(self, tmp_path: Path) -> None:
        # Path.joinpath on a drive-absolute component discards everything to
        # its left, so this is the one colon that really is an escape.
        with pytest.raises(UnsafeArchiveError, match="drive reference"):
            resolve_member("C:/windows/x", tmp_path)


class TestUnrepresentableNames:
    """A name this filesystem cannot hold is not an attack, and was treated as one.

    Spider ships `receipts (3:11:18, 5:53 PM)_original.csv`. Rejecting any
    colon as "a drive or stream separator" failed the entire benchmark on a CSV
    the loader does not even read. Found on first contact with the real
    archive, which is the only place it could have been found.
    """

    def test_a_colon_inside_a_name_is_skipped_not_refused(
        self, tmp_path: Path, settings: BenchmarkSettings
    ) -> None:
        archive = write_zip(
            tmp_path / "spider.zip",
            {
                "database/concert/concert.sqlite": b"data",
                "database/bakery/data_csv/receipts (3:11:18, 5:53 PM).csv": b"a,b",
            },
        )
        result = extract(archive, tmp_path / "out", settings=settings)

        assert (tmp_path / "out" / "database" / "concert" / "concert.sqlite").is_file()
        if os.name == "nt":
            assert [name for name, _ in result.skipped] == [
                "database/bakery/data_csv/receipts (3:11:18, 5:53 PM).csv"
            ]
        else:
            # Perfectly legal on POSIX; discarding it there would lose real data.
            assert result.skipped == []

    @pytest.mark.skipif(os.name != "nt", reason="these names are legal on POSIX")
    def test_an_unrepresentable_database_file_refuses_the_archive(
        self, tmp_path: Path, settings: BenchmarkSettings
    ) -> None:
        # The line between "cannot store a CSV" and "the corpus you converted
        # is not the corpus you think it is". Skipping a database silently
        # changes which databases exist.
        archive = write_zip(tmp_path / "bad.zip", {"database/we:ird/we:ird.sqlite": b"data"})

        with pytest.raises(UnsafeArchiveError, match="database file"):
            extract(archive, tmp_path / "out", settings=settings)

    @pytest.mark.skipif(os.name != "nt", reason="these names are legal on POSIX")
    @pytest.mark.parametrize("name", ["a<b.csv", 'a"b.csv', "a|b.csv", "a?b.csv", "a*b.csv"])
    def test_other_windows_illegal_characters_are_skipped(
        self, tmp_path: Path, settings: BenchmarkSettings, name: str
    ) -> None:
        archive = write_zip(tmp_path / "x.zip", {f"data/{name}": b"x", "data/ok.csv": b"y"})
        result = extract(archive, tmp_path / "out", settings=settings)

        assert len(result.skipped) == 1
        assert (tmp_path / "out" / "data" / "ok.csv").is_file()

    def test_a_traversal_component_is_never_classified_as_unwritable(self) -> None:
        # The regression that mattered. `..` ends in a dot, so a naive
        # representability rule matches it -- and when that rule ran first, a
        # traversal attempt was skipped as a portability issue instead of
        # refused. The whole traversal suite went red, which is what a negative
        # suite is for.
        assert unrepresentable_reason("../escaped.txt") is None
        assert unrepresentable_reason("nested/../../escaped.txt") is None

    def test_escape_wins_over_representability_when_both_apply(
        self, tmp_path: Path, settings: BenchmarkSettings
    ) -> None:
        # A member that is both an escape and unwritable must be refused, not
        # skipped. Silently skipping it would report an archive as clean.
        archive = write_zip(tmp_path / "both.zip", {"../we:ird.csv": b"x"})
        with pytest.raises(UnsafeArchiveError, match="escapes"):
            extract(archive, tmp_path / "out", settings=settings)

    def test_a_reason_is_recorded_with_every_skip(self) -> None:
        # A file the tool decided not to write must not be discoverable only by
        # noticing it missing later.
        reason = unrepresentable_reason("a/b:c.csv")
        assert (reason is not None and "Windows" in reason) or os.name != "nt"


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
