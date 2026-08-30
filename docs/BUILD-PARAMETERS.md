# Inner Circle — Complete Build Parameters

The full task breakdown from where we are to a shippable product. `ROADMAP.md`
holds the seven milestones and their acceptance criteria; this file breaks those
into **68 numbered tasks**, each one a single PR with its own test and its own
definition of done.

Read `ROADMAP.md` first for *why* each milestone exists. This file is *what to
build*, in order.

---

## Legend

| Mark | Meaning |
|---|---|
| ✅ | Merged |
| 🟢 | Built and pushed, PR not opened |
| 🔨 | In progress |
| ⬜ | Not started |
| 🔒 | Blocked — the blocker is named in the row |

**Size:** S = under a day · M = 1–3 days · L = 3+ days, consider splitting.

---

## Status right now

| Milestone | Tasks | Done | Notes |
|---|---|---|---|
| M0 Foundation | 3 | 3 ✅ | Merged as PR #1 |
| M1 Backend skeleton | 9 | 8 🟢 / 1 🔒 | Three PRs unopened; PR #3 unmerged |
| M2 AI gateway & planner | 11 | 3 🔨 | Schema, provider interface and gateway drafted |
| M3 Media retrieval | 9 | 0 ⬜ | |
| M4 Audio, captions, render | 12 | 0 ⬜ | Q&B-1 must be answered first |
| M5 Editor & approval UI | 13 | 0 ⬜ | The largest milestone |
| M6 Team features | 7 | 0 ⬜ | |
| M7 Advanced | 4 | 0 ⬜ | Stretch |
| **Total** | **68** | **11** | |

---

## Open decisions that block work

These are not tasks. Each one blocks the tasks named beside it, and each is a
group decision, not a solo one.

| # | Decision | Blocks | Why it cannot wait |
|---|---|---|---|
| **Q&B-1** | Render runtime: Remotion vs FFmpeg-only | 4.5 onward, all of M5 | Remotion is the difference between a week and a month on the editor. See the licensing note below |
| **Q&B-3** | Which LLM provider is primary | 2.6, 2.9 | It costs money per call. Not a solo call |
| **Q&B-4** | Who owns the `assets` and `beats` migrations | 3.4 | Mubashir's repo is inaccessible; a collision here corrupts a live schema |
| **Q&B-5** | Is there a budget for stock media APIs | 3.2, 3.3 | Pexels is free; the fallbacks may not be |

**Licensing note on the reference editor.** `D:\Projects\innner circle\reference\react-video-editor-pro-main`
is **React Video Editor Pro, and it is not open source.** Its licence forbids
use in "competing products [or] video editing libraries" and forbids
redistribution, with a stated £50,000 penalty per violation. Our repo is public,
so committing any of that code is redistribution.

What this means in practice:

- ✅ Read it to understand *how* a timeline, an overlay system or a Remotion
  render pipeline is structured. The task list below is better for having read it.
- ✅ Use **Remotion itself** — that is a separate product with its own licence
  (free for individuals and companies under 3 people; a company licence above
  that). Q&B-1 is partly a question about that fee.
- ❌ Copy its files, components or hooks into our repo.
- ❌ Keep it inside the repo directory or commit it.

M5 below is written as *build our own editor on Remotion*, informed by the
reference, not derived from it.

---

# M0 — Foundation ✅

| # | Task | Size | Status |
|---|---|---|---|
| 0.1 | PRD, architecture, roadmap, contributing, changelog | M | ✅ |
| 0.2 | `VideoSpec` / `AssetRecord` contracts frozen | S | ✅ |
| 0.3 | Repo structure, branch protection, CI skeleton | S | ✅ |

---

# M1 — Durable Backend Skeleton

