"""Schema catalog: introspection, serialization, embedding, retrieval.

The pipeline, in order:

    PostgresIntrospector  ->  SchemaSnapshot   read pg_catalog as the RO role
    serialization         ->  str              the text a vector is built from
    Embedder              ->  list[float]      a port; model chosen at runtime
    SchemaIndexer         ->  agent_meta       one transaction, as the owner
    SchemaRetriever       ->  RetrievalResult  ANN over what the indexer wrote

Each stage is separately testable: serialization is pure, the embedder has a
dependency-free implementation, and introspection needs only a connection.

The write path and the read path share one thing on purpose -- the ``Embedder``
instance. It supplies both the vectors and the ``model_version`` recorded
beside them, so an indexed corpus and the questions searched against it cannot
end up in different vector spaces.
"""
