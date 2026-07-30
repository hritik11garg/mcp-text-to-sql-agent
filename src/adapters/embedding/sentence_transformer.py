"""The real schema linker: a sentence-transformers bi-encoder.

Loaded lazily. Importing ``sentence_transformers`` pulls in torch and costs
several seconds plus hundreds of megabytes of RSS, which every unit test would
otherwise pay for to run code that never embeds anything.

The fine-tuned checkpoint of Stage 5 is this same adapter pointed at a local
path. That is deliberate: baseline and fine-tuned differ in ``model_version``
and in nothing else, so the eval harness compares them as a clean ablation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from core.exceptions import ConfigurationError

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class SentenceTransformerEmbedder:
    """Bi-encoder embedder over the :class:`~core.ports.embedder.Embedder` port.

    There is no default model here on purpose. ``RETRIEVER_MODEL`` in
    :class:`~core.settings.RetrievalSettings` is the single source of truth for
    which model this project uses; a second default in this file could drift
    from it, and the symptom would be vectors labelled with one model and
    produced by another.

    Args:
        model_name: A hub identifier or a local checkpoint path.
        local_files_only: Refuse to reach the network. Set for air-gapped runs
            and for any environment where an unexpected download would be a
            surprise rather than a convenience.
    """

    def __init__(self, model_name: str, *, local_files_only: bool = False) -> None:
        if not model_name:
            raise ConfigurationError("RETRIEVER_MODEL must not be empty")
        self._model_name = model_name
        self._local_files_only = local_files_only
        self._model: SentenceTransformer | None = None

    @property
    def model_version(self) -> str:
        """The model identifier itself.

        Not a separately configured label: a version string that can disagree
        with the model that produced the vectors is worse than none, because
        retrieval keeps working and only the results get quietly wrong.
        """
        return self._model_name

    @property
    def dimensions(self) -> int:
        size = self._load().get_sentence_embedding_dimension()
        if size is None:  # pragma: no cover - only for models without a pooling head
            raise ConfigurationError(
                f"model {self._model_name!r} does not report an embedding dimension"
            )
        return int(size)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: Any = self._load().encode(
            list(texts),
            normalize_embeddings=True,  # the port's contract, and cosine needs it
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in row] for row in vectors]

    def _load(self) -> SentenceTransformer:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self._model_name,
                local_files_only=self._local_files_only,
            )
        return self._model
