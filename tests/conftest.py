"""Shared fixtures.

Fixtures build data; tests assert on it. A fixture that asserts is doing the
test's job. See docs/development/CODE_STYLE.md section 11.
"""

from __future__ import annotations

import pytest

from adapters.llm.fake import FakeLLMClient
from core.settings import AgentSettings, ExecutionSettings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the developer's real .env from leaking into tests.

    Without this, a populated .env makes settings tests pass locally and fail
    in CI -- or worse, pass in both while asserting nothing.

    No teardown needed: monkeypatch restores the environment itself.
    """
    for var in (
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_ALLOWED_HOSTS",
        "DATABASE_URL",
        "DATABASE_RO_URL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def execution_settings() -> ExecutionSettings:
    return ExecutionSettings()


@pytest.fixture
def agent_settings() -> AgentSettings:
    return AgentSettings()
