"""Domain exception hierarchy.

One base class per concern, so callers can catch narrowly at the layer that can
actually act on the failure. See docs/development/CODE_STYLE.md section 8.

Note the deliberate omission: there is no exception type for an MCP *tool*
failure. A tool that fails returns structured content with ``isError: true`` so
the agent can read the failure and correct itself. Raising would give the agent
nothing to work with. See docs/architecture/MCP.md section 6.
"""

from __future__ import annotations


class TextToSQLError(Exception):
    """Base for every error this project raises deliberately."""


# --- configuration ---------------------------------------------------------


class ConfigurationError(TextToSQLError):
    """Invalid configuration, detected at startup.

    Always fatal. Configuration is validated before the service accepts
    traffic, never on the first request that happens to need the bad value.
    """


# --- LLM -------------------------------------------------------------------


class LLMError(TextToSQLError):
    """Base for failures talking to a language model."""


class LLMUnavailableError(LLMError):
    """The provider could not be reached, or failed after retries."""


class LLMQuotaExceededError(LLMUnavailableError):
    """Rate limited, or out of quota, on this model.

    Distinct from its parent because the recovery differs: the request is not
    wrong and the provider is not down, so the same call against a *different*
    model will usually succeed. Free tiers enforce per-model daily token caps,
    so this is an ordinary operating condition rather than an incident.
    """


class LLMResponseError(LLMError):
    """The provider responded, but the response could not be used."""


# --- retrieval -------------------------------------------------------------


class RetrievalError(TextToSQLError):
    """Base for schema retrieval failures."""


class EmbeddingModelMismatchError(RetrievalError):
    """Configured retriever version has no vectors in the catalog.

    Raised at startup rather than at query time: a mismatch silently degrades
    retrieval instead of erroring, because query and corpus embeddings land in
    different vector spaces and still return plausible-looking results.
    """


# --- SQL validation --------------------------------------------------------


class SQLValidationError(TextToSQLError):
    """Generated SQL failed a validation stage.

    Carries ``error_type`` because the correct recovery differs per stage --
    a syntax error is fixed by rewriting, an unknown identifier by
    re-retrieving. See docs/ml/PROMPTS.md section 3.3.
    """

    def __init__(self, error_type: str, message: str, identifier: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.identifier = identifier


# --- execution -------------------------------------------------------------


class ExecutionError(TextToSQLError):
    """Base for failures running a validated query."""


class StatementTimeoutError(ExecutionError):
    """The query exceeded its statement timeout.

    Distinct from SQLValidationError on purpose: this is a resource signal, not
    a correctness signal. Retrying the same query unchanged will time out again.
    """


class PermissionDeniedError(ExecutionError):
    """The read-only role refused the statement.

    Usually correct behaviour rather than a bug -- the role is meant to be
    unable to reach some things. Never resolve this by widening grants.
    """


# --- profiling -------------------------------------------------------------


class ProfilingError(TextToSQLError):
    """Base for failures profiling a table."""


class UnknownTableError(ProfilingError):
    """The requested table is not in the catalog.

    Raised *before* any SQL is composed, which makes it a containment control
    and not only a usability one: the catalog is the allowlist that decides
    which identifiers may reach a composed statement at all. Carries a
    suggestion for the same reason ``SQLValidationError`` does -- an agent told
    only "no" retries with the same name.
    """

    def __init__(self, table: str, suggestion: str | None = None) -> None:
        hint = f"; did you mean {suggestion!r}?" if suggestion else ""
        super().__init__(f"table {table!r} is not in the catalog{hint}")
        self.table = table
        self.suggestion = suggestion


# --- agent -----------------------------------------------------------------


class AgentError(TextToSQLError):
    """Base for agent orchestration failures."""


class BudgetExhaustedError(AgentError):
    """A bounded resource ran out before the agent converged."""

    def __init__(self, budget: str, limit: int) -> None:
        super().__init__(f"{budget} budget exhausted after {limit}")
        self.budget = budget
        self.limit = limit
