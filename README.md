# Inner Circle — Automated Video Generator

Internal, self-hosted video production orchestrator: brief/script in → planned storyboard → licensed visuals + narration → deterministic render → automated QA → human-approved MP4 out. Provider-neutral by design — every LLM, TTS, stock, and render provider sits behind a swappable adapter, configured from an admin screen, never hard-coded.

**Status:** Foundation (M0). No application code yet — see `docs/ROADMAP.md`.

## Start here

- [`docs/PRD.md`](docs/PRD.md) — what we're building and why
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how it's built
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — milestone-by-milestone plan
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branching/PR/release workflow
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — what shipped, when

## Core rule

> AI suggests. The workflow engine decides. PostgreSQL remembers. Workers execute.

## License

Private / proprietary. All rights reserved.
