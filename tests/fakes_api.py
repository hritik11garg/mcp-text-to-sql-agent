"""The API's dependency graph with every I/O boundary replaced.

Substituted through ``create_app(resource_factory=...)``. Without that seam the
assertions that use these would need a live PostgreSQL, which would put the
security suite behind a Docker daemon and make it the first thing to get
skipped.

Shared rather than copied. These started in ``tests/security/test_api_boundary``
and moved here when a second suite needed them, which is the point at which one
copy stops being simpler than one module: two copies of a fake drift, and the
one that drifts is the one whose test then passes for the wrong reason.

**A fake is a claim about the real thing, and the claims are here on purpose.**
:class:`FakeRetriever` records that ``dimensions`` was read, because on the real
retriever reading it is what opens the model checkpoint -- a side effect that
was missing from the lifespan for a whole slice and cost the first caller
twenty seconds. A fake that merely returned a number would have kept passing.
"""

from __future__ import annotations

from core.settings import Settings


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


class FakePool:
    """A pool that hands out one connection and counts the borrows.

    The count is what makes this more than a stub: a request that leaks a
    connection back into nothing is invisible until the pool is exhausted under
    load, which is the least convenient moment to discover it.
    """

    def __init__(self) -> None:
        self.conn = FakeConnection()
        self.borrowed = 0
        self.returned = 0
        self.closed = False

    def connection(self) -> FakePool:
        return self

    def __enter__(self) -> FakeConnection:
        self.borrowed += 1
        return self.conn

    def __exit__(self, *exc_info: object) -> None:
        self.returned += 1

    def close(self) -> None:
        self.closed = True


class FakeCatalog:
    tables = frozenset({"orders"})

    def resolve(self, *_: object, **__: object) -> None:
        return None


class FakeRetriever:
    model_version = "test-model"

    def __init__(self) -> None:
        self.model_opened = False

    @property
    def dimensions(self) -> int:
        """Stands in for the property whose *side effect* is the point.

        On the real retriever this reads the checkpoint. Recording the call is
        how a test can assert the load happened at startup without putting a
        90 MB download behind the suite.
        """
        self.model_opened = True
        return 384

    def search(self, *_: object, **__: object) -> object:
        raise AssertionError("no test using these fakes should reach retrieval")


class FakeResources:
    """The composed graph, built the way `create_app` expects to receive it."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.owner = FakeConnection()
        self.readonly = FakeConnection()
        self.readonly_pool = FakePool()
        self.catalog = FakeCatalog()
        self.retriever = FakeRetriever()
        self.closed = False

    def close(self) -> None:
        self.closed = True


__all__ = [
    "FakeCatalog",
    "FakeConnection",
    "FakePool",
    "FakeResources",
    "FakeRetriever",
]
