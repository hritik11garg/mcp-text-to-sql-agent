"""Retrieval over the wire: the MCP servers, driven from a synchronous harness.

Every accuracy number this project publishes was measured on the **direct**
answering path -- components called in process, the same path the HTTP API
uses. The four MCP servers are proven to work by ``tests/contract/`` and proven
to answer *as well* by nothing. This module is the missing half: it makes
``search_schema`` reachable from the eval harness so the claim can be measured
rather than argued.

Three constraints shape everything here, and none of them is incidental.

**The published tool contract is single-dataset.** ``search_schema`` takes a
query, a ``k`` and a table filter -- and no dataset. The server's retriever is
bound to ``DATASET`` at construction, which is correct for the deployment the
contract describes (one agent, one database) and is exactly wrong for a
benchmark of twenty. Adding a ``dataset`` argument would measure a contract
that did not exist when the claim was made, and would hand a caller the ability
to read a catalog it was not scoped to. So the scoping stays where the contract
puts it -- in the process -- and a database change means a **new server
process**. :class:`McpClientPool` is what keeps that bounded.

**The transport must be opened and closed by the same task.** ``ToolRegistry``
says so in as many words: its stdio transport is built on anyio task groups
whose cancel scopes cannot be exited by a different task. ``run_until_complete``
per call creates a new task each time, so the obvious bridge fails at teardown,
after every call has already succeeded. Instead one thread runs one coroutine
for the client's whole life: it opens the registry, serves calls off a queue,
and closes the registry -- all in a single task.

**The harness is synchronous.** :class:`~evals.pipeline.PipelineAnswerer` calls
``retrieve`` inline, off the event loop it holds for generation. Marshalling
through a dedicated thread keeps those two loops from ever meeting, which
matters because the LLM client caches a connection pool bound to the first loop
it ran in.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from agent.discovery import ToolRegistry, ToolResult
from core.exceptions import RetrievalError
from schema.models import ForeignKey
from schema.retrieval import RetrievalResult, RetrievedElement

logger = logging.getLogger(__name__)

SCHEMA_SEARCH_SERVER = "mcp_servers.schema_search"
"""The only server this baseline needs.

Launching all four would spend three subprocesses and three database
connections per database on capabilities nothing here calls. The baseline
measures the retrieval hop and says so; a validation-over-MCP baseline is a
separate measurement with a separate row.
"""

SEARCH_TOOL = "search_schema"

DEFAULT_START_TIMEOUT_SECONDS = 300.0
"""How long a server gets to become answerable.

Generous on purpose. ``schema_search`` reads ``retriever.dimensions`` inside
``build()``, which is the property whose side effect is loading a
sentence-transformer, so the first start on a cold model cache pays a download.
A benchmark that gave up after thirty seconds would report "server unavailable"
for what is a slow first run.
"""

DEFAULT_CALL_TIMEOUT_SECONDS = 120.0
"""How long one ``search_schema`` call gets before the run stops waiting.

