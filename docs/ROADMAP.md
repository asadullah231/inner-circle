# Inner Circle — Milestone Roadmap

Adapted from the dossier's 6-phase, 19-person, 9-week plan (`docs/reference/`) into sequential milestones sized for a solo/duo build. Each milestone ends in something that runs and can be demoed — not a partial layer. No calendar-week estimates are promised; size is relative (S / M / L) and we re-scope after each milestone's actual velocity.

**Process for every milestone:** PLAN → BUILD → TEST → REVIEW → COMMIT → PR → COMPLETE → NEXT. No milestone starts until the previous one's PR is merged to `develop`.

---

## M0 — Foundation & Decisions *(size: S)* — **this PR**

**Objective:** Freeze the architecture and get a real, version-controlled repo before any feature code.

**Deliverables:**
- Repo structure per `ARCHITECTURE.md`
- `PRD.md`, `ARCHITECTURE.md`, `ROADMAP.md`, `CONTRIBUTING.md`, `CHANGELOG.md`
- `VideoSpec` / `AssetRecord` schemas frozen (as Pydantic/TS types in `packages/contracts/`)
- Git strategy live: `main` (protected) / `develop` / `feature/*`, PR template, CI skeleton
- Source policy and provider shortlist documented (Pexels primary, Pixabay/Openverse/Wikimedia/Archive fallback)

**Dependencies:** none.

**Acceptance criteria:** repo exists on GitHub, `main` is protected, `develop` exists, PRD/architecture/roadmap are reviewable, a contributor (future you) can read `CONTRIBUTING.md` and know exactly how to open a PR.

**Testing requirements:** CI workflow runs (even if it just lints/type-checks an empty package) and is green on `develop`.

**Definition of Done:** this document, `PRD.md`, and `ARCHITECTURE.md` are merged to `main` via PR #1, tagged `v0.1.0-foundation`.

---

## M1 — Durable Backend Skeleton *(size: M)*

**Objective:** A project and a job can be created via API and survive a restart. No AI calls yet — this milestone proves the state machine, not the intelligence.

**Features:**
- PostgreSQL schema: `projects, jobs, beats, approvals, providers, costs, audit_events`
- API: project CRUD, job creation, job status
- Job state machine (`Draft → Planning → ... → Complete`) with idempotent, audit-logged transitions
- Redis + queue worker skeleton (accepts a job, marks it processed — no real work yet)
- Local-disk storage adapter (S3/MinIO interface defined, disk implementation for now)
- Basic auth (single-user token is fine for M1; full RBAC lands in M5 with the UI)

**Dependencies:** M0.

**Acceptance criteria:** `POST /projects` → `POST /jobs` → job visible in `Draft`, worker picks it up, transitions to a terminal state, restart the API/worker mid-job and the state is unchanged.

**Testing requirements:** unit tests for state transitions (illegal transitions rejected), integration test for create-project → create-job → worker-processes → restart-recovers.

**Definition of Done:** PR merged to `develop`, CI green, demo: curl/Postman walkthrough recorded in the PR description.

---

## M2 — AI Gateway & Planner *(size: M)*

**Objective:** A brief or script becomes a validated `VideoSpec` with beats, using a provider-neutral, admin-configurable model layer.

**Features:**
- `ai_providers, model_routes, model_aliases, provider_health, provider_usage, provider_costs, prompt_versions` tables
- Alias-based routing (`planner_fast` etc.) resolved from DB, not hard-coded
- One frontier-model adapter wired end-to-end (provider TBD — see Q&B-3)
- Planner: brief/script → structured `VideoSpec` JSON, schema-validated, retried on malformed output
- Cost estimation recorded per planner call

**Dependencies:** M1 (jobs to attach plans to).

**Acceptance criteria:** given a brief, the system produces a schema-valid `VideoSpec` with beats, persisted against the job; changing the `planner_fast` mapping in the DB changes which model is used on the *next* job with no code change or redeploy.

**Testing requirements:** schema-validity tests against a fixed eval set of sample briefs; provider-mock tests for retry-on-malformed-JSON; a live smoke test against the real provider (manual, documented, not in CI).

**Definition of Done:** PR merged, `docs/runbooks/ai-gateway.md` explains how to add a new provider/alias.

---

## M3 — Media Retrieval Worker *(size: M)*

**Objective:** Given a beat's visual intent, retrieve licensed candidate assets safely and traceably. *(This is the "Scraping and Media Automation Workers" lane from the original dossier assignment — reuses the throttle/retry/dedup/provider-adapter prototype already drafted.)*

