"""The LLM endpoint must not be usable to reach internal services.

These are negative tests: they pass when configuration is *refused*. A green
suite that never attempted a blocked endpoint would prove nothing.

Threat: docs/operations/SECURITY.md section 14.1 (SSRF, High, OWASP A10:2021).

The primary control is that LLM_BASE_URL is operator-only and never influenced
by request data. What is tested here is the defence-in-depth layer behind it.
"""

from __future__ import annotations

import pytest

from core.exceptions import ConfigurationError
from core.settings import LLMProvider, LLMSettings

pytestmark = pytest.mark.security


def _settings(url: str, **kw: object) -> LLMSettings:
    return LLMSettings(
        llm_provider=LLMProvider.OPENAI_COMPATIBLE,
        llm_base_url=url,
        llm_model="some-model",
        llm_api_key="test-key",  # not a real credential
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("http://169.254.169.254/latest/meta-data/", "cloud instance metadata -- IAM creds"),
        ("http://169.254.170.2/v2/credentials", "ECS task metadata"),
        ("https://10.0.0.5/v1", "private range"),
        ("https://192.168.1.10/v1", "private range"),
        ("https://172.16.5.4/v1", "private range"),
        ("https://224.0.0.1/v1", "multicast"),
    ],
)
def test_internal_addresses_are_refused(url: str, why: str) -> None:
    with pytest.raises(ConfigurationError):
        _settings(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://evil.example/",
        "ftp://evil.example/",
        "not-a-url",
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    with pytest.raises(ConfigurationError):
        _settings(url)


def test_plaintext_http_to_a_remote_host_is_refused() -> None:
    """Plaintext would expose LLM_API_KEY in transit."""
    with pytest.raises(ConfigurationError):
        _settings("http://api.groq.com/openai/v1")


def test_host_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(ConfigurationError):
        _settings("https://api.groq.com/openai/v1", llm_allowed_hosts=("api.openai.com",))


def test_loopback_is_permitted_without_a_key() -> None:
    """Local inference is the supported path for sensitive data (SECURITY 14.2)."""
    settings = LLMSettings(
        llm_provider=LLMProvider.OPENAI_COMPATIBLE,
        llm_base_url="http://localhost:11434/v1",
        llm_model="qwen2.5-coder",
    )
    assert settings.llm_api_key is None


def test_remote_endpoint_without_a_key_is_refused() -> None:
    with pytest.raises(ConfigurationError):
        LLMSettings(
            llm_provider=LLMProvider.OPENAI_COMPATIBLE,
            llm_base_url="https://api.groq.com/openai/v1",
            llm_model="some-model",
        )
