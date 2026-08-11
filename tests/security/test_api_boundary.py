"""What the first network-reachable surface must never do.

Every test here is about a caller who has nothing but the ability to send an
HTTP request -- no credential, no shell on the box, no prior access. That is a
new adversary for this project: everything before the API was a local process
the operator started.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.conftest import build_settings
from tests.fakes_api import FakeConnection, FakeResources, FakeRetriever

from api.app import create_app
from api.errors import GENERIC_FAILURE, REQUEST_ID_HEADER
from api.middleware import MAX_REQUEST_ID_CHARS, assign_request_id
from core.exceptions import ConfigurationError
from core.settings import APISettings, Settings

pytestmark = pytest.mark.security


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(build_settings(), resource_factory=FakeResources)
    with TestClient(app, raise_server_exceptions=False) as started:
        yield started


class TestTheServiceIsClosedByDefault:
    def test_the_default_bind_address_is_loopback(self) -> None:
        """0.0.0.0 by default would serve an unauthenticated database to the LAN."""
        assert APISettings().api_host == "127.0.0.1"

    @pytest.mark.parametrize(
        "host",
        ["0.0.0.0", "::", "192.168.1.10", "db.internal"],  # noqa: S104 - the input under test
    )
    def test_binding_beyond_this_machine_refuses_to_start(self, host: str) -> None:
        """CONFIG.md section 6 calls this a startup error. It is one.

        A warning would be read by nobody, on a service that started fine, and
        the failure mode is an unauthenticated endpoint running generated SQL
        for anyone who can route to it.
        """
        with pytest.raises(ConfigurationError, match=r"no authentication"):
            APISettings(api_host=host)

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
    def test_every_spelling_of_loopback_is_accepted(self, host: str) -> None:
        """A control that only knows one spelling gets a bypass added to it by
        the first person it wrongly blocks."""
        assert APISettings(api_host=host).api_host == host

    def test_an_unresolvable_host_fails_closed(self) -> None:
        """Guessing wrong here puts a database on a network."""
        with pytest.raises(ConfigurationError):
            APISettings(api_host="not-an-address")


class TestTheContainerEscapeHatch:
    """`API_ALLOW_NON_LOOPBACK`, and why the control needed one at all.

    The refusal above used to be absolute, on the stated reasoning that
    containers were unaffected because publishing a port "forwards to loopback
    inside the namespace". **That is false.** ``-p 8000:8000`` forwards to the
    container's interface on the bridge network, so a process bound to
    ``127.0.0.1`` inside the namespace is unreachable through a published port.

    The claim survived because nothing had ever been containerised. Writing the
    Dockerfile tested it, and the failure it would have produced is the quiet
    kind: a container that starts, passes its own health check, logs nothing
    unusual, and answers no request that arrives from outside.

    ADR-049.
    """

    @pytest.mark.parametrize("host", ["0.0.0.0", "::"])  # noqa: S104 - the input under test
    def test_the_hatch_permits_a_wide_bind(self, host: str) -> None:
        assert APISettings(api_host=host, api_allow_non_loopback=True).api_host == host

    def test_it_is_off_by_default(self) -> None:
        """The default is what an unattended deployment gets."""
        assert APISettings().api_allow_non_loopback is False

    @pytest.mark.parametrize(
        "host",
        ["0.0.0.0", "::", "192.168.1.10", "db.internal"],  # noqa: S104 - the input under test
    )
    def test_without_it_the_refusal_is_unchanged(self, host: str) -> None:
        """The hatch must not weaken the default path -- only open a named one."""
        with pytest.raises(ConfigurationError, match=r"no authentication"):
            APISettings(api_host=host, api_allow_non_loopback=False)

    def test_the_refusal_names_the_way_out(self) -> None:
        """An error that forbids without saying what to do instead is how a
        control gets bypassed by whoever needed to get on with their day."""
        with pytest.raises(ConfigurationError) as caught:
            APISettings(api_host="0.0.0.0")  # noqa: S104 - the input under test

        assert "API_ALLOW_NON_LOOPBACK" in str(caught.value)

    def test_it_waives_exposure_and_nothing_else(self) -> None:
        """What the hatch is, stated as a test, including what it does not do.

        With it set, ``API_HOST`` is no longer checked for reachability -- a
        bogus value passes here and fails loudly when uvicorn tries to bind it.
        That is deliberate: a syntactically valid hostname is indistinguishable
        from a typo without a DNS lookup, and resolving in a settings validator
        would reject legitimate interface names on any machine where the lookup
        fails. **A loud bind error is the better failure than a control that
        refuses correct configurations**, which is how a security check earns a
        bypass from whoever it wrongly blocked.
        """
        assert APISettings(api_host="not-an-address", api_allow_non_loopback=True)

        # And the default path still refuses it, so nothing is loosened for
        # anyone who has not opted in.
        with pytest.raises(ConfigurationError):
            APISettings(api_host="not-an-address")

    def test_openapi_is_not_served_by_default(self, client: TestClient) -> None:
        """The schema is a complete map of the attack surface."""
        for path in ("/openapi.json", "/docs", "/redoc"):
            assert client.get(path).status_code == 404, path

    def test_hiding_the_docs_page_is_not_enough(self) -> None:
        """Clearing docs_url alone leaves the machine-readable schema reachable."""
        app = create_app(build_settings(), resource_factory=FakeResources)
        assert app.openapi_url is None

    def test_docs_can_be_turned_on_deliberately(self) -> None:
        app = create_app(build_settings(api_docs_enabled=True), resource_factory=FakeResources)
        with TestClient(app) as enabled:
            assert enabled.get("/openapi.json").status_code == 200

    def test_no_cors_origin_is_trusted_by_default(self, client: TestClient) -> None:
        response = client.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers

    def test_a_wildcard_origin_is_refused_at_configuration_time(self) -> None:
        """Not a runtime check -- the process must not start this way."""
        with pytest.raises(ConfigurationError, match=r"must not contain"):
            APISettings(api_cors_origins=("*",))  # type: ignore[arg-type]

    def test_a_named_origin_never_carries_credentials(self) -> None:
        app = create_app(
            build_settings(api_cors_origins=("https://app.example",)),  # type: ignore[arg-type]
            resource_factory=FakeResources,
        )
        with TestClient(app) as configured:
            response = configured.get("/health", headers={"Origin": "https://app.example"})
            assert response.headers["access-control-allow-origin"] == "https://app.example"
            assert "access-control-allow-credentials" not in response.headers


class TestProbesRevealNothing:
    def test_health_says_only_that_it_is_alive(self, client: TestClient) -> None:
        """No version, no hostname, no uptime -- all of it is a fingerprint."""
        body = client.get("/health").json()
        assert body == {"status": "ok"}

    def test_health_does_not_touch_the_database(self) -> None:
        """Liveness that depended on Postgres would restart the fleet in an outage."""
        touched: list[str] = []

        class WatchedConnection(FakeConnection):
            def execute(self, *_: object) -> None:
                touched.append("query")

        class WatchedResources(FakeResources):
            def __init__(self, settings: Settings) -> None:
                super().__init__(settings)
                self.owner = WatchedConnection()
                self.readonly = WatchedConnection()

        app = create_app(build_settings(), resource_factory=WatchedResources)
        with TestClient(app) as watched:
            touched.clear()
            watched.get("/health")
        assert touched == []

    def test_ready_reports_two_fixed_words_per_dependency(self, client: TestClient) -> None:
        body = client.get("/ready").json()
        assert body["status"] == "ready"
        assert set(body["dependencies"].values()) == {"up"}

    def test_a_failed_dependency_does_not_publish_the_reason(self) -> None:
        """psycopg quotes the DSN back on failure. It must not reach the response."""

        class BrokenConnection(FakeConnection):
            def execute(self, *_: object) -> None:
                raise RuntimeError(
                    "connection to server at 10.0.4.19 failed: "
                    "postgresql://sql_agent_login:hunter2@db.internal/prod"
                )

        class BrokenResources(FakeResources):
            def __init__(self, settings: Settings) -> None:
                super().__init__(settings)
                self.readonly = BrokenConnection()

        app = create_app(build_settings(), resource_factory=BrokenResources)
        with TestClient(app, raise_server_exceptions=False) as broken:
            response = broken.get("/ready")

        assert response.status_code == 503
        text = response.text
        assert "hunter2" not in text
        assert "db.internal" not in text
        assert "10.0.4.19" not in text
        assert response.json()["error"]["details"]["dependencies"]["database_readonly"] == "down"


class TestTheErrorEnvelopeDoesNotLeak:
    def test_an_unknown_path_still_uses_the_envelope(self, client: TestClient) -> None:
        """Starlette's default is {"detail": ...}, which is a second shape to parse."""
        body = client.get("/v1/nonexistent").json()
        assert set(body) == {"error"}
        assert body["error"]["code"] == "not_found"
        assert body["error"]["request_id"]

    def test_an_unhandled_exception_publishes_nothing_about_itself(self) -> None:
        app = create_app(build_settings(), resource_factory=FakeResources)

        @app.get("/boom")
        async def boom() -> None:
            raise RuntimeError("password=hunter2 at /srv/app/secret.py line 12")

        with TestClient(app, raise_server_exceptions=False) as failing:
            response = failing.get("/boom")

        assert response.status_code == 500
        assert response.json()["error"]["message"] == GENERIC_FAILURE
        assert "hunter2" not in response.text
        assert "secret.py" not in response.text

    def test_every_response_carries_a_request_id_header(self, client: TestClient) -> None:
        for path in ("/health", "/ready", "/v1/nonexistent"):
            assert client.get(path).headers[REQUEST_ID_HEADER], path


