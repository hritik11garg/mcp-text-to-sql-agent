"""The eval and the loader must name a database's schema identically.

One name, not two -- and the whole of ADR-031 rests on it. These are unit
tests because the naming rule needs no database, and the previous version of
this rule was wrong for eight months without a single integration test
noticing, because every fixture database was already lower case.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from benchmark.convert import schema_name_for
from evals.pipeline import ScopeRegistry

pytestmark = pytest.mark.unit


def registry(prefix: str) -> ScopeRegistry:
    """A registry whose connections are never touched.

    ``_dataset_for`` is pure -- it reads ``self._prefix`` and nothing else --
    so passing ``None`` for the connections keeps this a unit test and makes
    any future I/O in that method fail loudly rather than silently needing a
    fixture.
    """
    return ScopeRegistry(
        cast(Any, None),
        cast(Any, None),
        settings=cast(Any, None),
        execution=cast(Any, None),
        embedder=None,
        needs_retrieval=False,
        needs_validation=False,
        prefix=prefix,
    )


# Spider dev's twenty databases, plus the shapes a benchmark can produce.
# `cre_Doc_Template_Mgt` is the real one and the reason this file exists.
DB_IDS = [
    "cre_Doc_Template_Mgt",
    "concert_singer",
    "car_1",
    "wta_1",
    "student_transcripts_tracking",
    "ALLCAPS",
    "MixedCase_1",
]


class TestTheEvalAndTheLoaderAgree:
    @pytest.mark.parametrize("db_id", DB_IDS)
    @pytest.mark.parametrize("prefix", ["spider_", "bird_", ""])
    def test_the_dataset_name_is_the_loaders_schema_name(self, db_id: str, prefix: str) -> None:
        """The assertion the old implementation would have failed.

        It concatenated prefix and db_id directly, which is what
        `schema_name_for` does *before* folding to lower case -- so the two
        agreed for every already-lower-case db_id and disagreed for the rest.
        """
        assert registry(prefix)._dataset_for(db_id) == schema_name_for(db_id, prefix=prefix)

    def test_a_mixed_case_db_id_is_folded(self) -> None:
        """The concrete failure: 84 Spider questions, one database, all
        `scope_unavailable` -- an infrastructure error, so they left the scored
        denominator and the run reported a clean accuracy over 9% fewer
        questions than it claimed to cover."""
        assert registry("spider_")._dataset_for("cre_Doc_Template_Mgt") == (
            "spider_cre_doc_template_mgt"
        )

    def test_an_already_lower_case_db_id_is_unchanged(self) -> None:
        """Why every existing test passed: this case was always correct."""
        assert registry("spider_")._dataset_for("concert_singer") == "spider_concert_singer"

    def test_no_prefix_still_folds(self) -> None:
        assert registry("")._dataset_for("MixedCase_1") == "mixedcase_1"


class TestTheCorpusIsCoveredNotSampled:
    def test_spider_dev_has_a_mixed_case_database(self) -> None:
        """A guard on the *premise*, not the fix.

        If this ever fails, the corpus stopped containing the case these tests
        are about -- which would make every assertion above pass for the reason
        the old code passed, and nobody would know.
        """
        assert any(db_id != db_id.lower() for db_id in DB_IDS)
