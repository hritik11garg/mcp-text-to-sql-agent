"""Schema catalog: introspection, serialization, embedding, retrieval.

The pipeline, in order:

    PostgresIntrospector  ->  SchemaSnapshot   read pg_catalog as the RO role
    serialization         ->  str              the text a vector is built from
    Embedder              ->  list[float]      a port; model chosen at runtime
    SchemaIndexer         ->  agent_meta       one transaction, as the owner

Each stage is separately testable: serialization is pure, the embedder has a
dependency-free implementation, and introspection needs only a connection.
Retrieval consumes what the indexer writes and lands in a later stage.
"""