class TestRequestIdIsNotTrusted:
    @pytest.mark.parametrize(
        "hostile",
        [
            "abc\nERROR authentication bypassed for admin",
            "abc\r\nSet-Cookie: session=stolen",
            "abc fake",
            "<script>alert(1)</script>",
            "a" * (MAX_REQUEST_ID_CHARS + 1),
            "",
            "id with spaces",
        ],
    )
    def test_a_hostile_value_is_replaced_not_echoed(self, hostile: str) -> None:
        """Log injection (CWE-117) and response splitting, in one input."""
        assigned = assign_request_id(hostile)
        assert assigned != hostile
        assert assigned.startswith("req_")

    def test_a_trailing_newline_is_rejected(self) -> None:
        """`$` matches before a trailing newline in Python; `\\Z` does not."""
        assert assign_request_id("abcdef\n") != "abcdef\n"

    def test_a_usable_value_is_preserved(self) -> None:
        """A gateway's trace id must survive, or the trace stops at this hop."""
        assert assign_request_id("0af7651916cd43dd8448eb211c80319c") == (
            "0af7651916cd43dd8448eb211c80319c"
        )

    def test_a_hostile_header_does_not_reach_the_response(self, client: TestClient) -> None:
        response = client.get("/health", headers={REQUEST_ID_HEADER: "abc\r\nX-Injected: yes"})
        assert "x-injected" not in response.headers
        assert response.headers[REQUEST_ID_HEADER].startswith("req_")


