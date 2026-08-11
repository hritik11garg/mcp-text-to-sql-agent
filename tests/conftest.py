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
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

from adapters.llm.fake import FakeLLMClient
from core.settings import (
    AgentSettings,
    APISettings,
    BenchmarkSettings,
    DatabaseSettings,
    ExecutionSettings,
    LLMProvider,
    LLMSettings,
    ProfilingSettings,
    RetrievalSettings,
    Settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PG_IMAGE = "pgvector/pgvector:pg16"
RO_PASSWORD = "test-ro-password"  # ephemeral container, never a real credential

type Conn = psycopg.Connection[tuple[object, ...]]

TEST_LAYERS = frozenset({"unit", "integration", "security", "contract", "e2e"})
"""The directories under ``tests/``, which are also the marker names."""


# --- property-based testing ------------------------------------------------

hypothesis_settings.register_profile(
    "dev",
    max_examples=100,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
hypothesis_settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
hypothesis_settings.load_profile("ci" if os.environ.get("CI") else "dev")
"""Two profiles, and the difference between them is the point of having any.

**A property test that runs the same hundred examples everywhere is an
example-based test with extra machinery.** The value is in the examples nobody
would have chosen, and finding those costs time -- so the developer loop stays
at 100 for speed and CI runs 500, where a few extra seconds buys coverage
nothing else in this repository provides.

``deadline=None`` on both, deliberately. Hypothesis' default is a 200 ms
per-example wall clock, and the code under test here parses SQL: on a shared
runner a slow example fails a *timing* assertion while the *property* holds
perfectly. That is a red build for a reason unrelated to correctness, and the
project has no latency budget expressed in this suite -- those live in
``pytest-benchmark`` (docs/operations/PERFORMANCE.md), where a deadline means
something.

``print_blob=True`` so a CI failure prints a reproduction blob. Without it the
failing example is a paragraph of output to retype by hand, and the example
database (``.hypothesis/``) is not shared between a runner and a laptop.
"""


# --- layer markers ---------------------------------------------------------


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply each test's layer marker from the directory it lives in.

    Markers were applied by hand, per module, and **13 files had drifted
    without one** -- including two under ``tests/security/``: the archive
    extraction suite and the DSN-redaction suite for the credential leak in
    SECURITY.md section 14.2.10. Both are release-gate tests.

    That is not a cosmetic gap. `pytest -m security` is what CI is told to run
    as a gate, and an unmarked test is silently deselected, so the gate reports
    green over tests it never ran. The README already warns that *skipped* and
    *passed* look alike; this is the same failure one level up, where the test
    is not even skipped -- it is invisible.

    Deriving the marker from the path rather than re-adding 13 declarations,
    because ``tests/security/`` **is** the security layer. A hand-written
    marker can disagree with the directory; a derived one cannot. Modules that
    already declare ``pytestmark`` keep it -- a duplicate marker is harmless,
    and removing them would make the layer invisible when reading one file.
    """
    for item in items:
        layer = _layer_of(item)
        if layer is None:
            raise pytest.UsageError(
                f"{item.nodeid} is not under any of {sorted(TEST_LAYERS)}. "
                f"Every test belongs to exactly one layer -- a test outside them "
                f"is one no marker-selected run, including the security gate, "
                f"would ever execute."
            )
        item.add_marker(getattr(pytest.mark, layer))


def _layer_of(item: pytest.Item) -> str | None:
    try:
        relative = Path(str(item.fspath)).resolve().relative_to(REPO_ROOT / "tests")
    except ValueError:
        return None
    return next((part for part in relative.parts if part in TEST_LAYERS), None)


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
        "LLM_MODEL_FALLBACKS",
        "RETRIEVAL_TOP_K",
        "HNSW_EF_SEARCH",
        "PROFILE_ALLOW_VALUE_SAMPLING",
        "PROFILE_TOP_K",
        "PROFILE_MIN_VALUE_FREQUENCY",
        "PROFILE_SCAN_LIMIT",
        "PROFILE_MAX_COLUMNS",
        "API_HOST",
        "API_PORT",
        "API_DOCS_ENABLED",
        "API_CORS_ORIGINS",
    ):
        monkeypatch.delenv(var, raising=False)

    for settings_cls in (
        LLMSettings,
        DatabaseSettings,
        ExecutionSettings,
        RetrievalSettings,
        ProfilingSettings,
        AgentSettings,
        APISettings,
        BenchmarkSettings,
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
def profiling_settings() -> ProfilingSettings:
    return ProfilingSettings()


@pytest.fixture
def agent_settings() -> AgentSettings:
    return AgentSettings()


@pytest.fixture
def benchmark_settings() -> BenchmarkSettings:
    return BenchmarkSettings()


def build_settings(**api: Any) -> Settings:
    """A complete :class:`Settings`, for tests that need the composed object.

    ``Settings.load()`` cannot be used: ``_isolate_env`` above clears the
    environment on purpose, and ``LLMSettings`` then refuses to construct
    without a model. The fake provider is the documented way to say "this test
    is not about the LLM", and every other group takes its declared defaults --
    so a test that cares about one setting states that one and nothing else.
    """
    return Settings(
        llm=LLMSettings(llm_provider=LLMProvider.FAKE),
        database=DatabaseSettings(),
        execution=ExecutionSettings(),
        retrieval=RetrievalSettings(),
        profiling=ProfilingSettings(),
        agent=AgentSettings(),
        api=APISettings(**api),
        benchmark=BenchmarkSettings(),
    )


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
def make_sqlite_db(tmp_path: Path) -> Callable[..., Path]:
    """Build a benchmark-shaped SQLite database: ``<root>/<db_id>/<db_id>.sqlite``.

    The layout matters as much as the contents -- ``find_databases`` matches on
    the directory name, and a fixture that wrote a bare file would let that
    logic go untested while every test still passed.
    """

    def build(db_id: str, script: str, *, root: Path | None = None) -> Path:
        folder = (root or tmp_path) / db_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{db_id}.sqlite"
        connection = sqlite3.connect(path)
        try:
            connection.executescript(script)
            connection.commit()
        finally:
            connection.close()
        return path

    return build


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


def require_docker() -> None:
    """Skip without Docker locally; **fail** without it in CI.

    That asymmetry is the whole point. On a developer's machine skipping is
    right: the unit layer still runs and nobody is blocked by a stopped daemon.
    In CI it is the worst available outcome -- the integration and security
    layers evaporate, every remaining test passes, and the run reports green
    over the release gate it exists to enforce.

    This project has already written down twice that *skipped* and *passed*
    look alike, and that an invisible test is worse than a failing one. A
    pipeline that silently dropped the read-only negative suite because a
    container did not start would be the third instance and the most expensive:
    that layer is what proves an LLM cannot write to the database.

    A module-level function rather than inline in the fixture so the guard
    itself is testable -- see ``tests/unit/test_ci_guard.py``. A safety
    mechanism with no test is the shape this repository keeps finding.

    Raises:
        pytest.UsageError: Docker is unavailable and ``CI`` is set.
    """
    if _docker_available():
        return
    if os.environ.get("CI"):
        raise pytest.UsageError(
            "Docker is unavailable, but CI is set. Refusing to skip: the "
            "integration and security layers would vanish and the run would "
            "report green over the release gate. Fix the runner, not this check."
        )
    pytest.skip("Docker daemon not available -- these tests need a real PostgreSQL")


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Start Postgres+pgvector and apply every migration.

    Not SQLite and not a mock. The security model *is* Postgres role
    enforcement, so testing it against anything else tests nothing.
    """
    require_docker()

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
def readonly_connection(postgres_url: str) -> Iterator[Conn]:
    """Connection as the ``SELECT``-only login role.

    The role the agent actually runs as, so it is the right one for anything
    asserting what the agent can *reach* -- catalog introspection especially,
    since a table indexed as the owner and unreadable by this role produces
    retrieval hits whose SQL is always refused.

    ``_ro_libpq`` predates this fixture; two suites had been rebuilding the
    same DSN inline before it existed.
    """
    with psycopg.connect(_ro_libpq(postgres_url), autocommit=True) as conn:
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
