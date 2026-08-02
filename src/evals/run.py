"""`python -m evals.run` — the one command EVALUATION.md section 3 requires.

Deliberately thin. Everything it does is available as a library call, because a
harness whose logic lives in its argument parser cannot be tested and cannot be
driven from anywhere else.

Three behaviours worth knowing before spending a token budget on it:

**It resumes.** Re-running the same ``--run-id`` skips questions already
recorded, so a spent daily cap costs the questions still outstanding rather
than the whole run. Changing the model, the prompt version or the commit
changes the configuration fingerprint and the resume is *refused* -- see
`RunStore.resume`.

**It writes progress to stderr.** stdout carries the summary JSON, so the
command composes: ``python -m evals.run ... | jq .execution_accuracy``.

**``--gold`` is required, and that is a correctness control rather than a
convenience.** A split file holds the benchmark's own SQLite SQL; the target is
PostgreSQL. The file named here is what ``benchmark.load verify`` produced --
the statement each gold query became *and* whether comparing its results
against SQLite succeeded. Making it optional would allow a run whose reference
answers nobody had checked, and that run would report a number about the loader
while looking like a number about the model. See :mod:`evals.gold`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import psycopg

from adapters.embedding.factory import build_embedder
from adapters.llm.factory import build_llm_client
from core.dsn import libpq_dsn, redact_dsn
from core.exceptions import ConfigurationError
from core.settings import Settings
from evals.artifacts import QuestionArtifact, RunManifest, RunStore, current_commit, new_run_id
from evals.dataset import Question, Split, load_questions
from evals.gold import Applied, apply_verified_gold, load_verified_gold
from evals.pipeline import (
    Baseline,
    PipelineAnswerer,
    SchemaScopedQueryRunner,
    ScopeRegistry,
)
from evals.runner import EvalRunner
from generation.generator import SQLGenerator

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Run the eval harness over a split and write artifacts.",
    )
    parser.add_argument("--questions", type=Path, required=True, help="JSONL question file")
    parser.add_argument(
        "--split",
        type=Split,
        choices=list(Split),
        default=Split.DEV,
        help="Only questions in this split. Report from held-out, iterate on dev",
    )
    parser.add_argument("--out", type=Path, default=Path("results"), help="Results root")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Reuse an existing run id to resume it. Omit to start a new run",
    )
    parser.add_argument("--dataset", default="default")
    parser.add_argument("--model", default="", help="Recorded in the manifest, not used to call")
    parser.add_argument("--retriever", default="", help="Retriever model_version")
    parser.add_argument("--prompt-version", default="sql_gen/v1")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after this many questions. For smoke-testing the harness itself",
    )
    parser.add_argument(
        "--gold",
        type=Path,
        required=True,
        help=(
            "Verified gold SQL from `benchmark.load verify --emit-gold`. Required: "
            "the split holds the benchmark's own SQLite SQL, and scoring against "
            "an unverified conversion measures the loader, not the model"
        ),
    )
    parser.add_argument(
        "--baseline",
        type=Baseline,
        choices=list(Baseline),
        default=Baseline.RETRIEVAL_ONLY,
        help="Which EVALUATION.md section 4 configuration to run",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="Schema-name prefix used at conversion time, e.g. spider_",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Schema elements retrieved per question. Defaults to RETRIEVAL_TOP_K",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)-8s %(message)s",
        force=True,
    )
    args = build_parser().parse_args(argv)

    try:
        questions, applied = prepare_questions(args)
    except (ValueError, OSError) as exc:
        logger.error("%s", exc)
        return 2
    if not questions:
        logger.error("no scoreable questions in %s for split %s", args.questions, args.split)
        return 2

    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2

    manifest = RunManifest(
        run_id=args.run_id or new_run_id(f"{args.dataset}-{args.split.value}"),
        dataset=args.dataset,
        split=args.split.value,
        model=args.model or settings.llm.llm_model,
        retriever_model_version=args.retriever,
        prompt_version=args.prompt_version,
        commit=current_commit(),
        seed=args.seed,
        notes=args.notes,
        baseline=args.baseline.value,
    )
    store = RunStore(args.out, manifest)

    try:
        owner, readonly = _connections(settings)
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 3

    try:
        answerer, run_query = build_pipeline(
            args, settings, owner=owner, readonly=readonly, schemas=applied.schemas
        )
        with answerer:
            runner = EvalRunner(store, answerer, run_query, on_progress=progress_line)
            summary = runner.run(questions)
    finally:
        owner.close()
        readonly.close()

    emit_summary(
        {
            **summary.to_dict(),
            "run_id": manifest.run_id,
            "out": str(store.root),
            "baseline": args.baseline.value,
            # The exclusions travel with the score, always. A denominator that
            # 113 questions were removed from is not the same measurement as
            # one they were never in, and only one of those is honest to
            # compare against a published number.
            "questions": applied.to_dict(),
        }
    )
    return 0


def prepare_questions(args: argparse.Namespace) -> tuple[list[Question], Applied]:
    """Load the split, swap in verified gold, and drop what cannot be scored.

    ``--limit`` is applied **after** the exclusions, so a smoke run of twenty
    questions is twenty scoreable ones rather than twenty rows off the top of a
    file, some of which quietly vanish.
    """
    questions = load_questions(args.questions, split=args.split)
    applied = apply_verified_gold(questions, load_verified_gold(args.gold))

    kept = applied.questions
    if args.limit is not None:
        kept = kept[: args.limit]
    return kept, applied


def build_pipeline(
    args: argparse.Namespace,
    settings: Settings,
    *,
    owner: psycopg.Connection[Any],
    readonly: psycopg.Connection[Any],
    schemas: dict[str, str],
) -> tuple[PipelineAnswerer, SchemaScopedQueryRunner]:
    """Construct the thing under test and the thing that runs SQL.

    The single wiring point. Everything above it -- loading, the manifest,
    resumption, the summary -- was complete and tested before this existed,
    which is what kept this function to a composition rather than a rewrite.

    Two roles, not one. The answerer reaches ``agent_meta`` for the catalog and
    the vectors, which requires the owner; everything that touches *benchmark*
    data goes through the read-only role. A benchmark run is not a reason to
    relax the boundary -- it is the run most likely to execute a query nobody
    has read.
    """
    baseline: Baseline = args.baseline
    needs_retrieval = baseline is not Baseline.FULL_SCHEMA

    scopes = ScopeRegistry(
        owner,
        readonly,
        settings=settings.retrieval,
        execution=settings.execution,
        # Built only when something will embed with it. Loading a
        # sentence-transformer for the full-schema baseline would spend a
        # minute and several hundred megabytes to produce vectors nothing reads.
        embedder=build_embedder(settings.retrieval) if needs_retrieval else None,
        needs_retrieval=needs_retrieval,
        needs_validation=baseline is Baseline.WITH_VALIDATION,
        prefix=args.prefix,
    )

    generator = SQLGenerator(
        build_llm_client(settings.llm),
        max_rows=settings.execution.max_rows_default,
    )
    answerer = PipelineAnswerer(scopes, generator, baseline=baseline, top_k=args.top_k)

    run_query = SchemaScopedQueryRunner(
        readonly,
        schema_for=schemas,
        statement_timeout_ms=settings.execution.clamp_timeout_ms(None),
        prefix=args.prefix,
    )
    return answerer, run_query


def _connections(
    settings: Settings,
) -> tuple[psycopg.Connection[Any], psycopg.Connection[Any]]:
    """Open both roles, failing before any question is attempted."""
    owner = _connect(settings.database.database_url, "DATABASE_URL", settings=settings)
    try:
        readonly = _connect(settings.database.database_ro_url, "DATABASE_RO_URL", settings=settings)
    except ConfigurationError:
        owner.close()
        raise
    return owner, readonly


def _connect(url: object, variable: str, *, settings: Settings) -> psycopg.Connection[Any]:
    if url is None:
        raise ConfigurationError(f"{variable} is required to run an evaluation")
    try:
        return psycopg.connect(
            libpq_dsn(url),
            autocommit=True,
            connect_timeout=max(1, settings.database.db_connect_timeout_ms // 1000),
        )
    except psycopg.Error as exc:
        # psycopg quotes the DSN back on a parse error, password included.
        raise ConfigurationError(
            f"could not connect using {variable}: {redact_dsn(str(exc)).strip()}"
        ) from None


def progress_line(artifact: QuestionArtifact) -> None:
    """One line per question, on stderr.

    A long run with no output is indistinguishable from a hung one, and the
    first thing anyone does about that is kill it.
    """
    mark = "ok  " if artifact.matched else "FAIL"
    logger.info(
        "%s %-24s %s (%.0f ms)",
        mark,
        artifact.question_id,
        artifact.failure_category,
        artifact.duration_ms,
    )


def emit_summary(payload: dict[str, object]) -> None:
    """Machine-readable, on stdout, for the BENCHMARKS row."""
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
