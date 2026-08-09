"""The request body, fuzzed. The second of ENGINEERING_MATRIX section 37's targets.

`POST /v1/query` is the one surface an unauthenticated caller can send
arbitrary structure to, and everything it does before a route sees it --
`Content-Length` checking, byte counting, JSON parsing, pydantic validation --
runs on bytes nobody chose.

**The claim is narrow on purpose.** Not that any particular body is rejected:
`{"question": "hi"}` is a generated body that should be accepted. What is
asserted is that **no body produces a 5xx, and every refusal answers in the
one envelope** -- because an unhandled exception on this path is both a denial
of service and, per SECURITY.md 13.3, a potential disclosure through whatever
the framework decides to render.

**And that the refusal does not reflect what was sent.** That is the property
this file exists for, and it is the one that was already broken -- see
`TestTheRefusalDoesNotEchoTheRequest`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.conftest import build_settings
from tests.fakes_api import FakeResources

from api.app import create_app
from api.errors import MAX_FIELD_PATH_CHARS, REQUEST_ID_HEADER

pytestmark = [pytest.mark.security, pytest.mark.property]


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """Module-scoped: the app is stateless between requests here, and building
    it per example would put a fake dependency graph behind every one of five
    hundred generated bodies."""
    app = create_app(build_settings(), resource_factory=FakeResources)
    with TestClient(app, raise_server_exceptions=False) as started:
        yield started


# --- what a hostile client sends -------------------------------------------

KEYS = st.one_of(
    st.sampled_from(["question", "stream", "options", "session_id", "max_rows", ""]),
    st.text(max_size=30),
)
"""Real field names mixed with generated ones.

