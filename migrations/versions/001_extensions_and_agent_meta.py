"""pgvector extension and the agent_meta schema

Two schemas with different jobs, kept separate deliberately:

  public      the target data users ask questions about
  agent_meta  this project's own catalog, embeddings, sessions, and audit log

The read-only role (migration 002) is granted SELECT on public and *nothing*
on agent_meta, so generated SQL cannot read the audit trail that records it.

Revision ID: 001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 384
"""Fixed by DDL. A model with a different dimension is a migration, not a
config toggle -- see docs/architecture/DATABASE.md section 6."""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS agent_meta")

    # --- retrieval corpus --------------------------------------------------
    op.execute(f"""
        CREATE TABLE agent_meta.schema_elements (
            id             bigserial PRIMARY KEY,
            dataset        text        NOT NULL,
            element_type   text        NOT NULL,
            table_name     text        NOT NULL,
            column_name    text,
            data_type      text,
            comment        text,
            serialized     text        NOT NULL,
            embedding      vector({EMBEDDING_DIM}),
            model_version  text        NOT NULL,
            updated_at     timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT schema_elements_type_ck
                CHECK (element_type IN ('table', 'column')),

            -- Duplicate embeddings for one element under one model would skew
            -- retrieval scores.
            CONSTRAINT schema_elements_unique
                UNIQUE (dataset, table_name, column_name, model_version)
        )
    """)

    op.execute("""
        COMMENT ON COLUMN agent_meta.schema_elements.model_version IS
        'Vectors from different models are NOT comparable -- queries must filter on this.'
    """)

    # Join paths, returned alongside retrieval results. Retrieval that returns
    # two tables without the path between them leaves the model to invent it.
    op.execute("""
        CREATE TABLE agent_meta.foreign_keys (
            id            bigserial PRIMARY KEY,
            dataset       text NOT NULL,
            from_table    text NOT NULL,
            from_column   text NOT NULL,
            to_table      text NOT NULL,
            to_column     text NOT NULL,
            constraint_name text,

            CONSTRAINT foreign_keys_unique
                UNIQUE (dataset, from_table, from_column, to_table, to_column)
        )
    """)

    # --- session memory ----------------------------------------------------
    op.execute("""
        CREATE TABLE agent_meta.sessions (
            id          text PRIMARY KEY,
            created_at  timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE agent_meta.session_turns (
            id           bigserial PRIMARY KEY,
            session_id   text        NOT NULL
                REFERENCES agent_meta.sessions(id) ON DELETE CASCADE,
            question     text        NOT NULL,
            generated_sql text,
            row_count    integer,
            truncated    boolean,
            created_at   timestamptz NOT NULL DEFAULT now()
        )
    """)

    # --- audit -------------------------------------------------------------
    # Deliberately NOT foreign-keyed to sessions: the audit trail must survive
    # session deletion. Result *values* are never stored here -- logging them
    # would copy the protected data into a second store.
    op.execute("""
        CREATE TABLE agent_meta.query_audit (
            id                bigserial PRIMARY KEY,
            created_at        timestamptz NOT NULL DEFAULT now(),
            request_id        text,
            trace_id          text,
            session_id        text,
            question          text,
            generated_sql     text,
            validation_attempts integer,
            db_role           text,
            duration_ms       integer,
            row_count         integer,
            truncated         boolean,
            outcome           text NOT NULL,
            error_type        text
        )
    """)

    # --- indexes -----------------------------------------------------------
    # Filter columns first: mixing vector spaces silently corrupts retrieval,
    # so every ANN query is pre-filtered by (dataset, model_version).
    op.execute("""
        CREATE INDEX schema_elements_dataset_model_ix
            ON agent_meta.schema_elements (dataset, model_version)
    """)
    op.execute("""
        CREATE INDEX schema_elements_dataset_table_ix
            ON agent_meta.schema_elements (dataset, table_name)
    """)

    # HNSW over IVFFlat: better recall at a given latency and no training step,
    # which matters because IVFFlat needs a populated table before its lists
    # can be built. See ADR-011.
    op.execute("""
        CREATE INDEX schema_elements_embedding_ix
            ON agent_meta.schema_elements
            USING hnsw (embedding vector_cosine_ops)
    """)

    op.execute("""
        CREATE INDEX foreign_keys_from_ix
            ON agent_meta.foreign_keys (dataset, from_table)
    """)
    op.execute("""
        CREATE INDEX session_turns_session_ix
            ON agent_meta.session_turns (session_id, created_at)
    """)
    op.execute("""
        CREATE INDEX query_audit_created_ix ON agent_meta.query_audit (created_at)
    """)
    op.execute("""
        CREATE INDEX query_audit_request_ix ON agent_meta.query_audit (request_id)
    """)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS agent_meta CASCADE")
    # The extension is left in place: other schemas may depend on it, and
    # dropping it is not this migration's business.