Bounded because a wedged subprocess is indistinguishable from a slow one, and
only one of them finishes. A timeout here becomes a recorded retrieval failure
for that question rather than a hung run.
"""


def to_retrieval_result(payload: Mapping[str, Any]) -> RetrievalResult:
    """Rebuild a :class:`RetrievalResult` from what the tool put on the wire.

    **This is the lossy step, and what it loses is the point of the
    measurement.** ``schema_search.render`` publishes table, column, type,
    comment and score, plus the foreign-key edges. It deliberately omits
    ``serialized`` -- that string carries sampled row values and must not reach
    a model (SECURITY.md section 14.2.5) -- and it has never carried
    ``element_type`` or ``constraint_name``.

    Neither omission changes a prompt or a metric, and that is asserted rather
    than assumed: ``generation.prompts.render_context`` reads table, column,
    ``data_type`` and comment; ``answering.retrieved_columns`` reads table and
    column. ``tests/unit/test_mcp_retrieval_source.py`` renders a result and its
    round trip and requires the two prompts to be byte-identical. If that test
    fails, the MCP baseline is measuring this function and not the servers.

    Raises:
        RetrievalError: the payload is not the shape the contract publishes.
            A subprocess is external input -- a malformed element silently
            dropped here would lower recall for a reason no artifact records.
    """
    raw_elements = payload.get("elements", [])
    raw_edges = payload.get("foreign_keys", [])
    if not isinstance(raw_elements, list) or not isinstance(raw_edges, list):
        raise RetrievalError(
            f"{SEARCH_TOOL} returned a payload with no element list; got keys {sorted(payload)!r}"
        )

    return RetrievalResult(
        elements=tuple(_element(item, index) for index, item in enumerate(raw_elements)),
        foreign_keys=tuple(_edge(item, index) for index, item in enumerate(raw_edges)),
    )


def _element(item: object, index: int) -> RetrievedElement:
    if not isinstance(item, dict):
        kind = type(item).__name__
        raise RetrievalError(f"{SEARCH_TOOL} element {index} is a {kind}, not an object")

    table = item.get("table")
    if not isinstance(table, str) or not table:
        raise RetrievalError(f"{SEARCH_TOOL} element {index} names no table")

    column = item.get("column")
    if column is not None and not isinstance(column, str):
        raise RetrievalError(f"{SEARCH_TOOL} element {index} has a non-string column")

    return RetrievedElement(
        # Derived, because the wire does not carry it. The distinction the
        # field records -- a table-level match versus a column-level one -- is
        # exactly the presence of a column name, which the wire does carry.
        element_type="column" if column else "table",
        table=table,
        column=column,
        data_type=_optional_str(item.get("type")),
        comment=_optional_str(item.get("comment")),
        # Never on the wire and never needed here. Empty rather than
        # reconstructed: a plausible-looking string would invite a reader to
        # treat it as the text that was embedded, which it would not be.
        serialized="",
        score=_score(item.get("score")),
    )


def _edge(item: object, index: int) -> ForeignKey:
    if not isinstance(item, dict):
        raise RetrievalError(f"{SEARCH_TOOL} foreign key {index} is not an object")
    try:
        return ForeignKey(
            from_table=str(item["from_table"]),
            from_column=str(item["from_column"]),
            to_table=str(item["to_table"]),
            to_column=str(item["to_column"]),
            # Not published by the contract. Nothing renders it -- the prompt
            # shows `a.b -> c.d` -- so an empty name costs nothing a reader
            # would look for.
            constraint_name="",
        )
    except KeyError as exc:
        missing = exc.args[0]
        raise RetrievalError(f"{SEARCH_TOOL} foreign key {index} is missing {missing!r}") from None


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _score(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


SECRET_NAME_PATTERN = re.compile(r"(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.IGNORECASE)
"""Environment variables a retrieval server has no business receiving."""

ENV_KEPT_DESPITE_PATTERN = frozenset({"DATABASE_URL", "DATABASE_RO_URL"})
"""Secrets the servers genuinely need: both DSNs carry a password.

Named explicitly so that "this one is passed on purpose" is a decision in the
code rather than an accident of how the pattern happens to be written."""


def scrubbed_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Inherit the environment, minus the secrets these servers do not use.

    **The finding this exists for.** ``schema_search`` embeds a question and
    queries pgvector. It never calls a model, and it was nonetheless being
    handed ``LLM_API_KEY`` -- because a subprocess inherits everything by
    default and nobody had asked whether it should. That is a standing
    least-privilege gap rather than a live vulnerability: it costs nothing
    until something in that process is compromised, at which point the key is
    one ``os.environ`` read away, and torch, transformers and
    sentence-transformers are a large surface to be confident about.
    A05:2021, confidentiality only. Severity Low, and worth fixing precisely
    because the fix is six lines.

    **A denylist, and the tradeoff is stated rather than hidden.** An allowlist
    is the stronger control and was rejected on robustness: these subprocesses
    need ``PATH``, ``SystemRoot``, the Hugging Face cache location, a TLS
    bundle and any proxy configuration, and an allowlist that omits one of
    those fails the run with an error naming none of them. The cost is that a
    secret in a variable this pattern does not match is still passed on. The
    mitigation is that the pattern covers the four shapes secrets are named in,
    and that the two variables deliberately kept are named above rather than
    matched by accident.

    Not applied to what a *host* launches -- Claude Desktop inherits its own
    environment and this function is not in that path. It scopes the benchmark,
    which is the caller this repository controls.
    """
    return {
        name: value
        for name, value in source.items()
        if name in ENV_KEPT_DESPITE_PATTERN or not SECRET_NAME_PATTERN.search(name)
    }


@dataclass(slots=True)
class _Call:
    """One pending ``tools/call``, and the future the caller is blocked on."""

    arguments: dict[str, Any]
    future: concurrent.futures.Future[ToolResult]


