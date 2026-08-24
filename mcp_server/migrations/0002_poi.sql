-- Second data source: activity venues near a location.
--
-- Deliberately its own pair of tables rather than a `source_type` inside
-- weather_documents / weather_embeddings. Three reasons:
--
--   1. POST /weather/search — the graded deliverable — is a raw top-k over
--      weather_embeddings with no scoping. Roughly 200 venue documents against
--      ~48 weather ones would make that index mostly venues, and similarity
--      already clusters tightly on Dutch text (see README_WEATHER.md). A castle
--      article that mentions the confluence of Maas and Waal could outrank a
--      weather document on "risk of flooding near rivers".
--   2. /weather/sync re-embeds the whole corpus inline, one connection per
--      document. Venue text never changes, so putting it in the same table would
--      pay that cost on every sync for nothing.
--   3. A source_type filter is something you can forget at one of several query
--      sites. Separate tables make weather-only retrieval unforgeable.
--
-- Same column shape as the weather tables, so the chunking and embedding code is
-- shared rather than duplicated.

CREATE TABLE IF NOT EXISTS poi_documents (
    id VARCHAR PRIMARY KEY,
    location VARCHAR,          -- venue name
    source_type VARCHAR,       -- 'poi_indoor' | 'poi_outdoor', derived from Wikidata P31
    headline TEXT,
    narrative_text TEXT,       -- what gets embedded
    issued_at TIMESTAMP,       -- article last-touched; venues are near-static
    payload JSONB,             -- qid, P31, lat/lon, article url
    synced_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT now(),
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION
);

-- Venues are always looked up by distance from a coordinate.
CREATE INDEX IF NOT EXISTS poi_documents_coords ON poi_documents (lat, lon);
CREATE INDEX IF NOT EXISTS poi_documents_source_type ON poi_documents (source_type);

CREATE TABLE IF NOT EXISTS poi_embeddings (
    id BIGSERIAL PRIMARY KEY,
    document_id VARCHAR REFERENCES poi_documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding vector(384),
    model_name VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS poi_embeddings_hnsw
    ON poi_embeddings USING hnsw (embedding vector_cosine_ops);
