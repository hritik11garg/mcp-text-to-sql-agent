"""Liveness and readiness, and the difference between them.

An orchestrator does two different things with these answers, and conflating
them produces the worst possible behaviour in an outage:

- ``/health`` failing means *restart this process*.
- ``/ready`` failing means *stop sending it traffic, leave it alone*.

So ``/health`` must not check the database. If it did, a database that went
away for thirty seconds would fail every pod's liveness probe at once and the
orchestrator would restart the entire fleet -- adding a cold start, a
connection storm, and a catalog reload to an incident that was going to resolve
itself. The process is alive; it just has nothing to serve. That is precisely
what ``/ready`` is for.

Both are unauthenticated, because a kubelet cannot hold a credential and a load
balancer will not. That makes them the two endpoints an unauthenticated
attacker can always reach, which decides everything below:

**They must not be a fingerprint.** No version, no hostname, no library names,
no uptime. A version string tells someone enumerating the service which
published vulnerabilities to try, and it is worth nothing to the orchestrator
that is the actual audience.

**Readiness must not report *why*.** Each dependency is one of two fixed
words. A driver message would carry the DSN, the host, and the role name --
psycopg quotes the connection string back on failure, which is exactly the leak
``core.dsn.redact_dsn`` exists for. "up" or "down" is what a probe consumes.
An operator gets the real cause from the log, correlated by ``request_id``.

**They must not be an amplifier.** A probe that opened a connection would let
an unauthenticated caller exhaust the pool with a loop. These reuse the
connection the process already holds and run the cheapest statement there is,
and the result is cached for :data:`READINESS_TTL_SECONDS` so the cost of being
probed does not scale with how often somebody probes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.errors import NOT_READY, REQUEST_ID_HEADER, request_id_of

logger = logging.getLogger(__name__)

READINESS_TTL_SECONDS: Final = 5.0
"""How long a readiness answer is reused.

Short enough that a dependency failure is noticed within one probe interval;
long enough that being probed is not itself the load. Kubernetes defaults to a
10-second period, so at most one real check runs per probe even with several
replicas of the probe itself.
"""

UP: Final = "up"
DOWN: Final = "down"
"""The complete vocabulary of a dependency's status. Two words, by design --
see the module docstring."""


@dataclass(frozen=True, slots=True)
class Probe:
    """One dependency and the cheapest question that proves it is there.

    ``check`` raises to mean "not ready". Raising rather than returning a bool
    is what lets the *reason* reach the log while only the verdict reaches the
    response -- a bool would have discarded it at the call site.
    """

    name: str
    check: Callable[[], None]


class Readiness:
    """Runs the probes, and remembers the answer for a moment.

    Holds no connections of its own. The probes close over whatever the
    application already opened at startup, so this class cannot become a second
    place that decides how to reach the database.

    Constructed empty and filled by :meth:`configure` during startup, because
    the route has to exist before the dependencies do -- the router is built
    when the app is, and the connections are opened in the lifespan. That order
    is not negotiable, so the gap between the two is made explicit here rather
    than papered over by mutating the route table mid-flight.
    """

    def __init__(self, probes: Sequence[Probe] = (), *, ttl: float = READINESS_TTL_SECONDS) -> None:
        self._probes: tuple[Probe, ...] | None = tuple(probes) or None
        self._ttl = ttl
        self._cached: tuple[float, bool, dict[str, str]] | None = None

    def configure(self, probes: Sequence[Probe]) -> None:
        """Install the probes once startup has something to probe."""
        self._probes = tuple(probes)
        self._cached = None

    async def status(self) -> tuple[bool, dict[str, str]]:
        """``(ready, {dependency: "up" | "down"})``, possibly from cache.

        Unconfigured is **not ready**, and deliberately not "ready with nothing
        to report". ``all()`` of an empty sequence is ``True``, so the naive
        version answers 200 during startup and again after any refactor that
        drops a probe -- a readiness endpoint whose failure mode is a
        confident yes is worse than not having one.

        Async because the probes are **blocking**: psycopg's sync driver is
        what this process holds, and running ``SELECT 1`` on it directly inside
        a route would stall the event loop for every other request. That is
        cheap when the database is healthy and unbounded when it is not -- a
        TCP write to a peer that has gone away blocks until the OS gives up,
        and the moment a readiness probe blocks is precisely the moment
        everything else needs the loop. See CODE_STYLE.md section 6, rule 1.
        """
        if self._probes is None:
            return False, {"startup": DOWN}

        now = time.monotonic()
        if self._cached is not None and now - self._cached[0] < self._ttl:
            _, ready, detail = self._cached
            return ready, dict(detail)

        # One thread for the whole refresh rather than one per probe: they are
        # sequential anyway, and each hop off the loop costs more than the
        # `SELECT 1` it is protecting.
        detail = await asyncio.to_thread(self._probe_all)
        ready = all(state == UP for state in detail.values())
        self._cached = (now, ready, detail)
        return ready, dict(detail)

    def _probe_all(self) -> dict[str, str]:
        """Every probe, in order. Runs off the event loop -- see :meth:`status`."""
        probes = self._probes or ()
        return {probe.name: self._run(probe) for probe in probes}

    def _run(self, probe: Probe) -> str:
        try:
            probe.check()
        except Exception:
            # Never re-raised and never rendered. `logger.exception` puts the
            # driver's message -- connection string included -- in the process
            # log, which is a place an operator already has access to and a
            # caller does not.
            logger.exception("readiness probe %r failed", probe.name)
            return DOWN
        return UP


def build_router(readiness: Readiness) -> APIRouter:
    """The two probe endpoints, closed over the readiness checker.

    A factory rather than a module-level router with a global, because a test
    that cannot construct the thing under test with fakes ends up asserting
    against the real database or against nothing.
    """
    router = APIRouter(tags=["operations"])

    @router.get("/health", summary="Process liveness")
    async def health(request: Request) -> JSONResponse:
        """Alive. Deliberately checks nothing else -- see the module docstring."""
        return JSONResponse(
            status_code=200,
            content={"status": "ok"},
            headers={REQUEST_ID_HEADER: request_id_of(request)},
        )

    @router.get("/ready", summary="Dependency readiness")
    async def ready(request: Request) -> JSONResponse:
        is_ready, dependencies = await readiness.status()
        request_id = request_id_of(request)

        if is_ready:
            return JSONResponse(
                status_code=200,
                content={"status": "ready", "dependencies": dependencies},
                headers={REQUEST_ID_HEADER: request_id},
            )

        # The error envelope, not a bare body: a client that handles failures
        # in one place should not need a second parser for this one path. The
        # per-dependency map rides in `details`, which is where the envelope
        # puts structured context.
        body: dict[str, Any] = {
            "code": NOT_READY.code,
            "message": "one or more dependencies are unavailable",
            "request_id": request_id,
            "details": {"dependencies": dependencies},
        }
        return JSONResponse(
            status_code=NOT_READY.status,
            content={"error": body},
            headers={REQUEST_ID_HEADER: request_id},
        )

    return router


def ping(connection: Any) -> None:
    """The cheapest proof a connection is still usable.

    ``SELECT 1`` rather than checking a driver flag: a connection whose TCP peer
    has gone away still looks open until something is written to it, so the flag
    reports the last known state and this reports the current one.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()


__all__ = ["DOWN", "READINESS_TTL_SECONDS", "UP", "Probe", "Readiness", "build_router", "ping"]
