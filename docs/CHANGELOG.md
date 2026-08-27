# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `packages/db` — core schema migration (`projects, jobs, beats, approvals, audit_events`)
  and a forward-only migration runner (M1.1)
- CI job `core-db-integration` running the core schema suite against postgres:16
- `packages/db/states.py` — pure job state machine: 12 states, 3 approval gates,
  legal-transition map, terminal states, idempotent self-transitions (M1.2)
- `packages/db/transitions.py` — durable transitions: row-locked reads, audit trail
  on every move, gate decisions with reject-requires-note (M1.2)
- `packages/api` — FastAPI control plane: project CRUD, job create/status/history,
  gate decisions, operator transitions, bearer-token auth, `/health` (M1.3)
- `packages/worker` — job queue over a Redis list (`BLMOVE` into a processing list so a
  crashed worker's job is recoverable) and the worker loop that advances one job one
  step per pass, parking at gates instead of polling (M1.4)
- `packages/worker/tests/test_recovery_integration.py` — the M1 acceptance tests:
  create through the API, drive with a worker, kill it mid-job, restart with no state
  lost or replayed, and two workers racing one job (M1.5)
- CI now runs the M1 acceptance tests in the `core-db-integration` job
- `demo_m1_full.py` — the whole milestone end to end with no Postgres or Redis
- `docs/runbooks/api-and-worker.md` — how to run, operate and debug both processes

### Changed
- `docs/ROADMAP.md` — M1 split into sub-milestones M1.0 through M1.5; FastAPI locked
  as the API stack (resolves Q&B-2)
- `README.md` — status now reflects shipped code (storage layer, core schema) and lists
  the package map; `packages/api` and `packages/worker` added
- `docs/ROADMAP.md` — M1.3, M1.4 and M1.5 marked done; the queue choice (a Redis list
  over a full broker) recorded with its reasoning

## [0.1.0-foundation] — M0

### Added
- Repository foundation: directory structure per `docs/ARCHITECTURE.md`
- `docs/PRD.md` — product requirements, restructured from the original Inner Circle dossier
- `docs/ARCHITECTURE.md` — approved system architecture, provider-neutral contracts
- `docs/ROADMAP.md` — M0–M7 milestone breakdown, sized for solo/duo development pace
- `CONTRIBUTING.md` — branching, commit, PR, versioning, and changelog discipline
- CI skeleton (`.github/workflows/ci.yml`)
- PR template
