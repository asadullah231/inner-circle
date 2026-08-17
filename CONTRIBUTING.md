# Contributing — Git & Release Workflow

Solo/duo project, but the discipline is real: history should read like a team worked on it, and any future contributor (or Inner Circle teammate joining later) should be able to reconstruct *why* a decision was made from `git log` and the PRs.

## Branches

- **`main`** — protected. Only merges via PR. Always deployable/demoable. Every merge to `main` is tagged.
- **`develop`** — integration branch. Milestone branches merge here first; `main` only receives a merge from `develop` at a milestone boundary.
- **`feature/<milestone>-<short-name>`** — e.g. `feature/m1-job-state-machine`. One feature branch per milestone (or split further if a milestone is large — prefer several small PRs over one huge one).
- **`fix/<short-name>`** — bug fixes outside the current milestone.

## Commits

Conventional Commits, imperative mood:

```
feat(api): add idempotent job state transitions
fix(media): correct Pixabay duration filter
docs(prd): update acceptance criteria for M1
test(throttle): add token-bucket burst test
chore(ci): add pytest workflow
```

Small, reviewable commits. A commit should be the unit you'd want to `git revert` on its own.

## Pull Requests

- One PR per milestone (or per sub-feature within a large milestone).
- PR description follows `.github/PULL_REQUEST_TEMPLATE.md`: objective, what changed, how it was tested, screenshots/demo for anything with a UI or visible output, checklist against the milestone's acceptance criteria from `docs/ROADMAP.md`.
- CI must be green before merge.
- Self-review checklist (solo dev, no second reviewer): re-read the diff top to bottom as if it were someone else's code, confirm the milestone's Definition of Done is actually met, confirm `docs/CHANGELOG.md` is updated.
- Merge `feature/*` → `develop` with a merge commit (keeps milestone history visible). Merge `develop` → `main` only at a milestone boundary, then tag.

## Versioning & Tags

Semantic-ish, pre-1.0: `v0.<milestone-number>.0` (e.g. `v0.1.0-foundation`, `v0.2.0` after M1, `v0.3.0` after M2 ...). First public/production-ready cut becomes `v1.0.0`, decided explicitly, not automatically.

## Changelog

`docs/CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/). Update it in the same PR as the change — not as a separate cleanup pass later.

## Documentation is part of Definition of Done

If a milestone adds a new provider, workflow, or admin capability, the relevant doc (`docs/ARCHITECTURE.md`, `docs/PRD.md`, or a new `docs/runbooks/*.md`) is updated in the *same* PR. A milestone is not done if the docs still describe the old behavior.
