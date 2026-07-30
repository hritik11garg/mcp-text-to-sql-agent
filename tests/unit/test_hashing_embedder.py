"""The hashing embedder exists to make retrieval testable without a download.

Two properties are load-bearing and both are asserted here: determinism across
processes, and enough lexical signal that a retrieval assertion means
something.
"""

from __future__ import annotations

import math
import subprocess
import sys

import pytest

from adapters.embedding.hashing import HashingEmbedder
from core.ports.embedder import Embedder


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


class TestPortContract:
    def test_satisfies_the_port(self, embedder: HashingEmbedder) -> None:
        assert isinstance(embedder, Embedder)

    def test_one_vector_per_input_in_order(self, embedder: HashingEmbedder) -> None:
        vectors = embedder.embed(["alpha", "beta", "gamma"])
        assert len(vectors) == 3
        assert vectors[0] == embedder.embed(["alpha"])[0]

    def test_vectors_have_the_declared_width(self, embedder: HashingEmbedder) -> None:
        assert all(len(vector) == embedder.dimensions for vector in embedder.embed(["a", "bb"]))

    def test_vectors_are_l2_normalised(self, embedder: HashingEmbedder) -> None:
        # The catalog is indexed with vector_cosine_ops. Mixing normalised and
        # unnormalised vectors in one space makes distances incomparable.
        for vector in embedder.embed(["orders.total_amount (numeric)", "customers (table)"]):
            assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)

    def test_empty_batch_is_empty(self, embedder: HashingEmbedder) -> None:
        assert embedder.embed([]) == []

    def test_rejects_zero_width(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            HashingEmbedder(dimensions=0)


class TestDeterminism:
    def test_stable_within_a_process(self, embedder: HashingEmbedder) -> None:
        assert embedder.embed(["orders"]) == embedder.embed(["orders"])

    def test_stable_across_processes(self) -> None:
        """The reason blake2b is used instead of the builtin hash().

        PYTHONHASHSEED randomises str hashing per process, so a builtin-hash
        implementation would pass every in-process test and still produce
        vectors that no longer match what is stored in the database after a
        restart. A subprocess is the only way to catch that.
        """
        script = (
            "from adapters.embedding.hashing import HashingEmbedder;"
            "print(HashingEmbedder().embed(['orders.total_amount'])[0][:4])"
        )
        runs = {
            subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            for _ in range(2)
        }
        assert len(runs) == 1, f"vectors differ between processes: {runs}"


class TestLexicalSignal:
    def test_shared_substrings_score_higher_than_unrelated_text(
        self, embedder: HashingEmbedder
    ) -> None:
        query, related, unrelated = embedder.embed(
            ["order total", "orders.total_amount (numeric)", "customers.country (text)"]
        )
        assert cosine(query, related) > cosine(query, unrelated)

    def test_identical_text_scores_one(self, embedder: HashingEmbedder) -> None:
        a, b = embedder.embed(["orders", "orders"])
        assert math.isclose(cosine(a, b), 1.0, rel_tol=1e-9)

    def test_model_version_names_it_as_unusable_in_production(
        self, embedder: HashingEmbedder
    ) -> None:
        # Vectors are filtered by model_version, so a catalog indexed with this
        # adapter cannot be silently searched with a real model.
        assert embedder.model_version == "hashing-trigram-384"
