"""The single-turn answering path, shared by the eval harness and the API.

**Why this module exists.** A benchmark number describes the system the
benchmark ran. If the HTTP API composes retrieval and generation even slightly
differently from the eval harness, the measured accuracy describes a system
nobody uses, and nothing in either test suite would notice. So the order of
operations lives here, once, and both callers depend on it.

**What is deliberately *not* shared: execution.** The two callers run the
generated SQL differently and must:

- The API runs it through :class:`~execution.executor.SQLExecutor`, which
  injects a row limit into the AST, sets a statement timeout, and audits.
- The eval runs it through its own runner with **no row limit**, because
  injecting one into a gold query and a predicted query cuts two unordered
  result sets in different places and reports a correct answer as a mismatch
  (ADR-032). It refuses an oversized result rather than truncating it.

That difference is a deliberate decision with a measured failure behind it, so
this module stops at the candidate SQL and neither caller can drift into the
other's execution policy by accident.

**This module raises.** The eval's answerer returns an ``Attempt`` instead of
raising, because for a benchmark a failure to produce SQL is a *result* that
belongs in the taxonomy. The API needs the opposite: a retrieval outage and an
unanswerable question are 503 and 422, and collapsing them into one value loses
the distinction. Exceptions carry that distinction, so the shared piece raises
and the eval flattens -- an exception converts to an ``Attempt`` cleanly, while
an ``Attempt`` cannot be recovered back into a status code.
"""

from __future__ import annotations

from answering.answerer import (
    Candidate,
    ContextSource,
    QuestionAnswerer,
    retrieved_columns,
)

__all__ = [
    "Candidate",
    "ContextSource",
    "QuestionAnswerer",
    "retrieved_columns",
]
