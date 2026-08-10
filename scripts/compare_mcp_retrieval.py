"""Does the MCP hop change what retrieval returns? Ask every question in a split.

The `mcp-retrieval` baseline exists to put a number beside `retrieval-only`,
and a difference between two accuracy figures is only attributable to the
transport if the transport is the only thing that differs. That is a claim
about **retrieval**, and it can be settled without spending a single token: run
each question through both paths and compare the elements that come back.

This is the stronger measurement of the two, and the cheaper one. An accuracy
comparison over a 100-question subset carries a confidence interval wide enough
to hide a real regression; this compares 921 ordered element lists exactly, and
a single differing pair is a finding.

Reports, on stdout, as JSON:

    identical          questions where both paths returned the same ordered list
    differing          questions where they did not, with the first few named
    empty_both         questions where neither retrieved anything
    server_starts      server launches -- one per database on an ordered split

Usage::

    python scripts/compare_mcp_retrieval.py \\
        --questions data/splits/spider-official-dev.jsonl \\
        --prefix spider_ --top-k 30
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from adapters.embedding.factory import build_embedder
from benchmark.convert import schema_name_for
from core.dsn import libpq_dsn, redact_dsn
from core.exceptions import RetrievalError
from core.settings import Settings
from evals.dataset import Split, load_questions
from evals.mcp_client import McpClientPool
from schema.retrieval import RetrievalResult, SchemaRetriever

logger = logging.getLogger("compare")

type Elements = list[tuple[str, str | None]]


def elements_of(result: RetrievalResult) -> Elements:
    """What is compared: the ordered identities, and nothing else.

    Scores are excluded deliberately. The wire rounds them to four decimal
    places, so comparing them would report a difference that changes no prompt
    and no metric -- and a check that cries wolf about rounding is a check
    nobody runs twice. What the prompt is built from is the ordered
    ``(table, column)`` list, and that is what has to match.
    """
    return [(element.table, element.column) for element in result.elements]


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. No interpolation, no numpy for four numbers."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
    return round(ordered[index], 2)


@dataclass(slots=True)
class Report:
    identical: int = 0
    differing: int = 0
    empty_both: int = 0
    failed: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)
    direct_ms: list[float] = field(default_factory=list)
    wire_ms: list[float] = field(default_factory=list)

    def latency(self) -> dict[str, Any]:
        """What the wire *does* cost, since it is not costing correctness.

        Excludes server start, deliberately and separately reported: a start is
        paid once per database and a call is paid once per question, so folding
        them together would spread twenty model loads across a thousand
        questions and describe neither.
        """
        return {
            "direct_p50_ms": percentile(self.direct_ms, 0.50),
            "direct_p95_ms": percentile(self.direct_ms, 0.95),
            "wire_p50_ms": percentile(self.wire_ms, 0.50),
            "wire_p95_ms": percentile(self.wire_ms, 0.95),
            "overhead_p50_ms": round(
                percentile(self.wire_ms, 0.50) - percentile(self.direct_ms, 0.50), 2
            ),
        }

    def record(self, question_id: str, direct: Elements, wire: Elements) -> None:
        if direct == wire:
            self.identical += 1
            if not direct:
                self.empty_both += 1
            return

        self.differing += 1
        if len(self.examples) < 5:
            self.examples.append(
                {
                    "question_id": question_id,
                    "only_direct": [e for e in direct if e not in wire][:8],
                    "only_wire": [e for e in wire if e not in direct][:8],
                    "same_set_different_order": sorted(map(str, direct)) == sorted(map(str, wire)),
                }
            )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)-8s %(message)s")
    args = _parser().parse_args(argv)

    settings = Settings.load()
    top_k = args.top_k or settings.retrieval.retrieval_top_k
    questions = load_questions(args.questions, split=args.split)
    if args.limit is not None:
        questions = questions[: args.limit]

    url = settings.database.database_url
    if url is None:
        logger.error("DATABASE_URL is required")
        return 2
    try:
        owner = psycopg.connect(libpq_dsn(url), autocommit=True)
    except psycopg.Error as exc:
        logger.error("could not connect: %s", redact_dsn(str(exc)).strip())
        return 3

    report = Report()
    embedder = build_embedder(settings.retrieval)
    retrievers: dict[str, SchemaRetriever] = {}
    pool = McpClientPool(max_live=args.mcp_max_live)

    try:
        for index, question in enumerate(questions, 1):
            dataset = schema_name_for(question.db_id, prefix=args.prefix)
            try:
                # The wire first. It is the side that can fail in ways worth
                # seeing early -- a server that will not start should stop this
                # after one question rather than after nine hundred.
                #
                # Started outside the timed section, so a model load is not
                # charged to the question that happened to follow an eviction.
                client = pool.acquire(dataset)
                started = time.perf_counter()
                wire = client.search(question.question, k=top_k)
                wire_ms = (time.perf_counter() - started) * 1000

                retriever = retrievers.get(dataset)
                if retriever is None:
                    retriever = SchemaRetriever(owner, embedder, dataset=dataset, default_k=top_k)
                    retrievers[dataset] = retriever
                started = time.perf_counter()
                direct = retriever.search(question.question, k=top_k)
                direct_ms = (time.perf_counter() - started) * 1000
            except RetrievalError as exc:
                report.failed += 1
                logger.warning("%s: %s", question.question_id, exc)
                continue

            report.record(question.question_id, elements_of(direct), elements_of(wire))
            report.direct_ms.append(direct_ms)
            report.wire_ms.append(wire_ms)
            if index % 50 == 0:
                logger.info(
                    "%d/%d  identical=%d differing=%d",
                    index,
                    len(questions),
                    report.identical,
                    report.differing,
                )
    finally:
        starts = pool.starts
        pool.close()
        owner.close()

    json.dump(
        {
            "questions": len(questions),
            "identical": report.identical,
            "differing": report.differing,
            "empty_both": report.empty_both,
            "failed": report.failed,
            "databases": len(retrievers),
            "server_starts": starts,
            "top_k": top_k,
            "latency": report.latency(),
            "examples": report.examples,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0 if report.differing == 0 and report.failed == 0 else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="compare_mcp_retrieval", description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--split", type=Split, choices=list(Split), default=Split.DEV)
    parser.add_argument("--prefix", default="")
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--mcp-max-live", type=int, default=1)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
