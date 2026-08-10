"""The wire between the retriever and the prompt, without a server.

The MCP baseline exists to answer one question: does routing retrieval through
a subprocess change the answer? That question is only meaningful if the
*transport* is what differs. If the payload cannot carry what the prompt reads,
the baseline measures this repository's serialization code and reports it as a
fact about MCP.

So the property under test is not "the round trip preserves the object" -- it
does not, and deliberately: ``serialized`` carries sampled row values and is
excluded from the wire as a security control (SECURITY.md section 14.2.5). The
property is **the round trip preserves the prompt**, which is a weaker claim
and the only one that matters.

``render`` is imported from the server rather than copied, so a change to the
published payload shape fails here rather than silently changing what a
benchmark measures.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any

import pytest

from answering import retrieved_columns
from core.exceptions import RetrievalError
from evals.mcp_client import (
    SEARCH_TOOL,
    McpClientPool,
    McpSchemaSearch,
    scrubbed_environment,
    to_retrieval_result,
)
from generation.prompts import render_context
from mcp_servers.schema_search.server import render
from schema.models import ForeignKey
from schema.retrieval import RetrievalResult, RetrievedElement


def element(
    table: str,
    column: str | None,
    *,
    data_type: str | None = "text",
    comment: str | None = None,
    score: float = 0.9,
) -> RetrievedElement:
    return RetrievedElement(
        element_type="column" if column else "table",
        table=table,
        column=column,
        data_type=data_type,
        comment=comment,
        # Row values, which is exactly what the wire must not carry.
        serialized=f"{table}.{column} sample: Ada, Grace",
        score=score,
    )


@pytest.fixture
def result() -> RetrievalResult:
    """A result with every feature the prompt renders.

    Two tables so grouping is exercised, a table-level match so the
    ``column is None`` branch is, a comment on one column and not another, a
    missing data type, and an edge between the two tables.
    """
    return RetrievalResult(
        elements=(
            element("singer", None),
            element("singer", "name", comment="stage name"),
            element("singer", "age", data_type="integer"),
            element("concert", "singer_id", data_type=None),
        ),
        foreign_keys=(ForeignKey("concert", "singer_id", "singer", "id", "concert_singer_fk"),),
    )


class TestTheWirePreservesThePrompt:
    def test_the_round_trip_renders_a_byte_identical_prompt(self, result: RetrievalResult) -> None:
        """The property the whole baseline rests on.

        If this fails, a difference between `retrieval-only` and
        `mcp-retrieval` is attributable to serialization rather than to the
        protocol, and the row must not be published.
        """
        round_tripped = to_retrieval_result(render(result))

        assert render_context(round_tripped) == render_context(result)

    def test_the_round_trip_preserves_what_recall_is_computed_from(
        self, result: RetrievalResult
    ) -> None:
        """Recall@k is measured over the same pairs on both sides, or it is two metrics."""
        round_tripped = to_retrieval_result(render(result))

        assert retrieved_columns(round_tripped) == retrieved_columns(result)

    def test_rank_order_survives(self, result: RetrievalResult) -> None:
        """The prompt groups by table in first-seen order, so order is not cosmetic."""
        round_tripped = to_retrieval_result(render(result))

        assert [(e.table, e.column) for e in round_tripped.elements] == [
            (e.table, e.column) for e in result.elements
        ]

    def test_row_values_do_not_cross_the_wire(self, result: RetrievalResult) -> None:
        """The exclusion is a security control, and it is the reason the trip is lossy.

        Asserted here as well as in tests/security/ because *this* module is
        where someone would "fix" the lossiness by adding `serialized` back.
        """
        payload = render(result)

        assert "Ada, Grace" not in str(payload)
        assert to_retrieval_result(payload).elements[0].serialized == ""

    def test_a_table_level_match_stays_a_table_level_match(self, result: RetrievalResult) -> None:
        """``element_type`` is not on the wire; the column's absence carries it."""
        round_tripped = to_retrieval_result(render(result))

        assert round_tripped.elements[0].element_type == "table"
        assert round_tripped.elements[1].element_type == "column"

    def test_an_empty_result_round_trips(self) -> None:
        assert to_retrieval_result(render(RetrievalResult())) == RetrievalResult()


