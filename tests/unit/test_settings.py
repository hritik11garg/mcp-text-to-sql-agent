"""Configuration must fail fast, and limits must be clamped, not trusted."""

from __future__ import annotations

import inspect
import re

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings
from tests.conftest import REPO_ROOT

from core import settings as settings_module
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


def _settings_env_names() -> list[str]:
    """Every environment variable any settings class reads."""
    names: set[str] = set()
    for member in vars(settings_module).values():
        if (
            inspect.isclass(member)
            and issubclass(member, BaseSettings)
            and member is not BaseSettings
        ):
            names |= {field.upper() for field in member.model_fields}
    return sorted(names)


class TestEverySettingIsDocumented:
    """A setting an operator cannot discover is a setting they cannot apply.

    This matters most for the ones that are security controls with safe
    defaults -- ``PROFILE_ALLOW_VALUE_SAMPLING`` and ``LLM_ALLOWED_HOSTS`` are
    correct out of the box, which is exactly why nobody notices they are
    missing from the file operators actually edit.

    It matters second-most for the ones that silently change what a measured
    number means: ``RETRIEVAL_TOP_K`` moved execution accuracy 30 points and
    ``LLM_MODEL_FALLBACKS`` turns a run into a blend of two models.

    Asserted rather than reviewed, because this drifted once already: 18 of 50
    settings had reached the code without reaching the template.
    """

    @pytest.mark.parametrize("name", _settings_env_names())
    def test_it_appears_in_the_env_template(self, name: str) -> None:
        template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

        # Commented-out is documentation too -- several settings are shown as
        # `# NAME=value` precisely because their default should not be edited
        # casually. What is refused is absence.
        assert re.search(rf"^#?\s*{name}=", template, re.MULTILINE), (
            f"{name} is read from the environment but absent from .env.example. "
            f"Add it, with a comment saying what happens if it is wrong."
        )

    @pytest.mark.parametrize("name", _settings_env_names())
    def test_it_appears_in_the_configuration_reference(self, name: str) -> None:
        reference = (REPO_ROOT / "docs" / "operations" / "CONFIG.md").read_text(encoding="utf-8")

        assert name in reference, f"{name} is undocumented in docs/operations/CONFIG.md"


def _env_template_names() -> list[str]:
    """Every ``NAME=`` in `.env.example`, commented or not."""
    template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    return sorted(
        {m.group(1) for m in re.finditer(r"^#?\s*([A-Z][A-Z0-9_]*)=", template, re.MULTILINE)}
    )


NOT_SETTINGS = {
    # docker-compose reads these directly; they configure the container, not
    # the application, and never reach pydantic-settings.
    "POSTGRES_DB",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    # migrations/versions/002_readonly_role.py reads this via os.environ. It
    # cannot be a setting: alembic runs without the application's Settings, and
    # the password must not sit in a settings object that gets logged.
    "SQL_AGENT_RO_PASSWORD",
}
"""Variables that legitimately appear in `.env.example` while no settings class
reads them, each with the consumer named.

An explicit set rather than a pattern, so adding one is a deliberate act that
shows up in a diff. The whole point of the test below is that the default
answer is *no*.
"""


class TestTheTemplateHasNothingDead:
    """`.env.example` must not offer a variable nothing reads.

    The reverse of :class:`TestEverySettingIsDocumented`, and it catches a
    worse failure. A missing setting is invisible; a **dead** one is worse than
    invisible, because an operator sets it, sees no error, and concludes it
    took effect.

    It had already happened. `LOG_LEVEL`, `LOG_FORMAT` and `LOG_RESULT_VALUES`
    sat uncommented with values while nothing in the codebase read any of them
    -- and CONFIG.md section 7 correctly called them planned, so the two files
    disagreed in the dangerous direction: the one operators actually edit was
    the one implying the controls worked.

    `LOG_RESULT_VALUES=false` is the reason this is a security test and not a
    tidiness one. It carried a comment describing what it protects, so a reader
    would conclude result logging was off *by policy*. It is off because the
    feature does not exist -- a different fact, and one that stops being true
    the moment somebody adds one.
    """

    @pytest.mark.parametrize("name", _env_template_names())
    def test_every_variable_is_read_by_something(self, name: str) -> None:
        if name in NOT_SETTINGS:
            pytest.skip(f"{name} is consumed outside pydantic-settings; see NOT_SETTINGS")

        assert name in _settings_env_names(), (
            f"{name} is offered in .env.example but no settings class reads it. "
            f"An operator who sets it gets no error and no effect. Either wire "
            f"it up, comment it out with a note saying it is not read yet, or "
            f"add it to NOT_SETTINGS naming what does consume it."
        )

    def test_the_allowlist_itself_is_not_stale(self) -> None:
        """An entry that is no longer in the template is an entry nobody rechecked."""
        unused = NOT_SETTINGS - set(_env_template_names())
        assert not unused, (
            f"NOT_SETTINGS names {sorted(unused)}, which .env.example no longer "
            f"contains. Remove them -- an allowlist nobody prunes stops being read."
        )
