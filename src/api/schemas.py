"""The request and response bodies for ``POST /v1/query``.

**Every field here does something.** [API.md](../../docs/architecture/API.md)
specifies a larger request than this one -- ``session_id`` for follow-up
questions -- and it is not built. It is therefore *absent* rather than accepted
and ignored, and ``extra="forbid"`` turns sending it into a ``400`` naming the
field.

``stream`` was in that category until the stream was built, and is now a real
field. That is the rule working in the direction it is supposed to: a field
appears when the behaviour behind it does, so the request shape and the served
behaviour never disagree.

That is the one design decision in this module. A field that parses and does
nothing is the same defect as a config variable nothing reads: the caller sets
it, receives no error, and concludes it took effect. It cost this project a
slice already -- three dead variables in ``.env.example``, one of which read as
a security control -- and the lesson generalises exactly. **A missing feature
produces a gap the caller can see; an accepted-and-ignored field produces
confidence.**

``session_id`` is the sharper of the two. Silently ignoring it means a caller
believes their follow-up question has the previous turn's context when it has
none, and the answer they get back is *plausible* rather than obviously wrong.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.settings import APISettings

DEFAULT_MAX_QUESTION_CHARS = APISettings.model_fields["api_max_question_chars"].default


class QueryOptions(BaseModel):
    """Per-request bounds. Every one may only make the request *smaller*.

    The server's configured ceilings are applied on top of whatever arrives
    here (``execution.SQLExecutor`` clamps, smaller-wins), so these are a way
    for a caller to ask for less and never a way to ask for more. A caller
    sending ``max_rows: 10_000_000`` gets the server's maximum, not an error --
    the request is not malformed, it is simply not the caller's decision.
    """

    model_config = ConfigDict(extra="forbid")

    max_rows: int | None = Field(default=None, ge=1)
    timeout_ms: int | None = Field(default=None, ge=1)
    explain_only: bool = False
    """Generate and validate, but do not execute.

    Useful, and it is the field to be careful with: validation resolves
    identifiers against the catalog, so its *failure* messages describe the
    schema. See ``api.query`` for why the response drops the detail that
    ``validate_sql`` returns to a local operator.
    """


class QueryRequest(BaseModel):
    """A question, and the bounds the caller wants applied to answering it."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=DEFAULT_MAX_QUESTION_CHARS)
    """1 to 2000 characters, per API.md.

    The bound is enforced twice, by two mechanisms that fail differently. Here
    it is a ``422`` describing the field, which is what a caller needs. In
    ``APISettings.api_max_question_chars`` it is the configured value actually
    checked at request time, which is what an operator tunes. This annotation
    carries the *default* of that setting so the generated schema states a
    number rather than nothing -- the runtime check is the authority.
    """

    stream: bool = False
    """Send the answer as server-sent events instead of one JSON body.

    This field was **refused by name** until the stream existed, which is the
    decision this module's docstring is about (ADR-038). It is accepted now for
    the same reason it was refused then: it does something. ``session_id``
    still is not here, because session memory still is not built.

    Default ``false``, so the non-streaming contract is unchanged for every
    caller written against it.
    """

    options: QueryOptions = Field(default_factory=QueryOptions)


class QueryStep(BaseModel):
    """One phase of answering, for the ``steps`` array API.md specifies."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    duration_ms: float
    status: str


class QueryResponse(BaseModel):
    """The non-streaming answer.

    Deliberately not the shape API.md's example shows. That example carries an
    ``answer`` field holding prose -- "Revenue in Q4 2025 was highest in EMEA"
    -- which is a *synthesis* step this system does not have: nothing turns a
    result set back into a sentence yet. Emitting the field as an empty string
    would be the same lie this module's docstring is about, so it is absent
    until Stage 4 builds the thing that fills it.
    """

    model_config = ConfigDict(extra="forbid")

    sql: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    row_count: int = 0
    truncated: bool = False
    """The row limit clipped the result. A client must not present it as complete."""

    executed: bool = True
    """``False`` when ``explain_only`` was set.

    An explicit flag rather than letting the caller infer it from an empty
    ``rows``: a query that legitimately returns no rows and a query that was
    never run are different facts, and they are indistinguishable by shape.
    """

    steps: tuple[QueryStep, ...] = ()
    usage: dict[str, int] = Field(default_factory=dict)


__all__ = [
    "DEFAULT_MAX_QUESTION_CHARS",
    "QueryOptions",
    "QueryRequest",
    "QueryResponse",
    "QueryStep",
]
