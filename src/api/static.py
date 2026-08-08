"""Serving the built demo UI from the same process as the API.

Optional, off unless ``API_STATIC_DIR`` names a directory. Off by default
because the built files are a *build artifact* -- ``web/dist`` is not committed
-- and a server that tried to serve a directory that is usually absent would
have to choose between failing to start and silently serving nothing. Neither
is a good default for an API whose UI is optional.

Two decisions here are security decisions rather than plumbing.

**There is no catch-all mount.** The obvious way to serve a single-page app is
``app.mount("/", StaticFiles(directory=dist, html=True))``, and it is wrong for
this application: a mount at ``/`` matches *every* path no route claimed, so a
request to ``/v1/quary`` -- a typo -- stops being a ``404`` with an error
envelope and becomes ``200 text/html``. A client that receives the demo page
where it expected JSON reports a parse error, and an operator reading access
logs sees a successful request. The API's error contract is a published one and
a static file server is not entitled to overwrite it. So exactly two things are
served: ``/`` and ``/assets/*``. Everything else keeps the ``404`` it had.

**The page is served with a Content-Security-Policy, and the policy is why the
build is configured the way it is.** ``script-src 'self'`` with no
``unsafe-inline`` only holds if every script has its own URL, which is why
``vite.config.ts`` sets ``assetsInlineLimit: 0``. The policy is defence in
depth rather than the control -- the control is that the UI never renders
markup it did not write, and both the row values and the generated SQL are
untrusted for different reasons. But a defence in depth that a build option can
silently disable is one worth writing down next to the option.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

INDEX_FILENAME: Final = "index.html"
ASSETS_DIRNAME: Final = "assets"

CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        # Nothing loads from another origin. The UI ships no web fonts, no CDN
        # and no remote images precisely so this can stay absolute.
        "default-src 'self'",
        # No inline `<script>` and no `eval`. Vite emits every bundle as its own
        # file, so this costs nothing here -- and it is the clause that turns a
        # successful injection into a blocked load rather than execution.
        "script-src 'self'",
        # `unsafe-inline` for styles only. React sets `style` attributes for
        # ordinary layout and this project has no nonce plumbing; the honest
        # position is that a style injection can deface this page and cannot
        # execute script, and the alternative is a policy nobody keeps working.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        # The API this page talks to is its own origin, by design -- see
        # `web/vite.config.ts`. So `connect-src 'self'` is not a restriction
        # the UI has to work around; it is a statement of how it already works,
        # and it means an injected script cannot exfiltrate a result set.
        "connect-src 'self'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    )
)

SECURITY_HEADERS: Final = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    # Without this a browser may sniff a response's type from its bytes, which
    # turns any endpoint that echoes caller-influenced content into a way to
    # get HTML executed under this origin.
    "X-Content-Type-Options": "nosniff",
    # `frame-ancestors` above is the modern control; this is the one older
    # browsers honour. Clickjacking a page whose only button runs a database
    # query is a low prize, but the page also displays results.
    "X-Frame-Options": "DENY",
    # A request id can appear in a URL a person pastes; no referrer means it
    # does not travel to whatever they navigate to next.
    "Referrer-Policy": "no-referrer",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the headers above to every response.

    Every response, not only the HTML one. ``nosniff`` matters on JSON as much
    as on a document, and a policy applied only to the page it was written for
    is a policy that stops applying the moment a second content type is served.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            # `setdefault` semantics: a route that set its own policy meant it.
            if name not in response.headers:
                response.headers[name] = value
        return response


def resolve_static_dir(configured: str) -> Path | None:
    """Validate ``API_STATIC_DIR``, or refuse to start.

    Fails at construction rather than on the first request. A misconfigured
    path that surfaces as a 404 an hour after deployment is indistinguishable
    from a UI that was never built, and the operator who could fix it has
    stopped watching by then.

    Returns:
        The resolved directory, or ``None`` when the setting is empty.

    Raises:
        ConfigurationError: the path is missing, is not a directory, or holds
            no ``index.html``. The last is the one worth checking separately:
            an empty ``dist`` is what a failed or interrupted build leaves
            behind, and serving it would answer every request with a 404 while
            the process reported itself healthy.
    """
    if configured.strip() == "":
        return None

    root = Path(configured).expanduser().resolve()
    if not root.is_dir():
        raise ConfigurationError(f"API_STATIC_DIR={configured!r} is not a directory")
    if not (root / INDEX_FILENAME).is_file():
        raise ConfigurationError(
            f"API_STATIC_DIR={configured!r} has no {INDEX_FILENAME}. "
            "Run `npm run build` in web/ first."
        )
    return root


def mount_ui(app: FastAPI, root: Path) -> None:
    """Serve ``/`` and ``/assets/*`` from a built bundle, and nothing else.

    Must be called **after** the API routers are registered. Starlette matches
    routes in registration order, so a path both could serve goes to whichever
    was added first -- and the API's must be.
    """
    assets = root / ASSETS_DIRNAME
    if assets.is_dir():
        # `StaticFiles` resolves each request against this root and refuses
        # anything that escapes it, which is what makes a caller-supplied path
        # segment safe here. Not relied on alone: only hashed build output
        # lives under this directory.
        app.mount(f"/{ASSETS_DIRNAME}", StaticFiles(directory=assets), name=ASSETS_DIRNAME)

    index = root / INDEX_FILENAME

    @app.get("/", include_in_schema=False)
    async def ui() -> FileResponse:
        # `no-cache` rather than a long max-age: the bundle filenames are
        # content-hashed and cached hard by the mount above, so the only thing
        # this document does is point at the current ones. Caching it is how a
        # browser ends up asking for a bundle that a redeploy replaced.
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    logger.info("serving the demo UI from %s", root)


__all__ = [
    "CONTENT_SECURITY_POLICY",
    "SECURITY_HEADERS",
    "SecurityHeadersMiddleware",
    "mount_ui",
    "resolve_static_dir",
]
