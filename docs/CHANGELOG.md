# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `packages/db` — core schema migration (`projects, jobs, beats, approvals, audit_events`)
  and a forward-only migration runner (M1.1)
- CI job `core-db-integration` running the core schema suite against postgres:16

### Changed
- `docs/ROADMAP.md` — M1 split into sub-milestones M1.0 through M1.5; FastAPI locked
  as the API stack (resolves Q&B-2)
- `README.md` — status now reflects shipped code (storage layer, core schema) and lists
  the package map

## [0.1.0-foundation] — M0

### Added
- Repository foundation: directory structure per `docs/ARCHITECTURE.md`
- `docs/PRD.md` — product requirements, restructured from the original Inner Circle dossier
- `docs/ARCHITECTURE.md` — approved system architecture, provider-neutral contracts
- `docs/ROADMAP.md` — M0–M7 milestone breakdown, sized for solo/duo development pace
- `CONTRIBUTING.md` — branching, commit, PR, versioning, and changelog discipline
- CI skeleton (`.github/workflows/ci.yml`)
- PR template
