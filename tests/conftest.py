"""Shared fixtures.

Fixtures build data; tests assert on it. A fixture that asserts is doing the
test's job. See docs/development/CODE_STYLE.md section 11.

The PostgreSQL fixtures live here rather than under tests/integration/ because
the security suite needs them too, and a conftest only applies to its own
directory and below. Nothing starts a container until a test asks for one, so
the unit suite stays fast.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from adapters.llm.fake import FakeLLMClient
from core.settings import (
    AgentSettings,
    DatabaseSettings,
    ExecutionSettings,
    LLMSettings,
    RetrievalSettings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PG_IMAGE = "pgvector/pgvector:pg16"
RO_PASSWORD = "test-ro-password"  # ephemeral container, never a real credential

type Conn = psycopg.Connection[tuple[object, ...]]


# --- environment isolation -------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the developer's real configuration from leaking into tests.

    Two independent sources have to be closed, and missing the second is easy:

    1. Environment variables -- cleared below.
    2. **The .env file** -- pydantic-settings reads it directly, bypassing
       os.environ entirely, so deleting variables does nothing about it.

    Only closing (1) produces tests that pass on a machine with no .env and
    fail on one that has it -- which is exactly what happened the first time a
    real .env was created here.

    No teardown needed: monkeypatch restores both itself.
    """
    for var in (
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_ALLOWED_HOSTS",
        "DATABASE_URL",
        "DATABASE_RO_URL",
        "DATASET",
        "EMBEDDER_PROVIDER",
        "RETRIEVER_MODEL",
        "SCHEMA_SAMPLE_VALUES",
    ):
        monkeypatch.delenv(var, raising=False)

    for settings_cls in (
        LLMSettings,
        DatabaseSettings,
        ExecutionSettings,
        RetrievalSettings,
        AgentSettings,
    ):
        monkeypatch.setitem(settings_cls.model_config, "env_file", None)


# --- pure fixtures ---------------------------------------------------------


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def execution_settings() -> ExecutionSettings:
    return ExecutionSettings()


@pytest.fixture
def retrieval_settings() -> RetrievalSettings:
    return RetrievalSettings()


@pytest.fixture
def agent_settings() -> AgentSettings:
    return AgentSettings()


# --- PostgreSQL ------------------------------------------------------------


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Start Postgres+pgvector and apply every migration.

    Not SQLite and not a mock. The security model *is* Postgres role
    enforcement, so testing it against anything else tests nothing.
    """
    if not _docker_available():
        pytest.skip("Docker daemon not available -- these tests need a real PostgreSQL")

    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(PG_IMAGE, driver=None) as container:
        url = container.get_connection_url()
        _run_migrations(url)
        yield url


def _run_migrations(url: str) -> None:
    """Apply migrations exactly as production does -- via alembic.

    A test that sets up its own schema proves the test's SQL is right, not the
    migration's.
    """
    env = {**os.environ, "DATABASE_URL": url, "SQL_AGENT_RO_PASSWORD": RO_PASSWORD}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")


def _libpq(url: str) -> str:
    """testcontainers returns a SQLAlchemy URL; psycopg wants a libpq one."""
    return url.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://"
    )


def _ro_libpq(url: str) -> str:
    parts = urlsplit(_libpq(url))
    netloc = f"sql_agent_login:{RO_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@pytest.fixture(scope="session")
def owner_connection(postgres_url: str) -> Iterator[Conn]:
    """Connection as the owner role -- builds fixtures, never the subject of a test."""
    with psycopg.connect(_libpq(postgres_url), autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def target_table(owner_connection: Conn) -> None:
    """A table in the target schema, so denial tests have something to aim at.

    Without it, an INSERT would fail with "relation does not exist" and the
    test would pass for entirely the wrong reason.
    """
    owner_connection.execute("""
        CREATE TABLE IF NOT EXISTS public.orders (
            id           bigserial PRIMARY KEY,
            total_amount numeric(12, 2) NOT NULL DEFAULT 0
        )
    """)
    owner_connection.execute("GRANT SELECT ON public.orders TO sql_agent_ro")


@pytest.fixture(scope="session")
def catalog_schema(owner_connection: Conn, target_table: None) -> None:
    """A small schema with the features introspection has to handle.

    Comments, a foreign key, a sensitive column name, and one table the
    read-only role deliberately cannot SELECT -- so "only index what the agent
    can read" is asserted against a real denial rather than assumed.
    """
    owner_connection.execute("""
        CREATE TABLE IF NOT EXISTS public.customers (
            id      bigserial PRIMARY KEY,
            name    text NOT NULL,
            email   text,
            country text
        )
    """)
    owner_connection.execute("""
        ALTER TABLE public.orders
            ADD COLUMN IF NOT EXISTS customer_id bigint REFERENCES public.customers(id)
    """)

    owner_connection.execute("COMMENT ON TABLE public.customers IS 'One row per customer account'")
    owner_connection.execute(
        "COMMENT ON COLUMN public.customers.country IS 'ISO 3166-1 alpha-2 country code'"
    )
    owner_connection.execute(
        "COMMENT ON COLUMN public.orders.total_amount IS 'Order total including tax, in USD'"
    )

    owner_connection.execute("""
        CREATE TABLE IF NOT EXISTS public.internal_payroll (
            id     bigserial PRIMARY KEY,
            amount numeric(12, 2) NOT NULL
        )
    """)

    owner_connection.execute("GRANT SELECT ON public.customers TO sql_agent_ro")
    owner_connection.execute("REVOKE ALL ON public.internal_payroll FROM sql_agent_ro")

    for values in (("Ada", "ada@example.com", "GB"), ("Linus", "linus@example.com", "FI")):
        owner_connection.execute(
            "INSERT INTO public.customers (name, email, country) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            values,
        )


@pytest.fixture
def ro_connection(postgres_url: str, target_table: None) -> Iterator[Conn]:
    """Connection as the read-only login role. The subject of every denial test."""
    conn = psycopg.connect(_ro_libpq(postgres_url), autocommit=True)
    try:
        yield conn
    finally:
        conn.close()
