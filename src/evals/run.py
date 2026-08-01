"""`python -m evals.run` — the one command EVALUATION.md section 3 requires.

Deliberately thin. Everything it does is available as a library call, because a
harness whose logic lives in its argument parser cannot be tested and cannot be
driven from anywhere else.

Two behaviours worth knowing before spending a token budget on it:

**It resumes.** Re-running the same ``--run-id`` skips questions already
recorded, so a spent daily cap costs the questions still outstanding rather
than the whole run. Changing the model, the prompt version or the commit
changes the configuration fingerprint and the resume is *refused* -- see
`RunStore.resume`.

**It writes progress to stderr.** stdout carries the summary JSON, so the
command composes: ``python -m evals.run ... | jq .execution_accuracy``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from evals.artifacts import QuestionArtifact, RunManifest, RunStore, current_commit, new_run_id
from evals.dataset import Split, load_questions
from evals.runner import Answerer, EvalRunner, QueryRunner

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
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)-8s %(message)s",
        force=True,
    )
    args = build_parser().parse_args(argv)

    questions = load_questions(args.questions, split=args.split)
    if args.limit is not None:
        questions = questions[: args.limit]
    if not questions:
        logger.error("no questions in %s for split %s", args.questions, args.split)
        return 2

    manifest = RunManifest(
        run_id=args.run_id or new_run_id(f"{args.dataset}-{args.split.value}"),
        dataset=args.dataset,
        split=args.split.value,
        model=args.model,
        retriever_model_version=args.retriever,
        prompt_version=args.prompt_version,
        commit=current_commit(),
        seed=args.seed,
        notes=args.notes,
    )
    store = RunStore(args.out, manifest)

    try:
        answerer, run_query = build_pipeline(args)
    except NotImplementedError as exc:
        logger.error("%s", exc)
        return 3

    runner = EvalRunner(store, answerer, run_query, on_progress=progress_line)
    summary = runner.run(questions)
    emit_summary({**summary.to_dict(), "run_id": manifest.run_id, "out": str(store.root)})
    return 0


def build_pipeline(args: argparse.Namespace) -> tuple[Answerer, QueryRunner]:
    """Construct the thing under test and the thing that runs SQL.

    The single wiring point, and the only part of this module that will change
    when the pipeline is connected. Everything above it -- loading, the
    manifest, resumption, the summary -- is complete and exercised by tests.

    Raises:
        NotImplementedError: until a dataset exists to run against. Refusing is
            the honest failure. The alternative is a harness that runs happily,
            records every question as unanswered, and reports 0% in a format
            indistinguishable from a measurement.
    """
    raise NotImplementedError(
        "no pipeline is wired up yet, so there is nothing to measure. The "
        "harness itself -- result comparison, Recall@k, artifacts, resumable "
        "runs -- is built and tested; loading a benchmark and connecting the "
        f"retriever/generator/executor is the next slice. (Would have run "
        f"{len(load_questions(args.questions, split=args.split))} question(s).)"
    )


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
