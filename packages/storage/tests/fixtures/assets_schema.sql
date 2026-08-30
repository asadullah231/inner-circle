-- Test-only bootstrap for the storage DB integration tests.
--
-- The real migrations are Mubashir Nadeem's, in the OTHER repo
-- (mubashirnadeemgm-lgtm/video-platform, apps/api/). They are reproduced here
-- so CI and local runs can stand up a schema without access to that repo.
-- Keep in sync if his migrations change.
--
-- GOTCHA (encoded here on purpose): his migration ALTERs a `beats` table that an
-- EARLIER migration of his creates — it does not create `beats` itself. We do
-- not own `beats` or `projects`, so this file creates MINIMAL stand-ins purely
-- so the foreign keys and list_in_use_asset_keys()'s JOIN work. These are NOT
-- the real schemas; the real ones are Mubashir's.
--
-- gen_random_uuid() is core in Postgres 13+ (CI uses postgres:16), so no
-- pgcrypto extension is needed.

-- Minimal test stand-in for tables we do not own.
CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid()
);

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

-- --- Mubashir's last_used_at migration (migration_last_used_at.sql) -------

ALTER TABLE assets
    ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_assets_last_used_at
    ON assets (last_used_at);

-- --- Mubashir's renders migration (migration_renders.sql) -----------------

CREATE TABLE IF NOT EXISTS renders (
    render_id      TEXT PRIMARY KEY,
    project_id     UUID NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
    mp4_key        TEXT NOT NULL,
    thumbnail_key  TEXT,
    captions_key   TEXT,
    manifest_key   TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_renders_project_id ON renders (project_id);
