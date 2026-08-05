"""The published error table, and what a message is allowed to contain."""

from __future__ import annotations

import json

import pytest

from api.errors import (
    AMBIGUOUS_QUESTION,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    LLM_UNAVAILABLE,
    QUERY_TIMEOUT,
    RATE_LIMITED,
    SQL_VALIDATION_FAILED,
    ApiError,
    ErrorCode,
    code_for,
    envelope,
)
from core.exceptions import (
    BudgetExhaustedError,
    ConfigurationError,
    LLMError,
    LLMQuotaExceededError,
    LLMResponseError,
    LLMUnavailableError,
    PermissionDeniedError,
    RetrievalError,
    SQLValidationError,
    StatementTimeoutError,
    TextToSQLError,
)


class TestTheMappingMatchesTheContract:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (StatementTimeoutError("slow"), QUERY_TIMEOUT),
            (PermissionDeniedError("denied"), SQL_VALIDATION_FAILED),
            (LLMQuotaExceededError("spent"), RATE_LIMITED),
            (LLMUnavailableError("down"), LLM_UNAVAILABLE),
            (LLMResponseError("garbled"), LLM_UNAVAILABLE),
            (LLMError("generic"), LLM_UNAVAILABLE),
            (RetrievalError("nothing matched"), AMBIGUOUS_QUESTION),
            (ConfigurationError("bad env"), INTERNAL_ERROR),
            (BudgetExhaustedError("tool_calls", 20), INTERNAL_ERROR),
            (TextToSQLError("unclassified"), INTERNAL_ERROR),
        ],
    )
    def test_each_domain_error_maps_to_its_published_code(
        self, exc: TextToSQLError, expected: ErrorCode
    ) -> None:
        assert code_for(exc) == expected

    def test_the_subclass_wins_over_the_base(self) -> None:
        """`LLMQuotaExceededError` is an `LLMUnavailableError`. Matching the base
        first would report a spent daily cap as an upstream outage, and a client
        would retry immediately instead of backing off."""
        assert code_for(LLMQuotaExceededError("spent")) == RATE_LIMITED
        assert code_for(LLMUnavailableError("down")) == LLM_UNAVAILABLE

    def test_a_validation_error_carries_the_component_message(self) -> None:
        """This one *is* meant for the caller -- it says which identifier was wrong."""
        exc = SQLValidationError("unknown_column", "column 'revenu' does not exist")
        assert code_for(exc) == SQL_VALIDATION_FAILED

    def test_an_unmapped_error_is_internal_rather_than_something_optimistic(self) -> None:
        """A new exception type must not fall through to a 200 or a 400."""

        class UnheardOfError(TextToSQLError):
            pass

        assert code_for(UnheardOfError("new")) == INTERNAL_ERROR


class TestTheEnvelopeShape:
    def test_it_matches_the_documented_body(self) -> None:
        response = envelope(INVALID_REQUEST, "bad question", request_id="req_1")
        body = json.loads(bytes(response.body))
        assert body == {
            "error": {"code": "invalid_request", "message": "bad question", "request_id": "req_1"}
        }
        assert response.status_code == 400

    def test_details_are_included_when_present(self) -> None:
        response = envelope(
            SQL_VALIDATION_FAILED, "no", request_id="req_2", details={"attempts": 3}
        )
        body = json.loads(bytes(response.body))
        assert body["error"]["details"] == {"attempts": 3}

    def test_an_empty_details_is_omitted_rather_than_null(self) -> None:
        """A key that is sometimes absent and sometimes null is two shapes."""
        response = envelope(INTERNAL_ERROR, "no", request_id="req_3", details={})
        assert "details" not in json.loads(bytes(response.body))["error"]

    def test_the_request_id_is_on_the_response_as_well_as_in_it(self) -> None:
        response = envelope(INTERNAL_ERROR, "no", request_id="req_4")
        assert response.headers["X-Request-Id"] == "req_4"


class TestApiError:
    def test_it_carries_a_code_from_the_table(self) -> None:
        """A route cannot invent a `code`, which is how a documented contract
        acquires undocumented members."""
        raised = ApiError(RATE_LIMITED, "too many in flight")
        assert raised.error is RATE_LIMITED
        assert str(raised) == "too many in flight"

    def test_details_default_to_nothing(self) -> None:
        assert ApiError(INVALID_REQUEST, "no").details is None