class TestThePayloadIsNotTrusted:
    """A subprocess is external input, and a dropped element is a silent recall loss."""

    def test_a_payload_with_no_element_list_is_refused(self) -> None:
        with pytest.raises(RetrievalError, match="no element list"):
            to_retrieval_result({"ok": True, "elements": "singer"})

    def test_an_element_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(RetrievalError, match="element 0 is a str"):
            to_retrieval_result({"elements": ["singer"]})

    def test_an_element_naming_no_table_is_refused(self) -> None:
        with pytest.raises(RetrievalError, match="element 1 names no table"):
            to_retrieval_result({"elements": [{"table": "singer"}, {"column": "name"}]})

    def test_a_non_string_column_is_refused(self) -> None:
        with pytest.raises(RetrievalError, match="non-string column"):
            to_retrieval_result({"elements": [{"table": "singer", "column": 7}]})

    def test_an_incomplete_foreign_key_is_refused(self) -> None:
        with pytest.raises(RetrievalError, match="missing 'to_column'"):
            to_retrieval_result(
                {
                    "elements": [],
                    "foreign_keys": [
                        {"from_table": "a", "from_column": "b", "to_table": "c"},
                    ],
                }
            )

    def test_an_unscorable_score_does_not_abort_the_question(self) -> None:
        """Score reaches no prompt and no metric, so a bad one is not worth failing over.

        The asymmetry is deliberate: a missing table changes what the model is
        shown, and a missing score changes nothing anyone reads.
        """
        rebuilt = to_retrieval_result({"elements": [{"table": "t", "column": "c", "score": "?"}]})

        assert rebuilt.elements[0].score == 0.0


# --- the pool --------------------------------------------------------------


class FakeClient:
    """A client that records its lifecycle instead of launching anything."""

    def __init__(self, dataset: str) -> None:
        self.dataset = dataset
        self.running = False
        self.starts = 0
        self.closes = 0
        self.searched: list[tuple[str, int]] = []

    def start(self) -> None:
        if not self.running:
            self.running = True
            self.starts += 1

    def search(self, question: str, *, k: int) -> RetrievalResult:
        # Not `self.start()`. The pool is what guarantees a live client, and a
        # double that started itself would let a pool that had stopped doing so
        # keep passing.
        assert self.running, "the pool handed out a client it had not started"
        self.searched.append((question, k))
        return RetrievalResult()

    def close(self) -> None:
        self.running = False
        self.closes += 1


