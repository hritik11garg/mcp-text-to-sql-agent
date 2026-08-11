"""A database cannot be both trained on and reported from.

This is the check Stage 5 lists as *"verify database-level split disjointness"*,
written before Stage 5 rather than during it, because of what an audit of the
existing split found.

**11 of the 20 databases in Spider's official dev set hash into this project's
`train` band, carrying 605 questions.** Every accuracy and Recall figure this
project publishes is measured on those 20 databases. A fine-tune over the
`train` split would therefore have fitted the retriever to 11 of the 20 schemas
it is scored against.

The reason that is worth a guard rather than a note is the shape of the failure.
It raises no error and produces no implausible number -- it produces a *better*
Recall@k, arriving precisely when a fine-tune is being evaluated for whether it
improved Recall@k. Both the before and the after would look correct, the
ablation would report a gain, and the gain would be memorisation.

ADR-021 anticipated the fix in its own Revisit clause: *if a benchmark ships an
official split, use it -- comparability beats internal consistency*. ADR-047
takes it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark.splits import SplitPolicy, assign, leaked_databases
from evals.dataset import Split

pytestmark = pytest.mark.unit

SPLITS = Path(__file__).resolve().parents[2] / "data" / "splits"

# Recorded by name so the test says which rather than how many. Regenerating the
# split with --reserve is what makes this list historical.
#
# Sixteen of Spider's twenty dev databases landed in a band this project would
# have trained on or iterated against. **Eleven were `train`** -- the fitting
# set, carrying 605 questions, and the sharp half of the finding. The other five
# were `dev`/`smoke`: not fitted to, but tuned against, which disqualifies them
# from being called held-out just the same.
TRAIN_LEAK = frozenset(
    {
        "concert_singer",
        "course_teach",
        "dog_kennels",
        "employee_hire_evaluation",
        "museum_visit",
        "poker_player",
        "singer",
        "student_transcripts_tracking",
        "tvshow",
        "world_1",
        "wta_1",
    }
)

ITERATED_LEAK = frozenset(
    {"cre_Doc_Template_Mgt", "flight_2", "network_1", "real_estate_properties", "voter_1"}
)

KNOWN_LEAK = TRAIN_LEAK | ITERATED_LEAK


class TestReservationBeatsTheHash:
    def test_a_reserved_database_is_never_trained_on(self) -> None:
        names = [f"db_{i}" for i in range(200)]
        reserved = frozenset(names[:40])
        assignment = assign(names, reserved=reserved)

        for db in reserved:
            assert assignment[db] is Split.HELD_OUT

    def test_reserving_nothing_leaves_the_hash_alone(self) -> None:
        """The parameter must not change behaviour for callers that omit it."""
        names = [f"db_{i}" for i in range(200)]
        assert assign(names) == assign(names, reserved=())

    def test_the_unreserved_databases_keep_their_buckets(self) -> None:
        """ADR-021's property survives the new parameter.

        Membership must still depend on the name alone -- reserving one database
        may not move a different one, or the split stops being stable under
        change and the whole hashing argument collapses.
        """
        names = [f"db_{i}" for i in range(200)]
        plain = assign(names)
        reserved = assign(names, reserved={"db_7", "db_11"})

        for db in names:
            if db not in {"db_7", "db_11"}:
                assert reserved[db] is plain[db], (
                    f"{db} moved because a different database was reserved"
                )

    def test_reserving_a_database_that_is_absent_is_not_an_error(self) -> None:
        """Spider's dev list may name databases a partial load never converted."""
        assignment = assign(["a", "b"], reserved={"a", "not_loaded"})
        assert assignment == {"a": Split.HELD_OUT, "b": assignment["b"]}
        assert "not_loaded" not in assignment


class TestTheLeakDetector:
    def test_it_finds_a_database_that_is_trained_on_and_evaluated(self) -> None:
        assignment = {"shared": Split.TRAIN, "safe": Split.HELD_OUT}
        assert leaked_databases(assignment, {"shared", "safe"}) == frozenset({"shared"})

    def test_held_out_is_not_a_leak(self) -> None:
        assignment = {"reported": Split.HELD_OUT}
        assert leaked_databases(assignment, {"reported"}) == frozenset()

    @pytest.mark.parametrize("split", [Split.TRAIN, Split.DEV, Split.SMOKE])
    def test_dev_and_smoke_count_as_trained_on(self, split: Split) -> None:
        """Not fitted to, but iterated against.

        A number reported from a database whose prompts were tuned on it is not
        a held-out number either, and the distinction is easy to lose because
        only ``TRAIN`` sounds dangerous.
        """
        assert leaked_databases({"db": split}, {"db"}) == frozenset({"db"})

    def test_an_empty_evaluation_set_leaks_nothing(self) -> None:
        assert leaked_databases({"db": Split.TRAIN}, ()) == frozenset()


class TestTheCommittedSplitIsClean:
    """Run against the real files, because the defect was in the real files."""

    @pytest.fixture(scope="class")
    @classmethod
    def committed(cls) -> dict[str, Split]:
        path = SPLITS / "spider-assignment.json"
        if not path.is_file():
            pytest.skip("no committed Spider split in this checkout")
        raw = json.loads(path.read_text(encoding="utf-8"))["databases"]
        return {db: Split(value) for db, value in raw.items()}

    @pytest.fixture(scope="class")
    @classmethod
    def official_dev(cls) -> frozenset[str]:
        path = SPLITS / "spider-official-dev.jsonl"
        if not path.is_file():
            pytest.skip("no official Spider dev file in this checkout")
        return frozenset(
            str(json.loads(line)["db_id"])
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def test_no_evaluated_database_is_trained_on(
        self, committed: dict[str, Split], official_dev: frozenset[str]
    ) -> None:
        """The assertion that would have failed before ADR-047.

        It named eleven databases and 605 questions. If it fails again, a split
        was regenerated without ``--reserve`` and Stage 5 must not run on it.
        """
        leaked = leaked_databases(committed, official_dev)
        assert not leaked, (
            f"{len(leaked)} evaluated databases are in a trained-on split: {sorted(leaked)}. "
            f"Regenerate with `--reserve data/splits/spider-official-dev.jsonl` (ADR-047)"
        )

    def test_the_reservation_would_have_caught_the_known_leak(
        self, official_dev: frozenset[str]
    ) -> None:
        """Pins the defect itself, not just its absence.

        Asserting only that today's split is clean would keep passing if the
        reservation were deleted and the split simply never regenerated. This
        rebuilds the old assignment and proves the guard changes its verdict.
        """
        without = assign(official_dev, policy=SplitPolicy())
        assert leaked_databases(without, official_dev) == KNOWN_LEAK
        assert {db for db, s in without.items() if s is Split.TRAIN} == TRAIN_LEAK

        with_reservation = assign(official_dev, policy=SplitPolicy(), reserved=official_dev)
        assert leaked_databases(with_reservation, official_dev) == frozenset()
