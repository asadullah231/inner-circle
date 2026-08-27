# Inner Circle — Automated Video Generator

Internal, self-hosted video production orchestrator: brief/script in → planned storyboard → licensed visuals + narration → deterministic render → automated QA → human-approved MP4 out. Provider-neutral by design — every LLM, TTS, stock, and render provider sits behind a swappable adapter, configured from an admin screen, never hard-coded.

**Status:** M1 — Durable Backend Skeleton, complete. A project and a job can be
created over the API, a worker drives the job through the pipeline, three human
gates block it until someone approves, and killing the worker mid-job loses
nothing. No AI yet: M2 puts a real planner behind the planning step. See
`docs/ROADMAP.md`.

## Try it

```bash
python demo_m1_full.py     # the whole milestone, no Postgres or Redis needed
pytest                     # the full suite
```

To run it for real, see [`docs/runbooks/api-and-worker.md`](docs/runbooks/api-and-worker.md).

## Start here

- [`docs/PRD.md`](docs/PRD.md) — what we're building and why
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how it's built
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — milestone-by-milestone plan
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branching/PR/release workflow
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — what shipped, when

## Packages

| Package | What it does | Milestone |
|---|---|---|
| `packages/contracts` | `VideoSpec` / `AssetRecord` — the frozen shapes every lane agrees on | M0 |
| `packages/storage` | S3/MinIO adapter: staging, dedup, signed URLs, retention, render packaging | M1 |
| `packages/db` | Core schema (`projects, jobs, beats, approvals, audit_events`), migration runner, job state machine | M1 |
| `packages/api` | FastAPI control plane: projects, jobs, gate decisions, job history | M1 |
| `packages/worker` | Job queue (Redis list) and the worker loop that advances jobs | M1 |

## Core rule

> AI suggests. The workflow engine decides. PostgreSQL remembers. Workers execute.

## License

Private / proprietary. All rights reserved.
