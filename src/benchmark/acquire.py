"""Getting an archive onto disk, proving what it is, and unpacking it safely.

The order of operations is the security control: **hash first, extract second**.
An archive is verified as bytes on disk before any member name is read, so a
tampered file never reaches the zip parser at all.

Extraction is where the real exposure is. ``ZipFile.extractall`` will happily
write a member named ``../../../.ssh/authorized_keys``, and CVE-2007-4559 is the
same bug in ``tarfile``, unfixed for fifteen years. This module writes members
one at a time through :func:`resolve_member`, which refuses anything that is not
a plain relative path landing under the destination.

Only zip is supported. Both benchmarks ship zips, and every additional archive
format is another parser reading hostile input for no benefit anyone has asked
for.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import zipfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO
from urllib.parse import urlparse

from core.exceptions import ArtifactIntegrityError, UnsafeArchiveError
from core.settings import BenchmarkSettings

logger = logging.getLogger(__name__)

_READ_CHUNK = 1024 * 1024

Opener = Callable[[str], AbstractContextManager[IO[bytes]]]
"""Injected so the download path is testable without a network.

The default reaches the internet; every test passes a local one. See
CODE_STYLE.md section 4 -- a module that reads its own dependencies cannot be
tested, and the download path is exactly the one where an untested branch
matters.
"""


@dataclass(frozen=True, slots=True)
class Acquired:
    """An archive on disk whose digest has been established."""

    path: Path
    sha256: str
    size_bytes: int


def sha256_file(path: Path) -> tuple[str, int]:
    """Digest and size, streamed. Returns ``(hexdigest, bytes)``."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


@contextmanager
def _urlopen(url: str) -> Iterator[IO[bytes]]:
    """The default fetcher. Isolated so the scheme check sits next to the call."""
    import urllib.request

    if urlparse(url).scheme != "https":
        raise ArtifactIntegrityError(f"refusing to download over a non-https URL: {url!r}")

    # S310: the URL cannot come from a caller -- it is a field of an entry in
    # KNOWN_ARTIFACTS, which is source code, and the scheme is checked above.
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        yield response


