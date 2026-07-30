"""Which columns must never have their values sampled.

Sampling copies real rows into ``schema_elements.serialized``, and that text is
later placed in prompts sent to a third-party model. For a column holding
personal or secret data that is a disclosure, and it is permanent: the values
sit in the catalog until re-indexed, and leave the building on every query that
retrieves the element.

This is a *name-based heuristic*, so it is a mitigation and never a guarantee.
A column called ``notes`` can hold anything. The actual control is that
sampling is off by default (``SCHEMA_SAMPLE_VALUES``); this list reduces the
damage when an operator turns it on without auditing every column first.

See docs/operations/SECURITY.md section 14.2 (OWASP LLM06).
"""

from __future__ import annotations

from collections.abc import Iterable

DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "session_id",
    "ssn",
    "social_security",
    "national_id",
    "tax_id",
    "passport",
    "license_no",
    "licence_no",
    "driver_license",
    "credit_card",
    "card_number",
    "cardno",
    "cvv",
    "iban",
    "swift",
    "account_number",
    "routing_number",
    "email",
    "phone",
    "mobile",
    "address",
    "postcode",
    "zipcode",
    "zip_code",
    "latitude",
    "longitude",
    "dob",
    "birth",
    "salary",
    "compensation",
    "income",
    "diagnosis",
    "medical",
    "biometric",
)
"""Substrings that mark a column as unsafe to sample.

Broad on purpose. A false positive costs a few example values in one
serialized string; a false negative sends real personal data to a third party.
"""


def is_sensitive(column_name: str, patterns: Iterable[str] = DEFAULT_SENSITIVE_PATTERNS) -> bool:
    """Whether ``column_name`` looks like it holds data that must not be sampled.

    Case-insensitive substring match, so ``customerEmailAddress``, ``EMAIL``
    and ``email_2`` all match ``email``.
    """
    lowered = column_name.casefold()
    return any(pattern in lowered for pattern in patterns)
