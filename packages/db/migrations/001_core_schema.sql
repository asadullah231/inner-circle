-- M1.1 — core state tables for the durable backend skeleton.
--
-- Scope note: `assets` is NOT created here. That migration is owned by the
-- media/storage lane (see packages/storage/tests/fixtures/assets_schema.sql)
-- and is applied separately. This file creates the tables the workflow engine
-- owns: projects, jobs, beats, approvals, audit_events.
--
-- `beats` IS created here because the workflow engine owns beat state. The
-- storage lane only ALTERs it to add the asset foreign key, so 002 (below)
-- must run after the assets migration.
--
-- Idempotent: safe to re-run. gen_random_uuid() is core in Postgres 13+.

-- --- enums ------------------------------------------------------------------
DO $$ BEGIN
    CREATE TYPE job_state AS ENUM (
        'draft', 'planning', 'planned', 'retrieving', 'retrieved',
        'rendering', 'rendered', 'qa', 'review', 'complete', 'failed', 'cancelled'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE approval_gate AS ENUM ('g1_script', 'g2_storyboard', 'g3_final');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE approval_decision AS ENUM ('pending', 'approved', 'rejected');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- --- projects ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    brief       TEXT,
    language    TEXT NOT NULL DEFAULT 'en',
    format_w    INTEGER NOT NULL DEFAULT 1080,
    format_h    INTEGER NOT NULL DEFAULT 1920,
    fps         INTEGER NOT NULL DEFAULT 30,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_projects_name_not_blank CHECK (length(trim(name)) > 0)
);

-- --- jobs -------------------------------------------------------------------
-- One production run of a project. `state` is advanced only by the state
-- machine (M1.2); every transition writes an audit_event.
CREATE TABLE IF NOT EXISTS jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    state           job_state NOT NULL DEFAULT 'draft',
    video_spec      JSONB,
    idempotency_key TEXT,
    attempt         INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_jobs_idempotency_key UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

-- --- beats ------------------------------------------------------------------
-- The unit approval, retrieval and retry operate on (PRD 7.1).
-- asset_id is plain UUID here; the storage lane's migration adds the FK.
CREATE TABLE IF NOT EXISTS beats (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id         UUID REFERENCES jobs(id) ON DELETE CASCADE,
    beat_key       TEXT,
    seq            INTEGER,
    narration      TEXT,
    start_s        NUMERIC,
    end_s          NUMERIC,
    visual_intent  TEXT,
    search_queries JSONB,
    shot_type      TEXT,
    overlay        TEXT,
    confidence     NUMERIC,
    asset_id       UUID,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_beats_job_key UNIQUE (job_id, beat_key)
);
CREATE INDEX IF NOT EXISTS idx_beats_job ON beats(job_id);

-- --- approvals --------------------------------------------------------------
-- The three human gates (PRD): G1 script, G2 storyboard, G3 final.
CREATE TABLE IF NOT EXISTS approvals (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id      UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    gate        approval_gate NOT NULL,
    decision    approval_decision NOT NULL DEFAULT 'pending',
    decided_by  TEXT,
    decided_at  TIMESTAMPTZ,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_approvals_job_gate UNIQUE (job_id, gate)
);
CREATE INDEX IF NOT EXISTS idx_approvals_job ON approvals(job_id);

-- --- audit_events -----------------------------------------------------------
-- Append-only. Every state transition and every gate decision lands here.
-- Never UPDATE or DELETE a row in this table.
CREATE TABLE IF NOT EXISTS audit_events (
    id          BIGSERIAL PRIMARY KEY,
    job_id      UUID REFERENCES jobs(id) ON DELETE CASCADE,
    project_id  UUID REFERENCES projects(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    from_state  job_state,
    to_state    job_state,
    actor       TEXT,
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_events(job_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(event_type);

-- --- updated_at triggers ----------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_projects_updated BEFORE UPDATE ON projects
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_jobs_updated BEFORE UPDATE ON jobs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_beats_updated BEFORE UPDATE ON beats
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
