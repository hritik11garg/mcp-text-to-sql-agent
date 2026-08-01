"""``python -m benchmark.load`` — acquire, convert, verify, split.

Four subcommands rather than one, because they fail for unrelated reasons and
have wildly different costs. Re-verifying after a code change should not
re-download eight gigabytes, and a conversion that failed on database 140 of
200 should be resumable by re-running the same command.

Every subcommand is thin. The logic is library code, tested without a CLI --
see CODE_STYLE.md section 4.

**Exit codes are distinct on purpose.** ``0`` success, ``1`` a failure of the
tool, ``2`` bad arguments, ``3`` the work ran and the *result* was negative --
a conversion that could not be verified. A verification failure that exited 0
would let a CI step "pass" while reporting that the data is wrong.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from benchmark import acquire, convert, readers, splits
from benchmark.sources import DEFAULT_LOCK_PATH, ArtifactLock, resolve_artifact
from benchmark.sqlite_source import open_database
from benchmark.verify import verify_database
from core.exceptions import BenchmarkError, ConfigurationError
from core.settings import BenchmarkSettings, DatabaseSettings
from evals.dataset import Question, Split, write_questions

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNVERIFIED = 3


@dataclass(frozen=True, slots=True)
class LoaderSettings:
    """The two settings groups this tool actually uses.

    Not :class:`core.settings.Settings`. Composing every group would validate
    the LLM configuration, so ``benchmark.load splits`` -- which reads a JSON
    file and writes JSONL, touching neither a model nor a database -- would
    refuse to start with ``LLM_MODEL is required``. A tool that demands
    configuration for a subsystem it never calls is one people work around by
    inventing a fake value, and a fake value in the environment outlives the
    command that needed it.

    The settings module composes its groups precisely so they can be taken
    separately; this is that being used rather than described.
    """

    benchmark: BenchmarkSettings
    database: DatabaseSettings

    @classmethod
    def load(cls) -> LoaderSettings:
        return cls(benchmark=BenchmarkSettings(), database=DatabaseSettings())


# --- acquire ---------------------------------------------------------------


def command_acquire(args: argparse.Namespace, settings: LoaderSettings) -> int:
    """Fetch or adopt an archive, check it against the lockfile, then extract."""
    artifact = resolve_artifact(args.artifact)
    data_dir = args.data_dir or settings.benchmark.benchmark_data_dir
    lock = ArtifactLock.load(args.lockfile)

    if args.archive:
        archive = Path(args.archive)
        if not archive.is_file():
            raise BenchmarkError(f"no archive at {archive}")
        digest, size = acquire.sha256_file(archive)
        source = f"local:{archive.name}"
    elif artifact.url:
        target = data_dir / "archives" / artifact.filename
        result = acquire.download(
            artifact.url,
            target,
            max_bytes=min(artifact.max_bytes, settings.benchmark.benchmark_max_archive_bytes),
        )
        archive, digest, size = result.path, result.sha256, result.size_bytes
        source = artifact.url
    else:
        print(
            f"{artifact.key} has no direct download.\n"
            f"  Get it from: {artifact.homepage}\n"
            f"  {artifact.note}\n"
            f"  Then: python -m benchmark.load acquire {artifact.key} --archive <path>",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.trust_on_first_use and artifact.key not in lock.entries:
        lock.record(
            artifact.key, filename=archive.name, digest=digest, size_bytes=size, source=source
        )
        lock.save()
        logger.warning(
            "recorded a first-seen digest for %s. Commit %s -- until it is "
            "committed, nothing checks that later runs use the same data.",
            artifact.key,
            args.lockfile,
        )

    # Unconditional, including immediately after recording: this is what proves
    # the recorded digest describes the file about to be extracted.
    lock.check(artifact.key, digest=digest, size_bytes=size)

    destination = data_dir / artifact.key
    if args.replace:
        acquire.clear_directory(destination)
    written = acquire.extract(archive, destination, settings=settings.benchmark)

    print(
        json.dumps(
            {
                "artifact": artifact.key,
                "sha256": digest,
                "size_bytes": size,
                "extracted_to": str(destination),
                "files": len(written),
            },
            indent=2,
        )
    )
    return EXIT_OK


# --- convert ---------------------------------------------------------------


def command_convert(args: argparse.Namespace, settings: LoaderSettings) -> int:
    """Convert every SQLite database under ``--databases`` into its own schema."""
    found = readers.find_databases(args.databases)
    if not found:
        raise BenchmarkError(f"no <name>/<name>.sqlite databases under {args.databases}")

    selected = _select(found, only=args.only, limit=args.limit)
    reports: list[dict[str, Any]] = []
    failures = 0

    with _connect(settings) as connection:
        for db_id, path in selected.items():
            try:
                with open_database(path, db_id=db_id) as database:
                    schema, plans = convert.plan_database(
                        database, settings=settings.benchmark, prefix=args.prefix
                    )
                    report = convert.convert_database(
                        connection,
                        database,
                        schema=schema,
                        plans=plans,
                        settings=settings.benchmark,
                        readonly_role=settings.database.db_readonly_role,
                        replace=args.replace,
                    )
                reports.append(report.as_dict())
                logger.info("converted %s -> %s (%d rows)", db_id, schema, report.rows)
            except BenchmarkError as exc:
                failures += 1
                reports.append({"db_id": db_id, "error": str(exc)})
                logger.error("could not convert %s: %s", db_id, exc)
                if not args.keep_going:
                    break

    _write_report(args.report, {"converted": reports, "failures": failures})
    print(json.dumps({"databases": len(reports), "failures": failures}, indent=2))
    return EXIT_ERROR if failures and not args.keep_going else EXIT_OK


# --- verify ----------------------------------------------------------------


def command_verify(args: argparse.Namespace, settings: LoaderSettings) -> int:
    """Execute every gold query on both engines and compare the results."""
    questions = _read_questions(args)
    by_db: dict[str, list[Question]] = defaultdict(list)
    for question in questions:
        by_db[question.db_id].append(question)

    found = readers.find_databases(args.databases)
    selected = _select(found, only=args.only, limit=args.limit)

    reports: list[dict[str, Any]] = []
    unverified: list[str] = []

    with _connect(settings) as connection:
        for db_id, path in selected.items():
            if db_id not in by_db:
                logger.debug("%s has no questions; nothing to verify", db_id)
                continue
            schema = convert.schema_name_for(db_id, prefix=args.prefix)
            with open_database(path, db_id=db_id) as database:
                report = verify_database(
                    connection,
                    database,
                    by_db[db_id][: args.per_database] if args.per_database else by_db[db_id],
                    schema=schema,
                    statement_timeout_ms=settings.benchmark.benchmark_verify_timeout_ms,
                )
            reports.append(report.as_dict())
            if not report.verified:
                unverified.append(db_id)

    _write_report(args.report, {"verified": reports, "unverified": unverified})
    print(
        json.dumps(
            {
                "databases": len(reports),
                "unverified": unverified,
                "comparable": sum(int(r["comparable"]) for r in reports),
                "matched": sum(int(r["matched"]) for r in reports),
            },
            indent=2,
        )
    )
    if unverified:
        logger.error(
            "%d database(s) did not reproduce every gold result. Any number "
            "computed against them is not comparable to a published one.",
            len(unverified),
        )
        return EXIT_UNVERIFIED
    if not reports:
        logger.error("nothing was verified -- no database had questions to check")
        return EXIT_UNVERIFIED
    return EXIT_OK


# --- splits ----------------------------------------------------------------


def command_splits(args: argparse.Namespace, settings: LoaderSettings) -> int:
    """Assign databases to splits and write the committed question file."""
    questions = _read_questions(args)
    policy = splits.SplitPolicy(
        train=args.train, dev=args.dev, held_out=args.held_out, smoke=args.smoke, seed=args.seed
    )
    assignment = splits.assign((question.db_id for question in questions), policy=policy)
    stamped = splits.apply(questions, assignment)

    args.out.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}
    for split in Split:
        subset = [question for question in stamped if question.split is split]
        if not subset:
            continue
        path = args.out / f"{args.dataset}-{split.value}.jsonl"
        write_questions(path, subset)
        written[str(path)] = len(subset)

    summary = splits.summarise(assignment, stamped)
    (args.out / f"{args.dataset}-assignment.json").write_text(
        json.dumps(
            {
                "seed": policy.seed,
                "policy": {"train": policy.train, "dev": policy.dev, "held_out": policy.held_out},
                "databases": {db: split.value for db, split in sorted(assignment.items())},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"files": written, "summary": summary}, indent=2))
    return EXIT_OK


# --- shared ----------------------------------------------------------------


def _select(
    found: dict[str, Path], *, only: list[str] | None, limit: int | None
) -> dict[str, Path]:
    """Narrow the database set, refusing a ``--only`` name that is not present.

    Silently ignoring an unknown name would let a typo produce a run over zero
    databases that exits 0.
    """
    if only:
        missing = [name for name in only if name not in found]
        if missing:
            raise BenchmarkError(f"no such database(s): {sorted(missing)}")
        found = {name: found[name] for name in only}
    if limit is not None:
        found = dict(list(found.items())[:limit])
    return found


def _read_questions(args: argparse.Namespace) -> list[Question]:
    reader = readers.READERS[args.benchmark]
    questions: list[Question] = []
    for path in args.questions:
        questions.extend(reader(path))
    if not questions:
        raise BenchmarkError("no questions were read")
    return questions


def _connect(settings: LoaderSettings) -> psycopg.Connection[Any]:
    """The owner connection. Conversion writes, so the read-only role cannot do it.

    Not a bypass of the containment boundary: the boundary is about what
    *generated SQL* may do, and this is an operator running a loader. The
    boundary is re-asserted at the end of every conversion by granting the
    read-only role SELECT and nothing more.
    """
    url = settings.database.database_url
    if url is None:
        raise ConfigurationError("DATABASE_URL is required to convert or verify")
    return psycopg.connect(
        url.get_secret_value(),
        autocommit=True,
        connect_timeout=max(1, settings.database.db_connect_timeout_ms // 1000),
    )


def _write_report(path: Path | None, body: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s", path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmark.load",
        description="Acquire, convert, verify and split a text-to-SQL benchmark.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    get = subparsers.add_parser("acquire", help="download or adopt an archive, then extract it")
    get.add_argument("artifact", help="an allowlisted key, e.g. spider or bird-dev")
    get.add_argument("--archive", type=Path, help="a file already downloaded by hand")
    get.add_argument("--data-dir", type=Path, default=None)
    get.add_argument("--lockfile", type=Path, default=DEFAULT_LOCK_PATH)
    get.add_argument(
        "--trust-on-first-use",
        action="store_true",
        help="record this archive's digest if none is locked yet. Commit the result",
    )
    get.add_argument("--replace", action="store_true", help="remove a previous extraction first")
    get.set_defaults(handler=command_acquire)

    conv = subparsers.add_parser("convert", help="SQLite -> PostgreSQL, one schema per database")
    conv.add_argument(
        "--databases", type=Path, required=True, help="folder of <name>/<name>.sqlite"
    )
    conv.add_argument("--prefix", default="", help="schema name prefix, e.g. spider_")
    conv.add_argument("--only", nargs="*", help="convert just these db_ids")
    conv.add_argument("--limit", type=int, default=None)
    conv.add_argument("--replace", action="store_true", help="drop an existing schema first")
    conv.add_argument("--keep-going", action="store_true", help="continue past a failed database")
    conv.add_argument("--report", type=Path, default=None)
    conv.set_defaults(handler=command_convert)

    check = subparsers.add_parser("verify", help="gold queries must return identical results")
    check.add_argument("--databases", type=Path, required=True)
    check.add_argument("--questions", type=Path, nargs="+", required=True)
    check.add_argument("--benchmark", choices=sorted(readers.READERS), required=True)
    check.add_argument("--prefix", default="")
    check.add_argument("--only", nargs="*")
    check.add_argument("--limit", type=int, default=None)
    check.add_argument(
        "--per-database", type=int, default=None, help="check at most N queries per database"
    )
    check.add_argument("--report", type=Path, default=None)
    check.set_defaults(handler=command_verify)

    split = subparsers.add_parser("splits", help="assign databases to splits and write them")
    split.add_argument("--questions", type=Path, nargs="+", required=True)
    split.add_argument("--benchmark", choices=sorted(readers.READERS), required=True)
    split.add_argument("--dataset", default="spider")
    split.add_argument("--out", type=Path, default=Path("data/splits"))
    split.add_argument("--train", type=float, default=0.70)
    split.add_argument("--dev", type=float, default=0.15)
    split.add_argument("--held-out", type=float, default=0.15)
    split.add_argument(
        "--smoke", type=float, default=0.025, help="fraction of the corpus, carved out of dev"
    )
    split.add_argument("--seed", default="t2sql-v1")
    split.set_defaults(handler=command_splits)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        settings = LoaderSettings.load()
        handler: Any = args.handler
        return int(handler(args, settings))
    except (BenchmarkError, ConfigurationError, KeyError) as exc:
        logger.error("%s", exc)
        return EXIT_ERROR
    except psycopg.Error as exc:
        logger.error("database error: %s", str(exc).splitlines()[0])
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
