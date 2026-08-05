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

from api.app import create_app
from api.errors import GENERIC_FAILURE, REQUEST_ID_HEADER
from api.middleware import MAX_REQUEST_ID_CHARS, assign_request_id
from core.exceptions import ConfigurationError
from core.settings import APISettings, Settings

pytestmark = pytest.mark.security


class FakeConnection:
    """Enough psycopg surface for `ping`, and nothing that touches a network."""

    closed = False

    def cursor(self) -> FakeConnection:
        return self

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, *_: object) -> None:
        return None

    def fetchone(self) -> tuple[int]:
        return (1,)

    def close(self) -> None:
        self.closed = True


class FakeCatalog:
    tables = frozenset({"orders"})


class FakeRetriever:
    model_version = "test-model"


class FakeResources:
    """The dependency graph, with every I/O boundary replaced.

    Substituted through `create_app(resource_factory=...)`. Without that seam
    these assertions would need a live PostgreSQL, which would put the security
    suite behind a Docker daemon and make it the first thing to get skipped.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.owner = FakeConnection()
        self.readonly = FakeConnection()
        self.catalog = FakeCatalog()
        self.retriever = FakeRetriever()
        self.closed = False

    def close(self) -> None:
        self.closed = True


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