class TestTheClientPoolIsBounded:
    """Twenty databases, twenty server trees, each holding a model. The bound is the design."""

    def test_one_database_starts_one_client(self) -> None:
        built: dict[str, FakeClient] = {}

        def factory(dataset: str) -> Any:
            built[dataset] = FakeClient(dataset)
            return built[dataset]

        pool = McpClientPool(factory=factory)
        for _ in range(3):
            pool.search("alpha", "who sings?", k=30)

        assert list(built) == ["alpha"]
        assert built["alpha"].starts == 1

    def test_moving_to_a_new_database_closes_the_old_one(self) -> None:
        built: dict[str, FakeClient] = {}

        def factory(dataset: str) -> Any:
            built[dataset] = FakeClient(dataset)
            return built[dataset]

        pool = McpClientPool(factory=factory)
        pool.search("alpha", "q", k=10)
        pool.search("beta", "q", k=10)

        assert built["alpha"].closes == 1
        assert built["beta"].closes == 0

    def test_the_bound_is_never_exceeded_even_momentarily(self) -> None:
        """Eviction happens before the newcomer starts.

        A pool that started first and evicted after would hold ``max_live + 1``
        server trees for the length of a model load, which is the moment memory
        is tightest -- so the bound would be an average rather than a bound.
        """
        live: set[str] = set()

        class Tracking(FakeClient):
            def start(self) -> None:
                super().start()
                live.add(self.dataset)
                assert len(live) <= 1, f"{len(live)} clients live at once"

            def close(self) -> None:
                super().close()
                live.discard(self.dataset)

        pool = McpClientPool(factory=Tracking)
        for dataset in ("alpha", "beta", "gamma"):
            pool.search(dataset, "q", k=10)

        assert len(live) == 1

    def test_returning_to_a_database_restarts_its_client(self) -> None:
        """An unordered split thrashes, and the count is what makes that visible."""
        built: dict[str, FakeClient] = {}

        def factory(dataset: str) -> Any:
            return built.setdefault(dataset, FakeClient(dataset))

        pool = McpClientPool(factory=factory)
        for dataset in ("alpha", "beta", "alpha", "beta"):
            pool.search(dataset, "q", k=10)
        pool.close()

        assert pool.starts == 4

    def test_a_larger_bound_keeps_both_alive(self) -> None:
        built: dict[str, FakeClient] = {}

        def factory(dataset: str) -> Any:
            return built.setdefault(dataset, FakeClient(dataset))

        pool = McpClientPool(max_live=2, factory=factory)
        for dataset in ("alpha", "beta", "alpha", "beta"):
            pool.search(dataset, "q", k=10)

        assert pool.starts == 2

    def test_closing_the_pool_closes_every_client_and_keeps_the_count(self) -> None:
        built: dict[str, FakeClient] = {}

        def factory(dataset: str) -> Any:
            return built.setdefault(dataset, FakeClient(dataset))

        pool = McpClientPool(max_live=2, factory=factory)
        pool.search("alpha", "q", k=10)
        pool.search("beta", "q", k=10)
        pool.close()

        assert [c.closes for c in built.values()] == [1, 1]
        assert pool.starts == 2

    def test_a_pool_that_keeps_nothing_live_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one client live"):
            McpClientPool(max_live=0)

    def test_k_reaches_the_client_unchanged(self) -> None:
        """The one number that must match the baseline it is compared against."""
        built: dict[str, FakeClient] = {}

        def factory(dataset: str) -> Any:
            return built.setdefault(dataset, FakeClient(dataset))

        McpClientPool(factory=factory).search("alpha", "who sings?", k=30)

        assert built["alpha"].searched == [("who sings?", 30)]


class TestTheServersDoNotInheritSecretsTheyDoNotUse:
    """Least privilege on a process environment. A05:2021, confidentiality.

    ``schema_search`` embeds a question and queries pgvector. It never calls a
    model, and it was being handed ``LLM_API_KEY`` because a subprocess
    inherits everything unless somebody asks whether it should.
    """

    def test_the_model_providers_key_is_not_passed_on(self) -> None:
        scrubbed = scrubbed_environment({"LLM_API_KEY": "secret", "PATH": "/usr/bin"})

        assert "LLM_API_KEY" not in scrubbed
        assert scrubbed["PATH"] == "/usr/bin"

    @pytest.mark.parametrize(
        "name",
        ["GROQ_API_KEY", "HF_TOKEN", "AWS_SECRET_ACCESS_KEY", "PGPASSWORD", "GH_CREDENTIALS"],
    )
    def test_the_pattern_covers_the_shapes_secrets_are_named_in(self, name: str) -> None:
        assert name not in scrubbed_environment({name: "secret"})

    def test_the_matching_is_case_insensitive(self) -> None:
        """A developer's shell holds lower-case names too."""
        assert "openai_api_key" not in scrubbed_environment({"openai_api_key": "secret"})

    def test_both_database_urls_survive(self) -> None:
        """The one secret these servers genuinely need, kept by name.

        Both DSNs carry a password, so the pattern would take them. Naming them
        makes "passed on purpose" a decision in the code rather than a property
        of how the regex happens to be written.
        """
        scrubbed = scrubbed_environment(
            {
                "DATABASE_URL": "postgresql://owner:pw@localhost/db",
                "DATABASE_RO_URL": "postgresql://ro:pw@localhost/db",
            }
        )

        assert set(scrubbed) == {"DATABASE_URL", "DATABASE_RO_URL"}

    def test_ordinary_configuration_is_untouched(self) -> None:
        """The reason this is a denylist and not an allowlist.

        These subprocesses need a model cache, a TLS bundle, proxy settings and
        on Windows a ``SystemRoot``. An allowlist that omitted one would fail a
        run with an error naming none of them.
        """
        environment = {
            "PATH": "/usr/bin",
            "HF_HOME": "/cache/hf",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca.pem",
            "HTTPS_PROXY": "http://proxy:3128",
            "SystemRoot": r"C:\Windows",
            "RETRIEVAL_TOP_K": "30",
        }

        assert scrubbed_environment(environment) == environment

    def test_a_real_client_does_not_carry_the_key(self) -> None:
        """The scrubber wired up, not merely present."""
        client = McpSchemaSearch("alpha", env={"LLM_API_KEY": "secret", "PATH": "/usr/bin"})

        assert "LLM_API_KEY" not in client._env
        assert client._env["DATASET"] == "alpha"


