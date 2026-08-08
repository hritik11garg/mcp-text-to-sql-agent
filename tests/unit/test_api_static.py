"""Serving the demo UI without giving up the API's contract or its headers.

Two properties are being defended here and they pull in opposite directions.
The UI has to be reachable from a browser at ``/``; the API has to keep
answering ``404`` with an error envelope for a path it does not serve. The
naive way to get the first -- a static mount at ``/`` -- silently gives up the
second, and nothing about the page would look wrong afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.conftest import build_settings
from tests.fakes_api import FakeResources

from api.app import create_app
from api.static import CONTENT_SECURITY_POLICY, SECURITY_HEADERS, resolve_static_dir
from core.exceptions import ConfigurationError


def build_dist(root: Path, *, with_assets: bool = True) -> Path:
    """A directory shaped like `npm run build` output."""
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div>'
        '<script type="module" src="/assets/main.js"></script></body></html>',
        encoding="utf-8",
    )
    if with_assets:
        assets = dist / "assets"
        assets.mkdir()
        (assets / "main.js").write_text("export const x = 1;\n", encoding="utf-8")
    return dist


@pytest.fixture
def ui_client(tmp_path: Path) -> Iterator[TestClient]:
    dist = build_dist(tmp_path)
    app = create_app(
        build_settings(api_static_dir=str(dist)),
        resource_factory=FakeResources,
    )
    with TestClient(app, raise_server_exceptions=False) as started:
        yield started


@pytest.fixture
def api_only_client() -> Iterator[TestClient]:
    app = create_app(build_settings(), resource_factory=FakeResources)
    with TestClient(app, raise_server_exceptions=False) as started:
        yield started


class TestTheUIIsOffUnlessConfigured:
    def test_the_default_serves_no_ui(self) -> None:
        """`web/dist` is a build artifact and is usually absent."""
        assert resolve_static_dir("") is None

    def test_whitespace_is_treated_as_unset(self) -> None:
        assert resolve_static_dir("   ") is None

    def test_the_root_path_is_a_404_when_no_ui_is_configured(
        self, api_only_client: TestClient
    ) -> None:
        response = api_only_client.get("/")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestAMisconfiguredPathRefusesToStart:
    """Fail at construction, not on the first request an hour later.

    A 404 from a mistyped path is indistinguishable from a UI nobody built, and
    the person who could fix it has stopped watching by the time anyone tries.
    """

    def test_a_missing_directory_is_a_configuration_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not a directory"):
            resolve_static_dir(str(tmp_path / "nope"))

    def test_a_file_is_not_a_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "index.html"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="not a directory"):
            resolve_static_dir(str(target))

    def test_a_directory_with_no_index_is_refused(self, tmp_path: Path) -> None:
        """What an interrupted build leaves behind."""
        empty = tmp_path / "dist"
        empty.mkdir()
        with pytest.raises(ConfigurationError, match=r"index\.html"):
            resolve_static_dir(str(empty))

    def test_a_build_with_no_assets_directory_still_serves(self, tmp_path: Path) -> None:
        """A bundle small enough to have no `assets/` is unusual, not broken."""
        dist = build_dist(tmp_path, with_assets=False)
        app = create_app(build_settings(api_static_dir=str(dist)), resource_factory=FakeResources)
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/").status_code == 200


class TestTheUIDoesNotSwallowTheAPIsErrors:
    """The reason there is no catch-all mount.

    `app.mount("/", StaticFiles(html=True))` answers every unmatched path with
    the demo page and a 200. A client that asked for JSON gets HTML, a typo in
    an endpoint looks like a success in the access log, and the published error
    contract is gone without any single thing appearing to be broken.
    """

    def test_the_ui_is_served_at_the_root(self, ui_client: TestClient) -> None:
        response = ui_client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert 'id="root"' in response.text

    def test_a_hashed_bundle_is_served(self, ui_client: TestClient) -> None:
        assert ui_client.get("/assets/main.js").status_code == 200

    @pytest.mark.parametrize("path", ["/v1/quary", "/v1/query/extra", "/v2/query", "/nope"])
    def test_an_unknown_path_is_still_a_json_404(self, ui_client: TestClient, path: str) -> None:
        response = ui_client.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["error"]["code"] == "not_found"

    def test_the_query_route_still_wins_over_the_static_files(self, ui_client: TestClient) -> None:
        """Registration order decides, and the API is registered first."""
        response = ui_client.get("/v1/query")
        assert response.status_code == 405
        assert response.json()["error"]["code"] == "method_not_allowed"

    def test_health_still_answers_json(self, ui_client: TestClient) -> None:
        assert ui_client.get("/health").json()["status"] == "ok"


class TestSecurityHeaders:
    def test_the_page_carries_the_policy(self, ui_client: TestClient) -> None:
        assert ui_client.get("/").headers["content-security-policy"] == CONTENT_SECURITY_POLICY

    @pytest.mark.parametrize("directive", ["script-src 'self'", "frame-ancestors 'none'"])
    def test_the_policy_blocks_inline_script_and_framing(self, directive: str) -> None:
        assert directive in CONTENT_SECURITY_POLICY

    def test_the_policy_has_no_unsafe_inline_for_scripts(self) -> None:
        """`assetsInlineLimit: 0` in the Vite config is what keeps this true."""
        script_src = next(
            part for part in CONTENT_SECURITY_POLICY.split("; ") if part.startswith("script-src")
        )
        assert "unsafe-inline" not in script_src
        assert "unsafe-eval" not in script_src

    @pytest.mark.parametrize("header", sorted(SECURITY_HEADERS))
    def test_every_header_reaches_an_api_response_too(
        self, api_only_client: TestClient, header: str
    ) -> None:
        """Not only the HTML. `nosniff` on JSON is the case it matters most."""
        assert header.lower() in {k.lower() for k in api_only_client.get("/health").headers}

    def test_headers_survive_a_response_no_route_produced(
        self, api_only_client: TestClient
    ) -> None:
        """The body cap refuses before routing, and that response needs them too."""
        oversized = b"x" * (64 * 1024 + 1)
        response = api_only_client.post(
            "/v1/query", content=oversized, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_headers_reach_a_404(self, api_only_client: TestClient) -> None:
        assert api_only_client.get("/nope").headers["x-frame-options"] == "DENY"
