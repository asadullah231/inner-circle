-- Test-only bootstrap for the storage DB integration tests.
--
-- The real `assets` migration is Mubashir Nadeem's, in the OTHER repo
-- (mubashirnadeemgm-lgtm/video-platform, apps/api/migration_assets.sql). It is
-- reproduced here verbatim so CI and local runs can stand up a schema without
-- access to that repo. Keep it in sync if his migration changes.
--
-- GOTCHA (encoded here on purpose): his migration ALTERs a `beats` table that an
-- EARLIER migration of his creates — it does not create `beats` itself. We do
-- not own `beats`, so this file first creates a MINIMAL stand-in (id + asset_id)
-- purely so the foreign key and list_in_use_asset_keys()'s JOIN work. This is
-- NOT the real beats schema; the real one is Mubashir's.
--
-- gen_random_uuid() is core in Postgres 13+ (CI uses postgres:16), so no
-- pgcrypto extension is needed.

-- Minimal test stand-in for the table we do not own.
CREATE TABLE beats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_id UUID
);

-- --- Mubashir's assets migration, reproduced verbatim ---------------------

CREATE TABLE assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,
    provider_asset_id TEXT,
    source_url TEXT,
    local_uri TEXT,
    media_type TEXT,
    width INTEGER,
    height INTEGER,
    duration_s NUMERIC,
    license TEXT,
    attribution TEXT,
    allowed_use TEXT,
    downloaded_at TIMESTAMPTZ,
    file_hash TEXT NOT NULL,
    embedding_uri TEXT,
    quality_score NUMERIC,
    storage_key TEXT,
    size_bytes BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_assets_file_hash UNIQUE (file_hash)
);

CREATE INDEX idx_assets_provider ON assets(provider);

ALTER TABLE beats
    ADD CONSTRAINT fk_beats_asset
    FOREIGN KEY (asset_id) REFERENCES assets(id)
    ON DELETE SET NULL;
