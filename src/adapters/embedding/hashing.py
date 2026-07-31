"""A dependency-free embedder for tests and offline development.

This is feature hashing over character n-grams -- the "hashing trick" -- not a
semantic model. It knows nothing about meaning: ``purchase`` and ``order`` are
as unrelated to it as ``purchase`` and ``xylophone``.

It exists because the real embedder is a 90 MB download plus torch, and the
retrieval tests need something that:

* is deterministic across processes and machines, so a stored vector can be
  compared byte-for-byte between runs;
* has *some* lexical signal, so a test asserting "the query 'order totals'
  retrieves ``orders.total_amount``" is meaningful rather than coincidental;
* costs microseconds.

It is a real implementation of the port, not a mock. Swapping it for the
sentence-transformer adapter changes retrieval quality and nothing else --
which is the point of having a port at all.

Never configure this in production. ``model_version`` says so out loud, and
because vectors are filtered by that value, a catalog indexed with it cannot be
silently searched with a real model.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator, Sequence

DEFAULT_DIMENSIONS = 384
"""Matches the vector column width, so this adapter is a drop-in for the
baseline sentence-transformer model."""

NGRAM_SIZE = 3


class HashingEmbedder:
    """Deterministic character-trigram feature hashing.

    Args:
        dimensions: Output width. Must match the catalog's vector column.
    """

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        self._dimensions = dimensions

    @property
    def model_version(self) -> str:
        return f"hashing-trigram-{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions

        for gram in _trigrams(text.casefold()):
            # blake2b rather than hash(): the builtin is salted per process by
            # PYTHONHASHSEED, which would make stored vectors unreproducible
            # across runs -- the one property this adapter exists to provide.
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            # The sign bit from an independent slice of the digest keeps
            # unrelated grams from only ever adding, which would push every
            # vector towards the same direction.
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign

        return _normalise(vector)


def _trigrams(text: str) -> Iterator[str]:
    """Character trigrams over a whitespace-padded string.

    Padding means short tokens still produce grams, and word boundaries carry
    signal of their own.
    """
    padded = f" {' '.join(text.split())} "
    if len(padded) < NGRAM_SIZE:
        yield padded
        return
    for start in range(len(padded) - NGRAM_SIZE + 1):
        yield padded[start : start + NGRAM_SIZE]


def _normalise(vector: list[float]) -> list[float]:
    """L2-normalise, per the port's contract.

    An all-zero vector is returned unchanged rather than divided by zero.

    Empty input does *not* produce one: the padded string ``"  "`` is still a
    gram. The only route to a zero vector is exact cancellation -- two grams
    landing on the same index with opposite signs -- which is unlikely but not
    impossible at 384 dimensions. It matters because cosine distance to a zero
    vector is undefined in pgvector, so every row would tie on NaN and the
    ranking would be whatever order the scan produced. ``SchemaRetriever``
    refuses such a query rather than ranking on it.
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]
