"""Configuration must fail fast, and limits must be clamped, not trusted."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.exceptions import ConfigurationError
from core.settings import (
    DatabaseSettings,
    ExecutionSettings,
    LLMProvider,
    LLMSettings,
    RetrievalSettings,
)

pytestmark = pytest.mark.unit


class TestLLMSettings:
    def test_fake_provider_needs_nothing(self) -> None:
        settings = LLMSettings(llm_provider=LLMProvider.FAKE)
        assert settings.llm_provider is LLMProvider.FAKE

    def test_missing_model_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="LLM_MODEL"):
            LLMSettings(llm_provider=LLMProvider.OPENAI_COMPATIBLE)

    def test_missing_base_url_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="LLM_BASE_URL"):
            LLMSettings(llm_provider=LLMProvider.OPENAI_COMPATIBLE, llm_model="m")


class TestDatabaseSettings:
    def test_pool_bounds_must_be_ordered(self) -> None:
        with pytest.raises(ConfigurationError, match="DB_POOL_MAX_SIZE"):
            DatabaseSettings(db_pool_min_size=10, db_pool_max_size=2)

    def test_read_only_url_must_differ_from_owner_url(self) -> None:
        """The read-only role is the outermost containment boundary.

        If both URLs are the same role, every guarantee in SECURITY.md is void
        while everything still appears to work.
        """
        same = "postgresql://owner:pw@localhost/analytics"
        with pytest.raises(ConfigurationError, match="must differ"):
            DatabaseSettings(database_url=same, database_ro_url=same)

    def test_distinct_roles_are_accepted(self) -> None:
        settings = DatabaseSettings(
            database_url="postgresql://owner:pw@localhost/analytics",
            database_ro_url="postgresql://ro:pw@localhost/analytics",
        )
        assert settings.database_url is not None


class TestExecutionSettings:
    def test_default_above_ceiling_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="MAX_ROWS_DEFAULT"):
            ExecutionSettings(max_rows_default=10_000, max_rows_ceiling=5_000)

    def test_timeout_above_ceiling_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="STATEMENT_TIMEOUT_MS"):
            ExecutionSettings(statement_timeout_ms=90_000, statement_timeout_ceiling_ms=60_000)

    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            (None, 500),  # default applies
            (100, 100),  # under the ceiling, honoured
            (99_999, 5_000),  # over the ceiling, clamped
            (0, 1),  # nonsense, floored
            (-5, 1),  # hostile, floored
        ],
    )
    def test_row_limit_is_clamped_not_trusted(self, requested: int | None, expected: int) -> None:
        assert ExecutionSettings().clamp_rows(requested) == expected

    @pytest.mark.parametrize(
        ("requested", "expected"),
        [(None, 30_000), (5_000, 5_000), (999_999, 60_000), (1, 100)],
    )
    def test_timeout_is_clamped_not_trusted(self, requested: int | None, expected: int) -> None:
        assert ExecutionSettings().clamp_timeout_ms(requested) == expected


class TestRetrievalSettings:
    """The tuning knobs are bounded at the configuration edge as well as at the
    retriever, because an operator typo and a hostile caller are different
    threats and only one of them is clamped at request time."""

    def test_defaults_match_the_documented_contract(self) -> None:
        settings = RetrievalSettings()

        assert settings.retrieval_top_k == 10
        assert settings.hnsw_ef_search == 40

    @pytest.mark.parametrize("top_k", [0, -1, 51, 10_000])
    def test_top_k_outside_the_ceiling_is_refused(self, top_k: int) -> None:
        with pytest.raises(ValidationError):
            RetrievalSettings(retrieval_top_k=top_k)

    @pytest.mark.parametrize("ef_search", [0, -1, 1_001])
    def test_ef_search_outside_its_range_is_refused(self, ef_search: int) -> None:
        with pytest.raises(ValidationError):
            RetrievalSettings(hnsw_ef_search=ef_search)
