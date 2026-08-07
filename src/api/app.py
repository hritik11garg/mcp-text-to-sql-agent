"""The application factory. Wiring only -- no business logic lives here.

Two things this module is responsible for that are easy to get wrong by
omission rather than by mistake.

**Startup either proves the deployment is safe, or the process does not
start.** The lifespan opens the read-only connection, and opening it runs
:func:`composition.assert_read_only`, which refuses a role that can write.
Failing at startup is the whole point: a boundary checked on the first request
is a boundary that a deployment can sit behind for a week before discovering it
was never there.

**Defaults are closed, and the open positions are configuration.** Docs are
off, CORS trusts nobody, and the bind address is loopback. See
:class:`core.settings.APISettings` for why each one is that way round.

There is no authentication here yet, and that is a stated gap rather than an
oversight -- see docs/operations/SECURITY.md. Until it lands, this service is
only safe on a network where every caller is already trusted, which is what the
loopback default enforces for anyone who does not change it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api import errors, query
from api.errors import NOT_READY, ApiError
from api.health import Probe, Readiness, build_router, ping
from api.middleware import BodySizeLimitMiddleware, RequestIdMiddleware
from api.query import QueryService
from composition import Resources
from composition.answering import build_answerer, build_executor
from core.settings import Settings

logger = logging.getLogger(__name__)

TITLE = "Text-to-SQL Analytics Agent"

ResourceFactory = Callable[[Settings], Resources]


def create_app(
    settings: Settings | None = None,
    *,
    resource_factory: ResourceFactory = Resources,
) -> FastAPI:
    """Build the application.

    Args:
        settings: Loaded from the environment when omitted. Injected in tests
            so a case can state the configuration it is about.
        resource_factory: Builds the dependency graph. Injected so the routing,
            the error envelope and the probe contract can be tested without a
            database -- the alternative is a suite that either needs Postgres
            for every assertion or asserts none of them.
    """
    settings = settings or Settings.load()

    app = FastAPI(
        title=TITLE,
        version="0.1.0",
        lifespan=partial(_lifespan, settings=settings, resource_factory=resource_factory),
        # Serving the schema is a decision, not a default. `openapi_url=None`
        # is what actually removes it -- leaving it set and only clearing
        # `docs_url` hides the rendered page while the machine-readable map of
        # every route stays reachable, which helps nobody except whoever is
        # enumerating it.
        docs_url="/docs" if settings.api.api_docs_enabled else None,
        redoc_url="/redoc" if settings.api.api_docs_enabled else None,
        openapi_url="/openapi.json" if settings.api.api_docs_enabled else None,
    )

    errors.install(app)
    app.add_middleware(RequestIdMiddleware)
    # Added *after* RequestIdMiddleware, which puts it **outside**: Starlette
    # builds the stack in reverse, so the last one added runs first. That is
    # the order this needs -- the body cap has to refuse before anything reads
    # the body, and a correlation id it can attach itself is a smaller loss
    # than a limit that only applies to bodies already in memory.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.api.api_max_body_bytes)

    # Built here, filled in the lifespan. The route table and the middleware
    # stack are frozen when the app starts serving, so a router added later
    # would depend on Starlette continuing to consult a mutable list -- an
    # implementation detail to hang two endpoints on. Until `configure` runs,
    # `/ready` answers 503, which is the honest answer during startup anyway.
    app.state.readiness = Readiness()
    app.state.query_service = None
    app.include_router(build_router(app.state.readiness))
    app.include_router(query.build_router(_query_service))

    if settings.api.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.api.api_cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", errors.REQUEST_ID_HEADER],
        )

    return app


@asynccontextmanager
async def _lifespan(
    app: FastAPI, *, settings: Settings, resource_factory: ResourceFactory
) -> AsyncIterator[None]:
    """Open every dependency before serving, and close them all after.

    Eager, not lazy. Each accessor below is a property that connects on first
    use, so touching them here moves every configuration failure -- an
    unreachable database, an unindexed catalog, a read-only URL that can write
    -- from the first request to the moment the process starts, where a
    deployment is watching and a rollback is still cheap.
    """
    resources = resource_factory(settings)
    app.state.resources = resources
    try:
        # Order matters for the message an operator sees: the read-only
        # assertion runs inside `.readonly`, and reporting "your boundary is
        # missing" is more useful than reporting a catalog that could not load
        # because the connection it needed was refused.
        readonly = resources.readonly
        owner = resources.owner
        catalog = resources.catalog
        retriever = resources.retriever

        # `model_version` is the model *name*, and reading it loads nothing --
        # which is how this line reported a ready retriever over an embedder
        # that had not opened its model file. `dimensions` is the property that
        # forces the load, so it is read here on purpose and not merely logged.
        #
        # Two things wrong without it, and the second is the one that hid the
        # first. The lifespan's promise at the top of this module is that
        # configuration failures surface at startup rather than on the first
        # request; a missing or corrupt checkpoint surfaced on the first
        # request. And the load costs about twenty seconds of CPU, which the
        # first caller paid -- invisible in the `steps` array, because
        # retrieval and generation are timed together as one `answer` stage.
        # The stage events added for streaming are what separated them.
        dimensions = retriever.dimensions

        logger.info(
            "ready: %d tables in catalog, retriever model_version=%s, dimensions=%d",
            len(catalog.tables),
            retriever.model_version,
            dimensions,
        )

        app.state.query_service = QueryService(
            build_answerer(resources),
            build_executor(resources),
            max_concurrent=settings.api.api_max_concurrent_requests,
            keepalive_seconds=settings.api.api_stream_keepalive_seconds,
        )

        app.state.readiness.configure(
            [
                Probe("database", partial(ping, owner)),
                Probe("database_readonly", partial(ping, readonly)),
                # No probe for the catalog or the retriever. Both were loaded
                # at startup and are held in memory, so a probe would assert
                # that this process still has its own attributes -- a check
                # that cannot fail is a check that reports nothing.
            ]
        )
        yield
    finally:
        resources.close()


def _query_service(request: Request) -> QueryService:
    """The service, or a 503 if the lifespan has not built it yet.

    Not an `assert` and not an attribute error. A request arriving before
    startup finished is an ordinary race during a rolling deploy, and the
    honest answer is the same one `/ready` is giving at that moment -- the
    process is up and cannot serve yet. An unhandled `AttributeError` would
    answer 500, which reads as a defect and invites a retry against a replica
    that is fine.
    """
    service = getattr(request.app.state, "query_service", None)
    if not isinstance(service, QueryService):
        raise ApiError(NOT_READY, "the service is still starting")
    return service


__all__ = ["TITLE", "create_app"]