class TestTheClientScopesItsServers:
    def test_the_dataset_reaches_the_subprocess_environment(self) -> None:
        """The contract has no dataset argument, so the scope travels as config.

        This is the whole reason a database change costs a process. Asserting
        it here means a future `dataset` parameter on the tool cannot quietly
        make the process-per-database cost look unnecessary while the
        environment still decides.
        """
        client = McpSchemaSearch("spider_concert_singer", env={"PATH": "/usr/bin"})

        assert client._env["DATASET"] == "spider_concert_singer"
        assert client._env["DB_TARGET_SCHEMA"] == "spider_concert_singer"
        assert client._env["PATH"] == "/usr/bin"

    def test_a_client_with_no_dataset_is_refused(self) -> None:
        """Twenty databases and no scope is not a default anything can pick."""
        with pytest.raises(ValueError, match="must name the dataset"):
            McpSchemaSearch("")

    def test_a_server_that_never_becomes_answerable_fails_the_run(self) -> None:
        """Fail fast, loudly, rather than answer every question with nothing.

        ``ToolRegistry`` records an unstartable server and carries on, which is
        the right degradation for an agent losing one capability. Here it would
        mean publishing an accuracy figure for a retriever that never ran.
        """
        client = McpSchemaSearch(
            "alpha",
            modules=("evals.no_such_server_module",),
            start_timeout=30.0,
        )

        with pytest.raises(RetrievalError, match="did not advertise 'search_schema'"):
            client.start()
        assert not client.running

    def test_closing_an_unstarted_client_is_a_no_op(self) -> None:
        McpSchemaSearch("alpha").close()

    def test_a_call_that_never_answers_becomes_a_retrieval_failure(self) -> None:
        """A wedged subprocess must cost one question, not the run."""
        client = McpSchemaSearch("alpha", call_timeout=0.05)
        client._thread = object()  # type: ignore[assignment]  # pretend it is running
        client._loop = _NullLoop()  # type: ignore[assignment]
        client._queue = _NullQueue()  # type: ignore[assignment]

        with pytest.raises(RetrievalError, match=f"{SEARCH_TOOL} did not answer within"):
            client.search("who sings?", k=30)


class _NullLoop:
    """A loop that accepts work and never runs it."""

    def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
        return None

    def is_closed(self) -> bool:
        return False


class _NullQueue:
    def put_nowait(self, item: Any) -> None:  # pragma: no cover - never reached
        return None


def test_a_future_abandoned_by_a_timeout_can_still_be_completed() -> None:
    """The server thread sets a result on a future nobody is waiting for.

    Worth pinning because the alternative -- the thread raising
    ``InvalidStateError`` on a cancelled future -- would kill the serving loop
    and take every remaining question with it.
    """
    future: concurrent.futures.Future[int] = concurrent.futures.Future()

    assert future.set_running_or_notify_cancel() is True
    future.set_result(1)