The real names matter more than they look: a body that is *nearly* right
exercises type coercion and the nested `options` model, while a body of pure
noise only ever reaches "unknown field".
"""

VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=50),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=10), children, max_size=3)
    ),
    max_leaves=8,
)

BODIES = st.dictionaries(KEYS, VALUES, max_size=6)

PROFILE = settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
"""The client fixture is module-scoped, but Hypothesis cannot see that through
the wrapper and warns anyway. Suppressed with the scope stated, rather than
widening the strategy until the warning stops."""


def post(client: TestClient, body: Any) -> Any:
    return client.post("/v1/query", json=body)


class TestNoBodyProducesAnUnhandledFailure:
    @PROFILE
    @given(BODIES)
    def test_the_status_is_never_a_server_error(
        self, client: TestClient, body: dict[str, Any]
    ) -> None:
        """A 5xx here is an unhandled exception on an unauthenticated path.

        `raise_server_exceptions=False` is what makes this meaningful: without
        it the TestClient re-raises and the test fails with the traceback
        instead of the status a real caller would receive.
        """
        assert post(client, body).status_code < 500

    @PROFILE
    @given(BODIES)
    def test_every_refusal_uses_the_one_envelope(
        self, client: TestClient, body: dict[str, Any]
    ) -> None:
        """A client that handles failures in one place must not need a second
        parser for whichever body happened to be malformed."""
        response = post(client, body)
        if response.status_code >= 400:
            error = response.json()["error"]
            assert set(error) >= {"code", "message", "request_id"}
            assert isinstance(error["code"], str)

    @PROFILE
    @given(BODIES)
    def test_every_response_carries_a_request_id(
        self, client: TestClient, body: dict[str, Any]
    ) -> None:
        """Including the refusals, which is where an operator needs it most --
        the log line correlating to a rejected request is the only record it
        happened."""
        assert post(client, body).headers.get(REQUEST_ID_HEADER)

    @given(st.text(max_size=200))
    def test_a_non_json_body_is_refused_rather_than_crashing(self, raw: str) -> None:
        """Bytes that are not JSON at all, which never reach pydantic."""
        app = create_app(build_settings(), resource_factory=FakeResources)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/v1/query", content=raw.encode(), headers={"Content-Type": "application/json"}
            )

        assert response.status_code < 500


class TestTheRefusalDoesNotEchoTheRequest:
    """The property this file was written for, and it was already broken.

    `api.errors._fields` strips pydantic's `input` -- the offending *value* --
    and its docstring says why: a request body appearing in a response ends up
    in any log or error tracker that records responses.

    It did not strip the **field path**, and with `extra="forbid"` the path of
    an unknown field *is* caller-supplied text. `{"question": "hi",
    "<script>alert(1)</script>": 1}` came back as
    `body.<script>alert(1)</script>`, unbounded in length and unrestricted in
    content. Found by reading the handler while writing this file, confirmed by
    hand, and fixed in the same commit; these are the regression guard.

    **Not rated as a serious vulnerability, and the reasoning is on the
    record.** The response is `application/json`, the demo UI renders no markup
    anywhere (invariant I-9), and the body cap bounds the volume. What it *is*
    is a control whose docstring described behaviour the code did not have.
    """

    @PROFILE
    @given(BODIES)
    def test_no_submitted_value_appears_in_the_response(
        self, client: TestClient, body: dict[str, Any]
    ) -> None:
        response = post(client, body)
        if response.status_code < 400:
            return
        rendered = response.text
        for value in body.values():
            if isinstance(value, str) and len(value) >= 8:
                assert value not in rendered

    @PROFILE
    @given(BODIES)
    def test_every_reported_field_path_is_bounded_and_plain(
        self, client: TestClient, body: dict[str, Any]
    ) -> None:
        """Bounded *and* restricted, because either alone is insufficient: a
        64-character `<script>` fragment is short, and an unbounded identifier
        is still an amplifier."""
        response = post(client, body)
        if response.status_code >= 400:
            for entry in response.json()["error"].get("details", {}).get("fields", []):
                path = entry["field"]
                assert len(path) <= MAX_FIELD_PATH_CHARS
                assert all(c.isalnum() or c in "._[]-<>" for c in path), path

    @given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=4, max_size=120))
    def test_a_hostile_field_name_is_never_reflected_verbatim(self, name: str) -> None:
        """Generated field *names*, which is where the defect lived.

        `BODIES` reaches this too, but rarely and never at length; naming the
        case keeps it from being a lucky draw.

        **`min_size=4` because a substring check is meaningless below it.** The
        first version allowed single characters and failed on the name `"`,
        which is "contained in" every JSON response ever written -- the
        response was correct (`body.<unnamed>`) and the assertion was not. Same
        reason the value check above only inspects strings of eight or more.
        """
        app = create_app(build_settings(), resource_factory=FakeResources)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/v1/query", json={"question": "hi", name: 1})

        assert response.status_code < 500
        if len(name) > 64 or not name.replace("_", "").replace("-", "").isalnum():
            assert name not in response.text


class TestTheBoundsHoldOverGeneratedInput:
    @given(st.integers(min_value=0, max_value=4000))
    def test_a_question_is_refused_by_length_never_truncated(self, length: int) -> None:
        """Truncating would answer a question the caller did not ask, which is
        worse than refusing: the answer would look valid."""
        app = create_app(build_settings(), resource_factory=FakeResources)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/v1/query", json={"question": "a" * length})

        if length == 0 or length > 2000:
            assert response.status_code == 400
        else:
            assert response.status_code != 400

    @given(st.integers(min_value=1, max_value=6))
    def test_an_oversized_body_is_refused_before_it_is_parsed(self, factor: int) -> None:
        """The cap is enforced on bytes received, not on a header the sender
        controls -- so this sends a real oversized body rather than a lie about
        one, which is covered separately in `test_api_errors.py`."""
        app = create_app(build_settings(), resource_factory=FakeResources)
        payload = json.dumps({"question": "a" * (256_000 * factor)})
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/v1/query", content=payload, headers={"Content-Type": "application/json"}
            )

        assert response.status_code == 413
        assert "a" * 100 not in response.text
