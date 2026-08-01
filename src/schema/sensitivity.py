"""Which columns must never have their values read.

Two callers, and they carry different risk, so it is worth being precise about
which is which:

**The indexer** copies sampled rows into ``schema_elements.serialized``. That
text is embedded and stored until the next re-index. It is *not* placed in
prompts -- ``generation.prompts`` renders the structured fields and never the
serialized string (SECURITY.md section 14.2.5) -- so this path is persistence,
not transmission.

**The profiler** is the path that transmits. Frequent values, extremes and
sampled rows exist precisely to be shown to a model so it can write a correct
``WHERE`` clause, so anything this list fails to catch there leaves the
building.

This is a *name-based heuristic*, so it is a mitigation and never a guarantee.
A column called ``notes`` can hold anything. The real controls are that both
callers are off by default (``SCHEMA_SAMPLE_VALUES``,
``PROFILE_ALLOW_VALUE_SAMPLING``) and that the profiler additionally withholds
any value too rare to be a category label; this list reduces the damage when an
operator turns one of them on without auditing every column first.

See docs/operations/SECURITY.md sections 14.2 and 14.2.6 (OWASP LLM06).
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
