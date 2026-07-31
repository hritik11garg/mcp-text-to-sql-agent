"""Nothing that leaves for a third-party model may carry row data.

The prompt is the only place in this system where data crosses an external
network boundary. Everything else -- the catalog, the audit trail, the logs --
stays in a database the operator controls. So the question "what can leak?" is
almost entirely the question "what is in the prompt?".

These tests enumerate what is allowed there and assert that everything else is
absent, using values that would be unmistakable if they appeared.
"""

from __future__ import annotations

import pytest

from generation.prompts import build_messages, render_context
from schema.models import ForeignKey
from schema.retrieval import RetrievalResult, RetrievedElement

pytestmark = pytest.mark.security

SECRETS = (
    "123-45-6789",
    "4111 1111 1111 1111",
    "ada@example.com",
    "hunter2",
)


def element(
    *,
    table: str = "customers",
    column: str = "notes",
    data_type: str = "text",
    comment: str | None = None,
    serialized: str = "",
) -> RetrievedElement:
    return RetrievedElement(
        element_type="column",
        table=table,
        column=column,
        data_type=data_type,
        comment=comment,
        serialized=serialized,
        score=0.9,
    )


@pytest.fixture
def sampled_context() -> RetrievalResult:
    """A catalog element as it would look with SCHEMA_SAMPLE_VALUES=true.

    ``serialized`` is the text that was embedded, and with sampling on it
    carries real values read out of the table.
    """
    return RetrievalResult(
        elements=(
            element(
                comment="Free-text account notes",
                serialized=(
                    "customers.notes (text) -- Free-text account notes. "
                    f"Examples: SSN {SECRETS[0]}, card {SECRETS[1]}, {SECRETS[2]}"
                ),
            ),
        ),
        foreign_keys=(ForeignKey("orders", "customer_id", "customers", "id"),),
    )


class TestSampledValuesNeverReachTheModel:
    @pytest.mark.parametrize("secret", SECRETS[:3])
    def test_a_sampled_value_is_absent_from_the_prompt(
        self, sampled_context: RetrievalResult, secret: str
    ) -> None:
        """Sampling may improve retrieval. It must not put row data in a prompt.

        SECURITY.md section 14.2.1 treats the catalog as an exfiltration path
        because sampled values are *persisted*. This is the control that stops
        the persisted copy from also being *transmitted*.
        """
        prompt = "\n".join(m.content for m in build_messages("who?", sampled_context))

        assert secret not in prompt

    def test_the_serialized_field_is_not_rendered_at_all(
        self, sampled_context: RetrievalResult
    ) -> None:
        """Stronger than checking for known secrets: the whole pre-rendered
        string is excluded, so a value this test never thought of is excluded
        with it."""
        rendered = render_context(sampled_context)

        assert "Examples:" not in rendered
        assert sampled_context.elements[0].serialized not in rendered

    def test_what_the_model_does_receive_is_exactly_this(
        self, sampled_context: RetrievalResult
    ) -> None:
        """The allowlist, written out. If this assertion has to change, the set
        of things crossing the network boundary has changed with it."""
        rendered = render_context(sampled_context)

        assert rendered.splitlines() == [
            "TABLE customers",
            "  notes (text)  -- Free-text account notes",
            "",
            "RELATIONSHIPS",
            "  orders.customer_id -> customers.id",
        ]


class TestWhatIsDeliberatelyAllowed:
    """Named so the trade-offs are visible rather than implied."""

    def test_column_comments_do_reach_the_model(self) -> None:
        """Deliberate, and a real consideration.

        Comments are what let the model write `country = 'FI'` rather than
        `'Finland'`. They are schema metadata written by humans, not row data
        -- but nothing sanitises them, so a comment containing personal data
        would be transmitted. That belongs in the operator's schema review,
        and it is recorded in SECURITY.md rather than silently assumed safe.
        """
        context = RetrievalResult(
            elements=(element(column="country", comment="ISO 3166-1 alpha-2 country code"),)
        )

        assert "ISO 3166-1" in render_context(context)

    def test_table_and_column_names_do_reach_the_model(self) -> None:
        """Unavoidable -- the model cannot write SQL against names it has not
        seen. Worth stating because names can themselves be disclosive."""
        context = RetrievalResult(elements=(element(table="hiv_status", column="result"),))

        assert "hiv_status" in render_context(context)

    def test_the_question_reaches_the_model_and_is_bounded(self) -> None:
        """Also unavoidable, and bounded at 2,000 characters."""
        messages = build_messages("x" * 5_000, RetrievalResult())

        assert "[truncated]" in messages[1].content
