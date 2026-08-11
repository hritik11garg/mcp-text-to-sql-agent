"""The retriever recorded on a run is the one the run will actually read.

``--retriever`` used to *supply* ``retriever_model_version``, defaulting to the
empty string. So the field sat inside ``config_fingerprint`` -- the guard that
refuses to resume a run whose retriever changed -- carrying a value nobody had
to provide.

The consequence is a Stage 5 one, which is why it is worth fixing before Stage 5
rather than during it. Every run made so far records ``""``. A fine-tuned
retriever arrives, its run also records ``""`` unless an operator remembers a
flag, and the two resume into each other: one results directory, half its
questions answered under a baseline vector space and half under a fine-tuned
one, with nothing downstream able to show it. **The guard would first be needed
at the exact moment it was first useless.**

It is now derived from the embedder, which is what ``SchemaRetriever`` binds
into its ``(dataset, model_version)`` predicate -- so it is the vector space the
run reads, by construction rather than by an operator's memory.
"""

from __future__ import annotations

import pytest

from adapters.embedding.hashing import HashingEmbedder
from core.exceptions import ConfigurationError
from core.settings import EmbedderProvider, RetrievalSettings
from evals.run import _retriever_version

pytestmark = pytest.mark.unit

# Imported rather than spelled. A test that hardcodes the adapter's version
# string keeps passing after the string changes, while checking nothing -- the
# same defect the security suite hit with `<unnamed>`.
HASHING_VERSION = HashingEmbedder().model_version


def _settings(**overrides: object) -> RetrievalSettings:
    """Real retrieval settings; nothing here opens a connection or a checkpoint.

    The helper takes ``RetrievalSettings`` rather than the whole ``Settings``
    because that is all it needs -- and a function that accepts the whole object
    is one whose tests have to build a database configuration to ask a question
    about an embedder.
    """
    return RetrievalSettings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestItIsDerivedRatherThanSupplied:
    def test_the_hashing_embedder_names_itself(self) -> None:
        settings = _settings(embedder_provider=EmbedderProvider.HASHING)
        assert _retriever_version("", settings) == HASHING_VERSION

    def test_the_sentence_transformer_reports_its_configured_model(self) -> None:
        """And does it without opening a checkpoint.

        If deriving the version cost a model load, it could not run before the
        manifest is written -- which is where it has to run, because the
        manifest is what the resume guard reads.
        """
        settings = _settings(
            embedder_provider=EmbedderProvider.SENTENCE_TRANSFORMER,
            retriever_model="sentence-transformers/all-MiniLM-L6-v2",
        )
        assert _retriever_version("", settings) == "sentence-transformers/all-MiniLM-L6-v2"

    def test_it_is_never_the_empty_string(self) -> None:
        """The defect stated as a property.

        An empty value is what made the fingerprint inert, so no configuration
        may produce one.
        """
        for provider in EmbedderProvider:
            settings = _settings(embedder_provider=provider)
            assert _retriever_version("", settings)

    def test_two_retrievers_produce_two_fingerprints(self) -> None:
        """The Stage 5 case, asserted before Stage 5 can rely on it."""
        baseline = _retriever_version("", _settings(embedder_provider=EmbedderProvider.HASHING))
        fine_tuned = _retriever_version(
            "",
            _settings(
                embedder_provider=EmbedderProvider.SENTENCE_TRANSFORMER,
                retriever_model="local/fine-tuned-v2",
            ),
        )
        assert baseline != fine_tuned


class TestTheFlagIsNowAnAssertion:
    def test_a_matching_value_is_accepted(self) -> None:
        settings = _settings(embedder_provider=EmbedderProvider.HASHING)
        assert _retriever_version(HASHING_VERSION, settings) == HASHING_VERSION

    def test_a_disagreeing_value_is_refused(self) -> None:
        """Fail fast, rather than recording whichever of the two was typed.

        A caller who states the retriever and is wrong has a misconfiguration.
        Recording their string would put a false provenance field on every
        artifact; recording the derived one silently would discard a signal that
        the run is not the run they think it is.
        """
        settings = _settings(embedder_provider=EmbedderProvider.HASHING)
        with pytest.raises(ConfigurationError, match=HASHING_VERSION):
            _retriever_version("local/fine-tuned-v2", settings)

    def test_the_refusal_names_both_values(self) -> None:
        """An error that names only one side leaves the reader guessing which."""
        settings = _settings(embedder_provider=EmbedderProvider.HASHING)
        with pytest.raises(ConfigurationError) as caught:
            _retriever_version("mistake", settings)

        message = str(caught.value)
        assert "mistake" in message
        assert HASHING_VERSION in message
        assert "EMBEDDER_PROVIDER" in message