def download(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    opener: Opener | None = None,
) -> Acquired:
    """Stream a URL to disk, capped, and hashed on the way in.

    The cap is enforced against bytes actually read, not against
    ``Content-Length``: a header is a claim by the server, and a server that
    wants to fill the disk simply does not send an honest one.

    Writes to a ``.part`` file and renames only on success, so an interrupted
    download can never be mistaken for a complete one by a later run that
    checks whether the file exists.

    Raises:
        ArtifactIntegrityError: The response exceeded ``max_bytes``.
    """
    fetch = opener or _urlopen
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")

    digest = hashlib.sha256()
    total = 0
    try:
        with fetch(url) as response, partial.open("wb") as sink:
            while chunk := response.read(_READ_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise ArtifactIntegrityError(
                        f"download from {url!r} exceeded {max_bytes} bytes and was "
                        f"abandoned. Raise BENCHMARK_MAX_ARCHIVE_BYTES if the "
                        f"artifact really is this large."
                    )
                digest.update(chunk)
                sink.write(chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    partial.replace(destination)
    logger.info("downloaded %s (%d bytes)", destination.name, total)
    return Acquired(path=destination, sha256=digest.hexdigest(), size_bytes=total)


_DRIVE = re.compile(r"^[A-Za-z]:")
_WINDOWS_ILLEGAL = '<>:"|?*'
DATABASE_SUFFIXES = (".sqlite", ".sqlite3", ".db")


def unrepresentable_reason(name: str) -> str | None:
    """Why the local filesystem cannot hold this member's name, or ``None``.

    **Separate from the escape checks on purpose, and that separation was
    missing until a real archive hit it.** Spider ships
    ``receipts (3:11:18, 5:53 PM)_original.csv`` -- a timestamp in a filename.
    A colon anywhere used to be rejected as "a drive or stream separator",
    which failed the entire benchmark on a CSV that is not an attack and not
    even a file this project reads.

    The two categories are genuinely different:

    - **An escape** -- ``..``, an absolute path, a leading drive letter, a
      symlink -- means the archive is not trustworthy. Refuse all of it.
    - **An unrepresentable name** means *this filesystem* cannot store it. The
      same archive extracts cleanly on Linux. Skip the member, record it, and
      keep going.

    Conflating them turns a portability limitation into an accusation and
    makes the tool refuse legitimate data.

    Windows-only because these characters are perfectly legal on POSIX, and
    skipping them everywhere would discard real files to no benefit. That does
    make extraction platform-dependent, which is acceptable for exactly one
    reason: :func:`extract` refuses outright if a *database* file is ever the
    unrepresentable one, so the converted corpus cannot differ between
    platforms without the loader saying so.
    """
    if os.name != "nt":
        return None
    for part in name.replace("\\", "/").split("/"):
        # `.` and `..` are path semantics, not filenames. Excluded so this
        # function cannot classify a traversal component as merely unwritable
        # even if it is ever called before the escape check -- which is a
        # mistake that has already been made once here.
        if not part or part in (".", ".."):
            continue
        illegal = sorted({ch for ch in part if ch in _WINDOWS_ILLEGAL})
        if illegal:
            return f"contains {''.join(illegal)!r}, which Windows filenames cannot hold"
        if part != part.rstrip(" ."):
            return "ends in a space or dot, which Windows silently strips"
    return None


def resolve_member(name: str, destination: Path) -> Path:
    """Where an archive member is allowed to be written, or an error.

    Every check here has a specific archive in mind:

    - an absolute path, or a leading Windows drive letter, writes wherever it
      likes -- and ``Path.joinpath`` on a drive-absolute component *discards*
      everything to its left, so this is not theoretical;
    - ``..`` anywhere in the path climbs out of the destination;
    - a backslash separator is a path separator on Windows and an ordinary
      character in a zip name, so ``..\\..\\x`` passes a POSIX-only check;
    - a trailing realpath containment check catches whatever the name-level
      checks did not anticipate, including a symlink planted earlier in the
      same archive.

    A colon *inside* a component is not checked here -- that is representability,
    not escape. See :func:`unrepresentable_reason`.

    Raises:
        UnsafeArchiveError: The member does not resolve to a path strictly
            inside ``destination``.
    """
    if not name or name.startswith(("/", "\\")):
        raise UnsafeArchiveError(f"archive member {name!r} is an absolute path")

    normalised = name.replace("\\", "/")
    parts = [part for part in PurePosixPath(normalised).parts if part not in (".",)]

    if any(part == ".." for part in parts):
        raise UnsafeArchiveError(f"archive member {name!r} escapes the destination with '..'")
    if any(_DRIVE.match(part) for part in parts):
        raise UnsafeArchiveError(f"archive member {name!r} contains a drive reference")
    if not parts:
        raise UnsafeArchiveError(f"archive member {name!r} has no usable path")

    root = destination.resolve()
    target = root.joinpath(*parts)

    # `resolve()` on a path that does not exist yet still collapses `..` and
    # follows any symlink that *does* exist along the way, which is the case
    # this catches: an archive that plants a link and then writes through it.
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise UnsafeArchiveError(f"archive member {name!r} resolves to {resolved}, outside {root}")
    return resolved


def _is_link(info: zipfile.ZipInfo) -> bool:
    """Whether a member carries Unix mode bits saying it is not a plain file.

    The file-type field has to be isolated before it means anything. Plenty of
    perfectly ordinary archives store permission bits with *no* type bits at
    all -- ``ZipFile.writestr`` records ``0o600`` and nothing else -- so testing
    "not S_ISREG and not S_ISDIR" rejects every one of them. An absent type
    field is not a claim about the member; only a present one is.
    """
    file_type = (info.external_attr >> 16) & 0o170000
    if file_type == 0:
        return False
    return file_type not in (stat.S_IFREG, stat.S_IFDIR)


@dataclass(frozen=True, slots=True)
class Extraction:
    """What came out of an archive, including what deliberately did not."""

    written: list[Path]
    skipped: list[tuple[str, str]]
    """``(member name, reason)`` for members this filesystem cannot hold.

    Returned rather than logged so a caller can surface the count. A file the
    tool decided not to write is exactly the kind of thing that should not be
    discoverable only by noticing it missing later.
    """

    total_bytes: int


def extract(archive: Path, destination: Path, *, settings: BenchmarkSettings) -> Extraction:
    """Unpack a zip, refusing anything that could write outside ``destination``.

    Every member path is validated before the archive is opened for reading, so
    a hostile archive is rejected without a single byte being written.

    The size caps are enforced against bytes *written*, not the sizes declared
    in the archive directory. A declared size is attacker-controlled; a 42-byte
    zip that expands to petabytes declares whatever it needs to.

    **An escape fails the archive; an unrepresentable name skips the member.**
    The exception is a *database* file — skipping one of those would silently
    change which databases the corpus contains, so it fails the archive
    instead. That is the line between "this filesystem cannot store a CSV with
    a colon in its name" and "the benchmark you converted is not the benchmark
    you think it is".

    Raises:
        UnsafeArchiveError: A member escapes, is a link, exceeds a cap, or is
            an unrepresentable database file.
    """
    destination.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()

        if len(infos) > settings.benchmark_max_archive_members:
            raise UnsafeArchiveError(
                f"{archive.name} declares {len(infos)} members, over the "
                f"{settings.benchmark_max_archive_members} limit"
            )

        # Whole-archive validation first: a rejection must not leave half an
        # extraction behind for a later run to pick up as if it were complete.
        targets: list[tuple[zipfile.ZipInfo, Path]] = []
        skipped: list[tuple[str, str]] = []
        for info in infos:
            if _is_link(info):
                raise UnsafeArchiveError(
                    f"archive member {info.filename!r} is a symlink or special file"
                )

            # ORDER IS LOAD-BEARING. The escape check runs first, always.
            #
            # Written the other way round for one commit, and the traversal
            # suite went red immediately: `..` is a path component that ends in
            # a dot, so the representability rule matched it first and a
            # traversal attempt was *skipped as a portability issue* rather
            # than refused. A usability fix had quietly disarmed the primary
            # control. Nothing may be classified as merely unwritable until it
            # has been proven not to be an escape.
            target = resolve_member(info.filename, destination)

            reason = unrepresentable_reason(info.filename)
            if reason is not None:
                if info.filename.lower().endswith(DATABASE_SUFFIXES):
                    raise UnsafeArchiveError(
                        f"database file {info.filename!r} {reason}. Skipping it would "
                        f"silently drop a database from the corpus, so the archive is "
                        f"refused instead. Extract it on a filesystem that can hold "
                        f"the name."
                    )
                skipped.append((info.filename, reason))
                continue

            targets.append((info, target))

        written: list[Path] = []
        total = 0
        for info, target in targets:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            budget = min(
                settings.benchmark_max_member_bytes,
                settings.benchmark_max_archive_bytes - total,
            )
            total += _copy_member(bundle, info, target, budget=budget)
            written.append(target)

        if skipped:
            logger.warning(
                "skipped %d member(s) this filesystem cannot name; none were databases",
                len(skipped),
            )
        logger.info("extracted %d files (%d bytes) from %s", len(written), total, archive.name)
        return Extraction(written=written, skipped=skipped, total_bytes=total)


def _copy_member(
    bundle: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    *,
    budget: int,
) -> int:
    """Stream one member, stopping the moment it exceeds its budget.

    Reads one chunk *past* the budget on purpose: a member whose real size
    equals the remaining budget exactly must be allowed through, and the only
    way to know it stopped there is to try to read more.
    """
    written = 0
    with bundle.open(info) as source, target.open("wb") as sink:
        while chunk := source.read(_READ_CHUNK):
            written += len(chunk)
            if written > budget:
                sink.close()
                target.unlink(missing_ok=True)
                raise UnsafeArchiveError(
                    f"archive member {info.filename!r} expanded past its "
                    f"{budget}-byte budget; refusing to continue "
                    f"(declared size was {info.file_size})"
                )
            sink.write(chunk)
    return written


def clear_directory(path: Path) -> None:
    """Remove a previously extracted tree.

    Used by ``--replace``. Refuses anything that is not a directory the caller
    just named under the data root, because the alternative is a recursive
    delete driven by a path that came from somewhere else.
    """
    if not path.exists():
        return
    if not path.is_dir() or path.is_symlink():
        raise UnsafeArchiveError(f"{path} is not a plain directory; refusing to remove it")
    if path.resolve() == Path(os.sep).resolve() or not path.resolve().parents:
        raise UnsafeArchiveError(f"refusing to remove {path}")
    shutil.rmtree(path)


__all__ = [
    "DATABASE_SUFFIXES",
    "Acquired",
    "Extraction",
    "clear_directory",
    "download",
    "extract",
    "resolve_member",
    "sha256_file",
    "unrepresentable_reason",
]