| # | Task | Size | Status | Notes |
|---|---|---|---|---|
| 1.1 | Storage layer: S3/MinIO, staging, dedup, signed URLs, retention | L | ✅ | Hafeez, PR #2 |
| 1.2 | Assets DB adapter | M | 🔒 | ronox, **PR #3 open — needs merging** |
| 1.3 | Core schema: 5 tables + migration runner | M | 🟢 | PR not opened |
| 1.4 | State machine: 12 states, 3 gates, legal-transition map | M | 🟢 | PR not opened |
| 1.5 | Durable transitions: row locks, audit trail, gate decisions | M | 🟢 | Same branch as 1.4 |
| 1.6 | FastAPI: project CRUD, job create/status/history | M | 🟢 | PR not opened |
| 1.7 | Bearer auth, `/health`, error translation (403/409/422) | S | 🟢 | Same branch as 1.6 |
| 1.8 | Queue (Redis list, `BLMOVE`) + worker loop, parks at gates | M | 🟢 | Same branch as 1.6 |
| 1.9 | Restart-recovery acceptance tests + M1 demo | M | 🟢 | Same branch as 1.6 |

**To close M1:** merge PR #3, open the three PRs. Nothing else.

---

# M2 — AI Gateway & Planner

**Objective:** a brief becomes a schema-valid `VideoSpec`, through a model layer
that is configured in the database rather than in code.

| # | Task | Size | Status | Detail |
|---|---|---|---|---|
| 2.1 | Gateway schema: 7 tables + `jobs.plan_*` columns | M | 🔨 | `003_ai_gateway.sql` written. Providers, routes, aliases, health, usage, costs, prompt versions |
| 2.2 | `TextProvider` interface + typed provider errors | S | 🔨 | Retryable vs not is the distinction that matters |
| 2.3 | JSON extraction + schema validation | S | 🔨 | Fenced, prose-wrapped and clean JSON all parse. `jsonschema` when present, a small built-in otherwise |
| 2.4 | Registry: alias resolution, health, usage, cost | M | 🔨 | Written; needs tests |
| 2.5 | Gateway: retry, repair-on-invalid, one-hop fallback, accounting | M | 🔨 | Written; needs tests |
| 2.6 | Claude adapter | M | 🔒 | **Q&B-3.** `client.messages.create`, `output_config.format` for JSON, typed error mapping |
| 2.7 | Fake provider for tests | S | ⬜ | Scriptable replies: valid, malformed, truncated, 429, timeout |
| 2.8 | Prompt registry + planner prompt v1 | M | ⬜ | Versioned rows, one active per name; `prompts/planner/` on disk seeds it |
| 2.9 | Planner: brief → `VideoSpec`, beats timed to the duration target | L | 🔒 | **Q&B-3.** The milestone's real work |
| 2.10 | Worker step: `planning` calls the planner, persists the spec | M | ⬜ | Replaces the stub in `_STEPS`. Failure marks the job failed with the reason |
| 2.11 | Eval set: 10 fixed briefs, schema-validity assertions | M | ⬜ | Catches a prompt regression before a job does |

**Acceptance:** a brief produces a schema-valid `VideoSpec` persisted on the job,
and changing `planner_fast`'s row changes which model the *next* job uses, with
no redeploy.

**Runbook:** `docs/runbooks/ai-gateway.md` — how to add a provider, an alias, a
prompt version.

---

# M3 — Media Retrieval

**Objective:** a beat's visual intent becomes ranked, licensed, deduplicated
candidate assets.

