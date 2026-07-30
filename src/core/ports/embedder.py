"""The embedding port.

Deliberately synchronous. Embedding is CPU-bound local computation, not I/O --
an ``async def`` here would be a lie that lets callers believe they can await it
concurrently for free. Callers that must not block the event loop wrap it in
``asyncio.to_thread`` at the edge, which is honest about the cost.

See ADR-014 for the same reasoning applied to the LLM port.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors. Nothing more.

    ``model_version`` is part of the port rather than of configuration because
    the model that produced a vector is a property of the *model*, not of the
    operator's intent. Storing an operator-supplied version string would let it
    drift from reality, and vectors from different models are not comparable --
    a drifted label silently corrupts retrieval instead of failing.
    """

    @property
    def model_version(self) -> str:
        """Identifier recorded next to every vector this embedder produces.

        Queries filter on it. Two embedders that produce different vector
        spaces must never share a value.
        """
        ...

    @property
    def dimensions(self) -> int:
        """Length of every vector returned by :meth:`embed`.

        Checked against the database column before any write, because pgvector
        rejects a mismatched dimension per row -- discovering it mid-write
        leaves the catalog half updated.
        """
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, returning one L2-normalised vector per input.

        Normalisation is part of the contract: the catalog is indexed with
        ``vector_cosine_ops``, and mixing normalised and unnormalised vectors
        in one space makes distances incomparable.

        Returns vectors in input order, one per input, always.
        """
        ...
