"""Selects and builds the configured embedder.

The only place that knows which embedders exist. Mirrors
:mod:`adapters.llm.factory` on purpose -- one construction pattern, learned
once.
"""

from __future__ import annotations

from core.ports.embedder import Embedder
from core.settings import EmbedderProvider, RetrievalSettings

from .hashing import HashingEmbedder
from .sentence_transformer import SentenceTransformerEmbedder


def build_embedder(settings: RetrievalSettings) -> Embedder:
    """Construct the adapter named by ``EMBEDDER_PROVIDER``."""
    match settings.embedder_provider:
        case EmbedderProvider.HASHING:
            return HashingEmbedder()

        case EmbedderProvider.SENTENCE_TRANSFORMER:
            return SentenceTransformerEmbedder(
                settings.retriever_model,
                local_files_only=settings.retriever_local_files_only,
            )