| # | Task | Size | Status | Detail |
|---|---|---|---|---|
| 3.1 | `AssetProvider` interface + search-result contract | S | ⬜ | Mirrors `TextProvider`: no vendor type escapes |
| 3.2 | Pexels adapter | M | 🔒 | **Q&B-5.** Primary provider |
| 3.3 | Fallback adapter (Pixabay or Openverse) | M | 🔒 | **Q&B-5** |
| 3.4 | Assets schema reconciliation | S | 🔒 | **Q&B-4.** Confirm ownership before writing a migration |
| 3.5 | Token-bucket throttle, per provider | S | ⬜ | Rate limits are per key, not per process |
| 3.6 | Retry with backoff + circuit breaker | M | ⬜ | Breaker trips a provider out after repeated failure |
| 3.7 | Two-layer dedup | M | ⬜ | By `(provider, provider_asset_id)` before download; by SHA-256 after |
| 3.8 | `AssetRecord` persistence incl. rights fields | M | ⬜ | Licence and attribution are not optional |
| 3.9 | Worker step: `retrieving` searches, ranks, downloads | L | ⬜ | Per-beat, resumable, independently retryable |

**Acceptance:** a beat returns ranked candidates from every enabled provider,
repeated runs download nothing twice, every file has a rights record.

---

# M4 — Audio, Captions & Rendering

**Objective:** one deterministic MP4 from a spec, its assets and its narration.

| # | Task | Size | Status | Detail |
|---|---|---|---|---|
| 4.1 | `TTSProvider` interface + one adapter | M | ⬜ | |
| 4.2 | Narration synthesis per beat, cached by text hash | M | ⬜ | Re-rendering must not re-synthesize unchanged narration |
| 4.3 | WhisperX forced alignment | M | ⬜ | **The audio is the timing authority** (PRD FR-8), not the planner's estimates |
| 4.4 | Word-level caption sidecar: SRT + VTT | M | ⬜ | Word timings, not sentence timings |
| 4.5 | Render adapter interface | M | 🔒 | **Q&B-1** |
| 4.6 | Render implementation | L | 🔒 | **Q&B-1.** Remotion composition, or an FFmpeg filter graph |
| 4.7 | Deterministic manifest: inputs → one render | M | ⬜ | Same manifest, same output. This is what makes a re-render trustworthy |
| 4.8 | Loudness normalization (EBU R128) | S | ⬜ | |
| 4.9 | Silence and clipping detection | S | ⬜ | |
| 4.10 | Render progress reporting | M | ⬜ | A 10-minute render with no progress reads as a hang |
| 4.11 | Worker step: `rendering` produces the MP4 | L | ⬜ | |
| 4.12 | Golden-render smoke test | M | ⬜ | Fixed input, `ffprobe`-asserted duration, dimensions, stream validity |

**Acceptance:** one script becomes one MP4 with correctly-timed captions,
reproducible from the same manifest.

---

# M5 — Editor & Approval UI

**Objective:** the three gates become real UI, and a producer can fix a beat
instead of rejecting the whole job.

This is the biggest milestone and the first with a frontend. Sub-grouped.

### Foundation

| # | Task | Size | Status | Detail |
|---|---|---|---|---|
| 5.1 | Next.js app scaffold + design tokens | M | ⬜ | Our own. Not the reference's components |
| 5.2 | API client + auth | S | ⬜ | Typed against the OpenAPI schema the API already emits |
| 5.3 | Project list, create, detail | M | ⬜ | |
| 5.4 | Job list + live status | M | ⬜ | Polling first; websockets only if polling proves inadequate |

### The three gates

| # | Task | Size | Status | Detail |
|---|---|---|---|---|
| 5.5 | G1: script review — beats, narration, timings, edit before approve | L | ⬜ | |
| 5.6 | G2: contact sheet — per beat, the chosen asset plus alternatives, source, licence, confidence | L | ⬜ | The screen that decides whether the output is usable |
| 5.7 | G3: final review — player, captions, QA report, approve or send back | L | ⬜ | |
| 5.8 | Rejection notes, required and surfaced | S | ⬜ | Already enforced server-side; the UI must show them |

### Editing

