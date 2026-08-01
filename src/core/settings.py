"""Configuration, validated at startup.

Every setting is typed and range-checked here, so invalid configuration fails
before the service accepts traffic rather than on the first request that needs
the bad value. See docs/operations/CONFIG.md.

Settings groups are separate classes composed into :class:`Settings` -- each
group is cohesive, independently testable, and injectable on its own.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlparse

from pydantic import BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from core.exceptions import ConfigurationError

_ENV_FILE = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


def _split_csv(value: object) -> object:
    """Accept ``a,b,c`` where pydantic-settings would demand ``["a","b","c"]``."""
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return value


CsvTuple = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_split_csv)]
"""A list-valued setting written the way a .env file is actually written.

``NoDecode`` is the load-bearing part. Without it pydantic-settings tries to
JSON-decode any complex-typed field *at the source*, before validators run, so
``LLM_MODEL_FALLBACKS=a,b`` fails with a parse error rather than reaching any
code that could interpret it. Requiring JSON inside a .env file is a trap:
CONFIG.md documents these as plain lists, every example writes them
comma-separated, and the error message names neither the format nor the field's
purpose.
"""


class LLMProvider(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"
    FAKE = "fake"


class LLMSettings(BaseSettings):
    """Provider selection and endpoint configuration.

    Security: ``llm_base_url`` is operator-only configuration. It is read once,
    here, from the environment. It must never be populated from a request, a
    header, a session, or the database -- that would turn this service into an
    SSRF primitive with the API key attached. The validation below is defence
    in depth against operator error, not the primary control.

    See docs/operations/SECURITY.md section 14.1.
    """

    model_config = _ENV_FILE

    llm_provider: LLMProvider = LLMProvider.OPENAI_COMPATIBLE
    llm_base_url: str | None = None
    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    llm_max_tokens: int = Field(default=16_000, ge=1, le=200_000)
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_timeout_ms: int = Field(default=60_000, ge=1_000, le=600_000)

    llm_allowed_hosts: CsvTuple = ()
    """Optional host allowlist. Empty means "any host passing the IP checks"."""

    llm_model_fallbacks: CsvTuple = ()
    """Models to try, in order, when ``LLM_MODEL`` is out of quota.

    Free tiers cap tokens **per model per day**, so exhausting one is routine
    rather than exceptional. Listing alternatives here turns a spent cap from a
    failed benchmark run into a logged switch.

    Same provider and same key -- this is a model chain, not a provider chain.
    Switching provider is still a ``LLM_BASE_URL`` change, deliberately: a
    second endpoint means a second credential and a second SSRF check, which is
    configuration a running process should not be improvising.
    """

    @model_validator(mode="after")
    def _check_provider_requirements(self) -> LLMSettings:
        if self.llm_provider is LLMProvider.FAKE:
            return self

        if not self.llm_model:
            raise ConfigurationError("LLM_MODEL is required unless LLM_PROVIDER=fake")

        if self.llm_provider is LLMProvider.OPENAI_COMPATIBLE:
            if not self.llm_base_url:
                raise ConfigurationError(
                    "LLM_BASE_URL is required when LLM_PROVIDER=openai_compatible"
                )
            _assert_endpoint_is_safe(self.llm_base_url, self.llm_allowed_hosts)
            if not _is_loopback_url(self.llm_base_url) and self.llm_api_key is None:
                raise ConfigurationError("LLM_API_KEY is required for non-local endpoints")

        elif self.llm_provider is LLMProvider.ANTHROPIC and self.llm_api_key is None:
            raise ConfigurationError("LLM_API_KEY is required when LLM_PROVIDER=anthropic")

        return self


class DatabaseSettings(BaseSettings):
    """Two roles, deliberately.

    ``database_ro_url`` must resolve to a SELECT-only role. If it does not,
    every containment guarantee in docs/operations/SECURITY.md is void. The
    startup check that proves it lives in the database layer, since it requires
    a live connection -- this class only enforces that the two URLs differ.
    """

    model_config = _ENV_FILE

    database_url: SecretStr | None = None
    database_ro_url: SecretStr | None = None
    db_pool_min_size: int = Field(default=2, ge=1, le=100)
    db_pool_max_size: int = Field(default=10, ge=1, le=100)
    db_connect_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)
    db_target_schema: str = "public"

    @model_validator(mode="after")
    def _check_pool_and_roles(self) -> DatabaseSettings:
        if self.db_pool_max_size < self.db_pool_min_size:
            raise ConfigurationError("DB_POOL_MAX_SIZE must be >= DB_POOL_MIN_SIZE")

        owner, readonly = self.database_url, self.database_ro_url
        if (
            owner is not None
            and readonly is not None
            and owner.get_secret_value() == readonly.get_secret_value()
        ):
            raise ConfigurationError(
                "DATABASE_RO_URL must differ from DATABASE_URL -- the read-only "
                "role is the outermost containment boundary and cannot be the "
                "same role that owns the schema"
            )
        return self


class ExecutionSettings(BaseSettings):
    """Limits on what a single query may consume.

    Every limit has a server-side ceiling. A caller may request something
    smaller; it may never request something larger.
    """

    model_config = _ENV_FILE

    max_rows_default: int = Field(default=500, ge=1)
    max_rows_ceiling: int = Field(default=5_000, ge=1)
    statement_timeout_ms: int = Field(default=30_000, ge=100)
    statement_timeout_ceiling_ms: int = Field(default=60_000, ge=100)
    max_estimated_cost: float = Field(default=1_000_000.0, gt=0)

    @model_validator(mode="after")
    def _check_ceilings(self) -> ExecutionSettings:
        if self.max_rows_default > self.max_rows_ceiling:
            raise ConfigurationError("MAX_ROWS_DEFAULT must be <= MAX_ROWS_CEILING")
        if self.statement_timeout_ms > self.statement_timeout_ceiling_ms:
            raise ConfigurationError("STATEMENT_TIMEOUT_MS must be <= STATEMENT_TIMEOUT_CEILING_MS")
        return self

    def clamp_rows(self, requested: int | None) -> int:
        """Clamp a caller-supplied row limit. Never trust the caller."""
        if requested is None:
            return self.max_rows_default
        return max(1, min(requested, self.max_rows_ceiling))

    def clamp_timeout_ms(self, requested: int | None) -> int:
        if requested is None:
            return self.statement_timeout_ms
        return max(100, min(requested, self.statement_timeout_ceiling_ms))


class EmbedderProvider(StrEnum):
    SENTENCE_TRANSFORMER = "sentence_transformer"
    HASHING = "hashing"


class RetrievalSettings(BaseSettings):
    """Schema catalog and retrieval configuration.

    Security: ``schema_sample_values`` defaults to ``False`` because sampling
    copies real rows out of the target tables and **persists** them in
    ``agent_meta.schema_elements``, where they stay until re-indexed. Prompt
    construction reads the structured fields and never the serialized string,
    so those values do not themselves leave the network (section 14.2.5) --
    but a store of real rows that no audit trail mentions is worth an explicit
    decision rather than a default.

    See docs/operations/SECURITY.md section 14.2.
    """

    model_config = _ENV_FILE

    dataset: str = "default"
    """Namespace for catalog rows, so several schemas can share one database."""

    embedder_provider: EmbedderProvider = EmbedderProvider.SENTENCE_TRANSFORMER
    retriever_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    """The single source of truth for which model produces vectors.

    384 dimensions, matching the catalog's vector column; small enough to run
    on CPU in the demo path. A model of a different width is a migration, not
    a config change. The value is also recorded as ``model_version`` on every
    row, so pointing this at a fine-tuned checkpoint keeps the two vector
    spaces separable.
    """

    retriever_local_files_only: bool = False
    """Refuse to download the model. Useful in CI and air-gapped deployments,
    where an implicit network fetch should fail loudly rather than succeed."""

    schema_sample_values: bool = False
    schema_sample_count: int = Field(default=3, ge=0, le=10)
    schema_sample_max_chars: int = Field(default=40, ge=1, le=200)
    schema_sample_scan_limit: int = Field(default=1_000, ge=1, le=100_000)
    """Rows scanned per column when sampling. Bounds the cost of indexing a
    large table; without it, indexing would seq-scan every table in the
    schema once per column."""

    schema_extra_sensitive_columns: CsvTuple = ()
    """Additional column-name substrings that must never be sampled.

    Added to the built-in list rather than replacing it, so a deployment can
    only ever tighten the rule by configuration.
    """

    retrieval_top_k: int = Field(default=10, ge=1, le=50)
    """Elements returned per search when a caller does not ask for a count.

    The upper bound here mirrors the hard ceiling in ``schema.retrieval``,
    which is the one that actually enforces it and which matches the published
    ``schema_search`` tool schema. Duplicated rather than imported because the
    core layer does not depend on the schema layer -- and a caller is clamped
    at the retriever regardless, so this bound is the outer of two.
    """

    hnsw_ef_search: int = Field(default=40, ge=1, le=1_000)
    """HNSW search breadth: higher is more accurate and slower.

    The recall/latency knob swept in Stage 6. It is configuration rather than a
    constant so that sweep does not require a code change.
    """


class ProfilingSettings(BaseSettings):
    """Bounds on what a table profile may read and what it may reveal.

    Profiling is the one component whose *output is row data by design*. The
    catalog indexer samples values to improve retrieval and never shows them to
    a model; a profile exists to be shown to a model, so every default here is
    set for the case where the target database holds real customer records and
    the operator has not read this file.

    Three independent gates, deliberately not one:

    1. ``profile_allow_value_sampling`` (off) governs raw rows.
    2. ``profile_min_value_frequency`` governs frequent values -- a value seen
       fewer than this many times identifies a record rather than a category,
       and is withheld even when sampling is on.
    3. The sensitive-column denylist governs which columns are read at all.

    See docs/operations/SECURITY.md section 14.2.6.
    """

    model_config = _ENV_FILE

    profile_scan_limit: int = Field(default=5_000, ge=1, le=1_000_000)
    """Rows read per column. Bounds cost, and bounds disclosure with it.

    The scan is ``LIMIT``ed without ``ORDER BY``, so it reads the first rows in
    physical order rather than a random sample. That is a deliberate accuracy
    trade -- see :class:`profiling.profiler.TableProfile`.
    """

    profile_max_columns: int = Field(default=30, ge=1, le=200)
    """Columns profiled per call. A wide table is truncated, not refused.

    Unbounded, one profile of a 300-column table would consume the agent's
    entire context budget in a single tool result.
    """

    profile_max_value_chars: int = Field(default=40, ge=1, le=200)
    profile_top_k: int = Field(default=5, ge=0, le=20)
    """Frequent values returned per eligible column. ``0`` disables them."""

    profile_min_value_frequency: int = Field(default=5, ge=2, le=100_000)
    """How often a value must occur before it may be reported.

    A small-cell threshold, the standard control in statistical disclosure:
    a value occurring once is a record, a value occurring many times is a
    category. ``ge=2`` is a floor in the type -- this setting can be relaxed,
    but never to the point where a unique value becomes reportable.
    """

    profile_allow_value_sampling: bool = False
    """Whether raw sampled rows may be returned at all. Off is the safe default.

    Statistics answer "is this column the one I want?". Raw rows answer "what
    do the values look like?", which is occasionally necessary and always a
    disclosure.
    """

    profile_sample_rows: int = Field(default=5, ge=0, le=20)
    """Rows returned when sampling is allowed. Ignored when it is not."""

    profile_timeout_ms: int = Field(default=10_000, ge=100, le=120_000)
    """Its own budget rather than the executor's: profiling is a side quest
    during answering, and it should give up long before the real query would."""

    def clamp_sample_rows(self, requested: int | None) -> int:
        """Clamp a caller-supplied sample size. Zero unless explicitly allowed.

        The caller here is a language model reading user-supplied text, so
        ``sample_rows: 10000`` is an input to bound, not a request to honour.
        """
        if not self.profile_allow_value_sampling:
            return 0
        if requested is None:
            return self.profile_sample_rows
        return max(0, min(requested, self.profile_sample_rows))

    def clamp_columns(self, requested: int | None) -> int:
        if requested is None:
            return self.profile_max_columns
        return max(1, min(requested, self.profile_max_columns))


class AgentSettings(BaseSettings):
    """Bounds on the agent loop.

    ``max_tool_calls_per_request`` is not optional. A self-correcting agent
    that fails to converge retries until something stops it, and that something
    must be a counter rather than a hope.
    """

    model_config = _ENV_FILE

    max_tool_calls_per_request: int = Field(default=20, ge=1, le=200)
    max_sql_retries: int = Field(default=3, ge=0, le=20)
    max_decomposition_steps: int = Field(default=5, ge=1, le=20)
    agent_timeout_ms: int = Field(default=120_000, ge=1_000)


@dataclass(frozen=True, slots=True)
class Settings:
    """Composed application settings, constructed once at startup and injected.

    Never read from the environment mid-module -- pass this object down.
    """

    llm: LLMSettings
    database: DatabaseSettings
    execution: ExecutionSettings
    retrieval: RetrievalSettings
    profiling: ProfilingSettings
    agent: AgentSettings

    @classmethod
    def load(cls) -> Settings:
        """Build from the environment, failing fast on anything invalid."""
        return cls(
            llm=LLMSettings(),
            database=DatabaseSettings(),
            execution=ExecutionSettings(),
            retrieval=RetrievalSettings(),
            profiling=ProfilingSettings(),
            agent=AgentSettings(),
        )


# --- endpoint safety -------------------------------------------------------

_BLOCKED_REASON = "LLM_BASE_URL resolves to a blocked address ({addr}): {why}"


def _is_loopback_url(url: str) -> bool:
    host = urlparse(url).hostname
    if host is None:
        return False
    try:
        return all(
            ipaddress.ip_address(info[4][0]).is_loopback for info in socket.getaddrinfo(host, None)
        )
    except (socket.gaierror, ValueError):
        return False


def _assert_endpoint_is_safe(url: str, allowed_hosts: tuple[str, ...]) -> None:
    """Reject endpoints that could be used to reach internal services.

    Defence in depth behind the primary control, which is that this value is
    operator-only and never influenced by request data.
    """
    parsed = urlparse(url)
    host = parsed.hostname

    if parsed.scheme not in {"http", "https"} or not host:
        raise ConfigurationError(f"LLM_BASE_URL must be an http(s) URL, got {url!r}")

    if allowed_hosts and host not in allowed_hosts:
        raise ConfigurationError(f"LLM_BASE_URL host {host!r} is not in LLM_ALLOWED_HOSTS")

    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        raise ConfigurationError(f"LLM_BASE_URL host {host!r} does not resolve") from exc

    for addr in resolved:
        ip = ipaddress.ip_address(addr)

        if ip.is_loopback:
            # Local inference (Ollama, LM Studio) is a documented, supported
            # configuration, and it is the recommended path for sensitive data.
            continue

        if ip.is_link_local:
            # 169.254.169.254 is the cloud instance metadata endpoint. Reaching
            # it would disclose IAM credentials.
            raise ConfigurationError(_BLOCKED_REASON.format(addr=addr, why="link-local"))
        if ip.is_private:
            raise ConfigurationError(_BLOCKED_REASON.format(addr=addr, why="private range"))
        if ip.is_reserved or ip.is_multicast:
            raise ConfigurationError(_BLOCKED_REASON.format(addr=addr, why="reserved"))

        if parsed.scheme != "https":
            raise ConfigurationError(
                "LLM_BASE_URL must use https for non-local endpoints "
                "(plaintext would expose LLM_API_KEY in transit)"
            )
