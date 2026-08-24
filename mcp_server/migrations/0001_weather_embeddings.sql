-- Weather Intelligence homework — Part 2 schema.
--
-- weather_documents already exists (created ad-hoc, not previously migrated) and keeps
-- its own legacy `embedding` column used by the /weather/query hybrid chat endpoint.
-- This migration adds the spec-required weather_embeddings table: one row per chunk,
-- with a stable (document_id, chunk_index) key so re-embedding a document is a safe
-- delete-and-reinsert instead of an ever-growing table.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS weather_documents (
    id VARCHAR PRIMARY KEY,
    location VARCHAR,
    source_type VARCHAR,
    headline TEXT,
    narrative_text TEXT,
    issued_at TIMESTAMP,
    payload JSONB,
    synced_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT now(),
    embedding vector(384)
);

CREATE TABLE IF NOT EXISTS weather_embeddings (
    id BIGSERIAL PRIMARY KEY,
    document_id VARCHAR REFERENCES weather_documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding vector(384),
    model_name VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS weather_embeddings_hnsw
    ON weather_embeddings USING hnsw (embedding vector_cosine_ops);