class TestStartupAndShutdown:
    def test_resources_are_closed_when_the_app_stops(self) -> None:
        built: list[FakeResources] = []

        def factory(settings: Settings) -> FakeResources:
            resources = FakeResources(settings)
            built.append(resources)
            return resources

        with TestClient(create_app(build_settings(), resource_factory=factory)):
            pass

        assert built
        assert built[0].closed

    def test_a_dependency_that_cannot_open_stops_the_process(self) -> None:
        """Not a degraded start. A boundary checked later is a boundary missed."""

        class RefusingResources:
            """A read-only URL that turned out to be writable, as
            `composition.assert_read_only` reports it."""

            def __init__(self, settings: Settings) -> None:
                self.settings = settings

            @property
            def readonly(self) -> FakeConnection:
                raise ConfigurationError("DATABASE_RO_URL can write")

            def close(self) -> None:
                return None

        with (
            pytest.raises(ConfigurationError),
            TestClient(
                create_app(build_settings(), resource_factory=RefusingResources)  # type: ignore[arg-type]
            ),
        ):
            pass  # pragma: no cover - the context manager raises on entry


class TestTheBodyCapIsNotAdvisory:
    """An unauthenticated caller must not choose how much this process allocates.

    The cap is enforced twice on purpose. ``Content-Length`` is the cheap
    refusal -- a caller declaring 90 MB is turned away before a byte arrives --
    but it is a *claim*, and the request that matters is the one that lies
    about it or omits it entirely under chunked encoding. Checking only the
    header is a limit any attacker can opt out of.
    """

    def test_an_oversized_body_is_refused(self) -> None:
        app = create_app(build_settings(api_max_body_bytes=2048), resource_factory=FakeResources)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/v1/query", json={"question": "x" * 5000})

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"

    def test_a_lying_content_length_does_not_get_through(self) -> None:
        """The header is understated; the real body is not. A cap that trusts
        the declaration is a cap the caller sets."""
        app = create_app(build_settings(api_max_body_bytes=1024), resource_factory=FakeResources)
        oversized = b'{"question": "' + b"x" * 4000 + b'"}'

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/v1/query",
                content=oversized,
                headers={"Content-Type": "application/json", "Content-Length": "10"},
            )

        assert response.status_code != 200

    def test_a_refusal_still_carries_a_request_id(self) -> None:
        """The cap runs outside the correlation middleware, so it has to assign
        its own. An operator investigating a flood of 413s needs the same key
        as for any other traffic."""
        app = create_app(build_settings(api_max_body_bytes=1024), resource_factory=FakeResources)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/v1/query", json={"question": "x" * 4000})

        assert response.headers.get(REQUEST_ID_HEADER)
        assert response.json()["error"]["request_id"]

    def test_an_ordinary_request_is_unaffected(self) -> None:
        """A limit that also refuses legitimate traffic gets raised until it
        stops being a limit."""
        app = create_app(build_settings(), resource_factory=FakeResources)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/v1/query", json={"question": "how many orders?"})

        assert response.status_code != 413