**Features:**
- `AssetProvider` interface + Pexels adapter (primary) + one fallback (Pixabay or Openverse)
- Per-provider throttling (token bucket) and retry-with-backoff (circuit breaker on repeated failure)
- Two-layer dedup: by `(provider, provider_asset_id)` before download, by SHA-256 file hash after
- Full `AssetRecord` metadata + rights fields persisted for every downloaded asset
- Internal asset library ingestion stub (catalog entry per asset, semantic search deferred to M6)

**Dependencies:** M2 (beats with `visual_intent` and `search_queries` to search for).

**Acceptance criteria:** given a beat, the worker returns ranked candidates from all enabled providers, zero duplicate downloads across repeated runs, every downloaded file has a sidecar rights/metadata record.

**Testing requirements:** provider-mock unit tests (no real API calls in CI), throttle/circuit-breaker unit tests, dedup unit tests, one manual live-provider smoke test documented in the PR.

**Definition of Done:** PR merged; `docs/runbooks/media-retrieval.md` documents rate limits and how to add a provider.

---

## M4 — Audio, Captions & Rendering *(size: L)*

**Objective:** Produce one complete, deterministic MP4 from a `VideoSpec` + resolved assets + narration.

**Features:**
- TTS adapter + WhisperX alignment (audio waveform is the timing authority, per PRD FR-8)
- Caption sidecar SRT/VTT generation
- Render adapter — **needs a decision before this milestone starts: Remotion (pending license review) vs. FFmpeg-only first** (see Q&B-1)
- Loudness normalization, silence/clipping detection

**Dependencies:** M2 (VideoSpec), M3 (assets).

**Acceptance criteria:** one script → one rendered MP4 with correctly-timed captions, reproducible byte-for-byte (or frame-for-frame) from the same manifest.

**Testing requirements:** golden-video smoke test (fixed input → expected duration/dimensions/stream validity via `ffprobe`), caption-timing test against known word timestamps.

**Definition of Done:** PR merged; a real MP4 from a real brief is attached to the PR as evidence.

---

## M5 — Quality Gates & Minimal Dashboard *(size: L)*

**Objective:** The three human approval gates are real UI, not a manual DB edit. This is the first milestone with a frontend.

**Features:**
- Next.js dashboard: project list/create, storyboard/contact-sheet cards (narration, timestamps, asset, alternatives, source/license, confidence, accept/replace/regenerate)
- G1/G2/G3 approval controls wired to the state machine
- Pre-render validation gate, post-render QA gate (`ffprobe` + frame sampling + audio + caption bounds)
- Per-beat independent retry surfaced in the UI
- Basic RBAC (Admin / Producer / Reviewer / Viewer, per PRD §10)

**Dependencies:** M1–M4 (there must be a real pipeline to put a UI in front of).

**Acceptance criteria:** a producer completes brief → approved script → approved contact sheet → rendered → QA-passed → final-approved video entirely through the UI, no direct API calls.

**Testing requirements:** e2e test of the full approval flow (Playwright or similar); a failed beat is retried from the UI without restarting the job.

**Definition of Done:** PR merged; recorded screen-capture demo linked in the PR.

---

## M6 — Team Production Features *(size: M)*

**Objective:** Make repeated, reproducible production actually usable day-to-day.

**Features:** brand profiles + motion packs, internal asset library semantic search, batch generation (CSV/JSON), cost dashboard + per-project spend limits, 9:16/16:9/1:1 output profiles.

**Dependencies:** M5.

**Acceptance criteria:** two projects can run in parallel; a prior render reproduces from its saved manifest; a spend limit actually blocks a job that would exceed it.

**Testing requirements:** reproducibility test (re-render from manifest, diff output), spend-limit enforcement test.

**Definition of Done:** PR merged; cost dashboard screenshot in PR.

---

## M7 — Advanced Orchestration *(size: L, stretch)*

**Objective:** The system explains and improves its own provider choices.

**Features:** provider scoring/evaluation engine using accumulated production results, optional AI-generated-clip fallback (behind `VideoProvider`, same interface as everything else), thumbnail generation, optional web-research ledger.

**Dependencies:** M6 and enough production history to have evaluation data.

**Acceptance criteria:** system can show, for a given job, why each provider/asset/model was chosen, with cost estimated before execution.

**Definition of Done:** PR merged; not started until M6 is stable in real use — do not pull this forward to "look advanced."

---

## Deferred indefinitely (Future Scope — see `PRD.md` §14)

Avatar/host video · long-form 10–30 min production · multi-language dubbing · transcript-based timeline editor · platform publishing integrations · local GPU generation · collaborative simultaneous editing.
