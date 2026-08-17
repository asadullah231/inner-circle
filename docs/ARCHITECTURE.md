# Inner Circle — Architecture

**Status:** Approved baseline, carried forward from the dossier (sections 4, 18–22) with solo/duo-pace adaptations noted inline.

## Core rule

> **AI suggests. The workflow engine decides. PostgreSQL remembers. Workers execute.**

LLMs and provider APIs are never the source of truth for project state. Every state transition is durable, idempotent, auditable, and independently retryable — this rule is not negotiable and should block a PR review if violated.

## Service boundaries

| Layer | Responsibility |
|---|---|
| Experience | Next.js dashboard, storyboard, preview, approvals, project controls |
| Control | API gateway, auth/RBAC, project service, audit events |
| Workflow | Durable state machine, queue, retries, checkpoints, internal events |
| Intelligence | AI Gateway — model aliases, routing, prompt registry, cost, evaluation |
| Media | Asset retrieval, internal library, TTS, alignment, generation, metadata |
| Rendering | Remotion primary renderer, FFmpeg fallback |
| Quality | Visual/audio/caption/rights checks, final human approval |
| Data | PostgreSQL, Redis, S3/MinIO, vector index, logs |

```
Experience  ──REST+WS──▶  Control (API·Auth·RBAC)
                              │
                 ┌────────────┼────────────┐
                 ▼            ▼             ▼
             Planner   Job Orchestrator  Governance
           (LLM/VideoSpec) (queue/retries) (gates/cost)
                 │            │             │
                 └─────┬──────┴──────┬──────┘
                        ▼             ▼
              Provider gateway   Media/Render workers
                        │             │
                    Postgres      S3/MinIO
```

## Recommended stack

| Layer | Choice | Reason |
|---|---|---|
| Dashboard | Next.js + TypeScript | Matches Remotion ecosystem, strong review UI |
| API | FastAPI *or* NestJS | FastAPI if media/ML-heavy work stays in Python (media workers already are); NestJS if we go all-TypeScript — **decision needed, see Q&B-2** |
| Queue | Redis + BullMQ/Celery | Jobs are long-running and must be retryable |
| Database | PostgreSQL | Projects, versions, permissions, providers, costs, audit events |
| Media storage | S3-compatible / MinIO | Private media, signed URLs, dedup, lifecycle rules |
| Rendering | Remotion adapter + FFmpeg adapter | React design system + portable fallback |
| Local analysis | WhisperX, PySceneDetect, ffprobe, optional CLIP/VLM | Cheap, reproducible, no external uploads |
| Internal events | Redis Streams | Lightweight; Kafka is not needed at this scale |

## Provider interface (never bypass this)

```
TextProvider.generate_json(prompt, schema, model, temperature)
VisionProvider.score_asset(asset, visual_intent, constraints)
TTSProvider.synthesize(text, voice, language, style)
ASRProvider.align(audio_uri, language)
AssetProvider.search(query, filters)
VideoProvider.generate(prompt, duration_s, aspect_ratio, references)
Renderer.render(video_spec, asset_manifest, runtime)
```

No component below the provider gateway may depend on a vendor name (no `if provider == "pexels"` outside `workers/media/providers/pexels.py`).

## AI Gateway (admin-managed routing)

Model routing lives in the database (`ai_providers`, `model_routes`, `model_aliases`, `provider_health`, `provider_usage`, `provider_costs`, `prompt_versions`), edited from an `Admin > AI Providers` screen. Application code calls **stable aliases only**:

```
planner_fast      -> current low-cost planning model
script_quality    -> strongest narrative model
visual_direction  -> multimodal scene model
asset_ranker      -> low-cost vision or local model
post_reviewer     -> strongest multimodal QA model
```

Raw API keys are never sent to or displayed in the browser; the server stores an encrypted reference.

## Media intelligence & events

Every stock/uploaded/generated asset lands in one normalized catalog (asset ID, SHA-256 hash, source, rights metadata, dimensions, tags, embedding, quality score, usage count). Preferred sourcing order: team-owned → licensed stock → public-domain/CC → AI-generated.

Internal events (Redis Streams is sufficient — no Kafka): `PROJECT_CREATED, SCRIPT_APPROVED, AUDIO_READY, ASSET_SELECTED, BEAT_FAILED, RENDER_STARTED, RENDER_COMPLETED, QA_FAILED, PROJECT_COMPLETE`.

## Rendering governance

Runtime (`remotion` or `ffmpeg`) is selected and **locked** at proposal time. No silent runtime switch after approval — output behavior and licensing review must stay reproducible. React compositions receive `VideoSpec` + resolved asset URLs only; **no external API calls during render**.

## Suggested repository layout

```
inner-circle/
├── apps/
│   ├── web/          # team dashboard and storyboard review
│   ├── api/           # HTTP API, auth, project state
│   └── renderer/       # Remotion compositions and render entrypoints
├── packages/
│   ├── contracts/      # VideoSpec, AssetRecord, JobEvent schemas
│   ├── provider-gateway/ # model aliases, routing, cost, retries
│   ├── remotion-presets/ # brand themes, captions, transitions
│   └── timeline/        # runtime-neutral timeline utilities
├── workers/
│   ├── planner/  ├── media/  ├── audio/  ├── vision/  └── render/
├── pipelines/     # short-stock.yaml, explainer.yaml, documentary-montage.yaml
├── prompts/       # planner/, research/, visual-direction/, reviewer/
├── tests/         # contracts/, render-golden/, provider-mocks/, e2e/
├── docs/          # this folder: PRD, ARCHITECTURE, ROADMAP, runbooks
└── .github/workflows/
```

This is the dossier's own suggested layout (section 13), unchanged — it was already good.

## Adaptation note: solo/duo build pace

The dossier's phase plan (P0–P5, 9 weeks) assumes ~19 people working several layers in parallel. Built by one developer (+ AI pair-programming), the same layers must be built **sequentially or in tight pairs**, which is why the roadmap (`ROADMAP.md`) resequences the dossier's phases into smaller milestones, each ending in something runnable and testable, rather than six large parallel-track phases. The architecture itself — the diagram, contracts, and provider interfaces above — is unchanged from what the team already approved.