| # | Task | Size | Status | Detail |
|---|---|---|---|---|
| 5.9 | Remotion Player preview | M | 🔒 | **Q&B-1** |
| 5.10 | Timeline: beats, drag to retime, zoom | L | 🔒 | **Q&B-1.** The single largest piece of UI work |
| 5.11 | Overlay editing: text, image, position | L | 🔒 | **Q&B-1** |
| 5.12 | Per-beat retry from the UI | M | ⬜ | Retry one beat without restarting the job |
| 5.13 | RBAC: Admin / Producer / Reviewer / Viewer | M | ⬜ | Replaces M1's single shared token |

**Acceptance:** a producer goes brief → approved script → approved contact sheet
→ render → QA → final approval entirely in the UI, with no API calls by hand.

---

# M6 — Team Production Features

| # | Task | Size | Status | Detail |
|---|---|---|---|---|
| 6.1 | Brand profiles: fonts, colours, logo, lower-thirds | M | ⬜ | |
| 6.2 | Motion packs | M | ⬜ | |
| 6.3 | Internal asset library + semantic search | L | ⬜ | Embeddings over previously used assets |
| 6.4 | Batch generation from CSV/JSON | M | ⬜ | The idempotency key already carries this |
| 6.5 | Cost dashboard | M | ⬜ | `provider_usage` already holds the data |
| 6.6 | Per-project spend limits, enforced | M | ⬜ | A limit that does not block a job is decoration |
| 6.7 | Output profiles: 9:16, 16:9, 1:1 | M | ⬜ | |

---

# M7 — Advanced Orchestration (stretch)

| # | Task | Size | Status | Detail |
|---|---|---|---|---|
| 7.1 | Provider scoring from production history | L | ⬜ | |
| 7.2 | AI-generated clip fallback behind `VideoProvider` | L | ⬜ | Same interface as everything else |
| 7.3 | Thumbnail generation | M | ⬜ | |
| 7.4 | Decision explainer: why this model, this asset, this cost | M | ⬜ | |

**Do not start M7 until M6 is stable in real use.**

---

## Cross-cutting work

Not a milestone. These accumulate and are worth tracking.

| # | Task | When | Detail |
|---|---|---|---|
| X.1 | Docker Compose: Postgres, Redis, MinIO, API, worker | Before M3 | Nobody can run the whole system locally today |
| X.2 | `psycopg` + Docker on the dev machine | Before M3 | Integration tests currently only run in CI |
| X.3 | Structured logging with a job id on every line | During M3 | Debugging a 10-minute pipeline without it is guesswork |
| X.4 | Metrics: queue depth, step duration, failure rate | During M4 | |
| X.5 | Secrets: how keys reach production | Before M2 ships | `credential_env` names a variable; something must set it |
| X.6 | Backup and restore runbook | Before real use | |
| X.7 | Load test: 10 concurrent jobs | After M4 | |

---

## Rules that hold across every task

1. **One task, one PR, one test.** A task with no test is not done.
2. **No milestone starts before the previous one's PRs are merged.**
3. **No vendor name below the gateway.** No `if provider == "pexels"` outside
   that provider's own adapter file.
4. **Every state change goes through the state machine.** No endpoint, worker or
   script writes `jobs.state` directly.
5. **Every provider call writes a usage row**, successful or not.
6. **Licence check before any dependency lands.** The reference editor is the
   reason this rule is written down.
7. **`git add -A` is banned in this repo.** Named paths only.

---

## Suggested order

The dependency graph is nearly linear, with two exceptions worth exploiting:

```
M1 (close it)  →  M2 planner  →  M3 media  →  M4 render  →  M5 UI  →  M6
                     ↑                            ↑
                  Q&B-3                        Q&B-1
```

- **X.1 (Docker Compose)** can happen any time and makes everything after it
  easier. It is the best use of a blocked afternoon.
- **5.1–5.4 (UI foundation)** do not depend on M3 or M4 — only on M1's API,
  which exists. If someone else joins, this is the parallel lane.
- **Q&B-1 should be answered during M3**, not at the start of M4. It gates
  eleven tasks and the answer needs a licence review, which takes calendar time.