class McpSchemaSearch:
    """One dataset's ``schema_search`` server, usable from synchronous code.

    Owns a thread, that thread owns an event loop, and that loop runs exactly
    one task for the object's lifetime -- see the module docstring for why the
    obvious alternatives do not work.

    Lazy and restartable. :meth:`search` starts the server if it is not running,
    which is what lets :class:`McpClientPool` close an idle one and have a later
    question bring it back rather than fail.

    Args:
        dataset: Catalog namespace, which is also the PostgreSQL schema name.
            Already validated as an identifier by ``benchmark.convert``.
        env: Base environment for the subprocess. Defaults to this process's.
        modules: Servers to launch. One, by default.
    """

    def __init__(
        self,
        dataset: str,
        *,
        env: Mapping[str, str] | None = None,
        modules: tuple[str, ...] = (SCHEMA_SEARCH_SERVER,),
        python: str | None = None,
        start_timeout: float = DEFAULT_START_TIMEOUT_SECONDS,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        if not dataset:
            raise ValueError("an MCP retrieval client must name the dataset it is scoped to")

        self._dataset = dataset
        self._modules = modules
        self._python = python
        self._start_timeout = start_timeout
        self._call_timeout = call_timeout
        self._env = {
            **scrubbed_environment(os.environ if env is None else env),
            "DATASET": dataset,
            "DB_TARGET_SCHEMA": dataset,
        }

        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._failure: Exception | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_Call | None] | None = None
        self.starts = 0
        """How many times the servers have been launched. A pool eviction
        metric: with questions grouped by database this equals the number of
        databases, and anything higher is thrashing that a run should see."""

    @property
    def dataset(self) -> str:
        return self._dataset

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        """Launch the servers and block until a tool call would be answered.

        Idempotent. Fails fast rather than degrading: ``ToolRegistry`` records
        an unstartable server and carries on, which is right for an agent
        losing one capability and wrong here -- a benchmark whose retriever is
        absent would answer every question with an empty context and publish
        the result as an accuracy figure.

        Raises:
            RetrievalError: the server did not become answerable in time, or
                started without advertising :data:`SEARCH_TOOL`.
        """
        if self._thread is not None:
            return

        self._arm()
        self.starts += 1
        logger.info(
            "mcp: starting %s for dataset=%s (start #%d)",
            ", ".join(self._modules),
            self._dataset,
            self.starts,
        )

        thread = threading.Thread(
            target=self._run,
            name=f"mcp-{self._dataset}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

        if not self._ready.wait(self._start_timeout):
            self._thread = None
            raise RetrievalError(
                f"the {SCHEMA_SEARCH_SERVER} server for dataset {self._dataset!r} did not "
                f"become answerable within {self._start_timeout:.0f}s. The first start loads "
                f"a sentence-transformer; a cold model cache downloads it."
            )
        failure = self._failure
        if failure is not None:
            self.close()
            raise failure

    def _arm(self) -> None:
        """Clear the last start's outcome, ready for a new one.

        A method rather than two lines inline, and the reason is a type-checker
        one worth stating: ``_failure`` is written by the serving thread and
        read here, so a checker that sees only this function's assignments
        concludes the read can only ever produce ``None`` and calls the failure
        branch dead. The write genuinely happens -- on another thread -- and
        putting the reset behind a call is how the analysis stops being wrong
        about it.
        """
        self._ready.clear()
        self._failure = None

    def search(self, question: str, *, k: int) -> RetrievalResult:
        """Call ``search_schema`` and rebuild the result.

        Raises:
            RetrievalError: the server is unreachable, took too long, or
                answered with a tool error. Every one of those is a retrieval
                failure for this question, which the harness records rather
                than raising through -- see ``PipelineAnswerer``.
        """
        self.start()

        future: concurrent.futures.Future[ToolResult] = concurrent.futures.Future()
        call = _Call(arguments={"query": question, "k": k}, future=future)

        loop, queue = self._loop, self._queue
        if loop is None or queue is None:  # pragma: no cover - start() guarantees both
            raise RetrievalError(f"the {self._dataset!r} retrieval client is not running")
        try:
            loop.call_soon_threadsafe(queue.put_nowait, call)
        except RuntimeError as exc:
            raise RetrievalError(
                f"the {self._dataset!r} retrieval client has stopped: {exc}"
            ) from None

        try:
            result = future.result(timeout=self._call_timeout)
        except concurrent.futures.TimeoutError:
            raise RetrievalError(
                f"{SEARCH_TOOL} did not answer within {self._call_timeout:.0f}s "
                f"for dataset {self._dataset!r}"
            ) from None

        if result.is_error:
            # The envelope's own words. `error_type` is what the contract says
            # a client dispatches on, so it leads -- a benchmark failure that
            # reads `invalid_arguments` is a bug here, and one that reads
            # `connection_failed` is a bug in the environment.
            raise RetrievalError(
                f"{SEARCH_TOOL} returned {result.error_type or 'an error'}: "
                f"{result.payload.get('message', '(no message)')}"
            )
        return to_retrieval_result(result.payload)

    def close(self) -> None:
        """Stop the servers. Idempotent, and safe to call after a failed start."""
        thread, loop, queue = self._thread, self._loop, self._queue
        self._thread = None
        if thread is None:
            return

        if loop is not None and queue is not None and not loop.is_closed():
            # The loop can stop between the check and the call, and a client
            # that raised while being shut down would mask the run's own error.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, None)

        thread.join(timeout=self._start_timeout)
        if thread.is_alive():  # pragma: no cover - a wedged subprocess
            logger.warning("mcp: the %s client did not stop; leaving it daemonised", self._dataset)
        self._loop = None
        self._queue = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- the thread ---------------------------------------------------------

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # reported to the caller rather than swallowed
            self._failure = RetrievalError(
                f"the {SCHEMA_SEARCH_SERVER} server for dataset {self._dataset!r} "
                f"failed to start: {type(exc).__name__}: {exc}"
            )
        finally:
            # Always, so a caller blocked in `start()` learns about a failure
            # rather than waiting out the whole timeout for it.
            self._ready.set()

    async def _serve(self) -> None:
        """Open the registry, serve calls, close the registry. **One task.**

        The loop body is the reason this class exists: entering and exiting the
        stdio transport happen either side of it, in the same task, with every
        ``tools/call`` in between.
        """
        self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue[_Call | None] = asyncio.Queue()
        self._queue = queue

        async with ToolRegistry(self._modules, python=self._python, env=self._env) as registry:
            if SEARCH_TOOL not in registry.tools:
                self._failure = RetrievalError(
                    f"{SCHEMA_SEARCH_SERVER} started but did not advertise {SEARCH_TOOL!r} "
                    f"for dataset {self._dataset!r}. Unavailable servers: "
                    f"{registry.unavailable or '(none)'}"
                )
                return

            logger.info("mcp: %s ready for dataset=%s", SEARCH_TOOL, self._dataset)
            self._ready.set()

            while True:
                call = await queue.get()
                if call is None:
                    return
                if call.future.set_running_or_notify_cancel():
                    await self._answer(registry, call)

    async def _answer(self, registry: ToolRegistry, call: _Call) -> None:
        try:
            call.future.set_result(await registry.call(SEARCH_TOOL, call.arguments))
        except Exception as exc:  # handed to the waiting caller
            # Broad on purpose. A transport error, a dead subprocess and a
            # protocol error all mean the same thing to the question waiting on
            # this future, and letting any of them escape would kill the server
            # loop and take every remaining question with it.
            call.future.set_exception(exc)


class McpClientPool:
    """Keeps at most ``max_live`` server trees running, keyed by dataset.

    A benchmark walks twenty databases and the contract scopes a server to one,
    so an unbounded cache is twenty Python processes each holding a
    sentence-transformer -- which is not a design tradeoff, it is an
    out-of-memory error. Least-recently-used eviction bounds it, and
    :meth:`McpSchemaSearch.start` being restartable is what makes eviction
    cheap enough to default to **one**.

    That default is only cheap because the split is ordered. Spider's dev file
    is grouped by database -- twenty contiguous runs for twenty databases -- so
    a pool of one starts each server exactly once. :attr:`starts` is published
    so a run over an unordered split shows the thrashing instead of hiding it.
    """

    def __init__(
        self,
        *,
        max_live: int = 1,
        factory: Callable[[str], McpSchemaSearch] | None = None,
    ) -> None:
        if max_live < 1:
            raise ValueError("a pool must keep at least one client live")
        self._max_live = max_live
        self._factory = factory or McpSchemaSearch
        self._clients: OrderedDict[str, McpSchemaSearch] = OrderedDict()
        self.starts = 0
        """Server launches across the run. Equals the database count on an
        ordered split, and exceeds it by exactly the amount a run is thrashing.

        Counted **here**, as launches happen, rather than summed from the
        clients at the end. Summing looks equivalent and is not: a client's own
        counter is cumulative, an evicted client that is later reacquired is
        added twice, and the total drifts upward the more a run thrashes --
        which is the case the number exists to report.
        """

    def search(self, dataset: str, question: str, *, k: int) -> RetrievalResult:
        return self.acquire(dataset).search(question, k=k)

    def acquire(self, dataset: str) -> McpSchemaSearch:
        """The running client for one dataset, evicting others down to the bound.

        Eviction happens **before** the newcomer starts, so the pool never
        holds ``max_live + 1`` server trees even momentarily -- which is the
        difference between a bound and an average, and it matters during a
        model load, when memory is tightest.
        """
        client = self._clients.pop(dataset, None)
        if client is None:
            client = self._factory(dataset)

        while len(self._clients) >= self._max_live:
            _, evicted = self._clients.popitem(last=False)
            logger.info("mcp: evicting the client for dataset=%s", evicted.dataset)
            evicted.close()

        if not client.running:
            client.start()
            self.starts += 1

        self._clients[dataset] = client
        return client

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "DEFAULT_START_TIMEOUT_SECONDS",
    "SCHEMA_SEARCH_SERVER",
    "SEARCH_TOOL",
    "SECRET_NAME_PATTERN",
    "McpClientPool",
    "McpSchemaSearch",
    "scrubbed_environment",
    "to_retrieval_result",
]