class TestThePoolIsNotShared:
    def test_each_query_borrows_and_returns_a_connection(self) -> None:
        """Two concurrent requests on one connection would interleave their
        transactions -- and `statement_timeout` is set per transaction, so one
        request would run under another's limit. A leaked connection is
        invisible until the pool is exhausted."""
        app = create_app(build_settings(), resource_factory=FakeResources)
        with TestClient(app, raise_server_exceptions=False) as client:
            client.post("/v1/query", json={"question": "how many orders?"})
            pool = client.app.state.resources.readonly_pool  # type: ignore[attr-defined]

        assert pool.borrowed == pool.returned, "a connection was not returned to the pool"

    def test_the_pool_is_closed_with_the_process(self) -> None:
        app = create_app(build_settings(), resource_factory=FakeResources)
        with TestClient(app) as client:
            resources = client.app.state.resources  # type: ignore[attr-defined]

        assert resources.closed


class TestStartupOpensTheModel:
    """The lifespan promises eager, and one dependency was quietly lazy.

    `app._lifespan` states that touching each accessor moves configuration
    failures from the first request to process start. It touched the retriever
    -- but `model_version` returns a configured string and loads nothing, so a
    sentence-transformer sat with its checkpoint unopened while the log line
    said `ready`.

    Two costs, and the second is what hid the first. A missing or corrupt
    checkpoint surfaced on the first request rather than at startup, which is
    the exact failure the eager lifespan exists to prevent. And the load is
    tens of seconds of CPU, paid by whoever asked first -- invisible in the
    `steps` array because retrieval and generation are timed together as one
    `answer` stage. The per-stage events added for streaming are what
    separated them, and the difference measured live was 21.8 s cold against
    0.5 s warm.
    """

    def test_the_model_is_opened_before_the_first_request(self) -> None:
        app = create_app(build_settings(), resource_factory=FakeResources)

        with TestClient(app) as client:
            retriever = client.app.state.resources.retriever  # type: ignore[attr-defined]
            assert retriever.model_opened, "startup left the checkpoint unopened"

    def test_reporting_readiness_is_not_enough_on_its_own(self) -> None:
        """`model_version` must not be mistaken for a liveness signal again.

        It answers from configuration, so it cannot fail and cannot report
        anything about the model. This pins that it stays cheap -- if it ever
        starts loading, the startup check above becomes redundant in a way
        nobody would notice.
        """
        retriever = FakeRetriever()

        assert retriever.model_version == "test-model"
        assert not retriever.model_opened
