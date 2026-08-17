# Inner Circle — Automated Video Generator
## Product Requirements Document

**Status:** Draft v1.0 — awaiting owner approval
**Owner:** Asad Ullah
**Source baseline:** *Inner Circle · Delivery and Ownership Dossier* (14 Aug 2026) — this PRD restructures and formalizes that dossier into a living spec. Where this document and the dossier disagree, this document wins; the dossier remains the historical record of the original approved direction.

---

## 1. Product Goals

Build an internal, self-hosted **video production orchestrator** — not a video-generation model, not a nonlinear editor. A user supplies a script or a brief; the system plans a storyboard, sources or generates the visuals, produces narration and captions, renders a deterministic video, runs automated QA, and hands the result to a human for final approval before delivery.

Primary goals, in order:

1. **Reliability over cleverness.** A production line that is auditable, retryable, and reproducible beats a system that occasionally produces something impressive but cannot explain or repeat itself.
2. **Provider neutrality.** No vendor (LLM, TTS, stock footage, video-gen model, render engine) is ever hard-coded into application logic. Swapping a provider is a configuration change, not a redeploy.
3. **Stock-first, generation-as-fallback.** AI video generation is used only when licensed/real footage cannot satisfy a beat, to control cost, latency, and rights risk.
4. **Human-in-the-loop by design.** Script, assets, and final render each pass a human approval gate. Nothing publishes itself.
5. **Cost and rights are first-class data**, not an afterthought bolted on later.

### Non-goals (explicitly out of scope for v1)

- A full nonlinear editor (Premiere/CapCut replacement)
- Automatic publishing to social platforms
- Arbitrary web/social scraping for source footage
- A custom video-generation model
- Public self-service accounts, billing, subscriptions
- Avatar/host video, long-form (10–30 min) production, multi-language dubbing, collaborative simultaneous editing — all deferred to Future Scope (§14)

---

## 2. Users / Personas

This is an **internal team platform**, not a public product. Personas are roles a real user occupies while using the finished system (distinct from the people *building* it):

| Persona | Description | Primary need |
|---|---|---|
| **Producer** (Editor/Researcher) | Supplies a script or brief, reviews the storyboard/contact sheet, replaces or regenerates individual beats | Fast path from idea to a video worth publishing, with control at the moments that matter |
| **Reviewer / QA** | Reviews script for factual/safety issues (Gate 1), asset relevance (Gate 2), and does final sign-off (Gate 3/G3) | Confidence that nothing unapproved reaches "final" |
| **Admin / Manager** | Manages AI provider/model routing, brand profiles, budgets, org/team membership, RBAC | Control cost and quality without touching backend code |
| **Viewer / Stakeholder** | Read-only visibility into project status and finished output | Awareness without needing edit access |

---

## 3. Core Workflows

### 3.1 The production pipeline (every project follows this)

```
01 Create project → 02 Provide brief/script → 03 Plan & estimate
  → [G1 Approve script] → 04 Produce audio → 05 Source visuals
  → [G2 Approve contact sheet] → 06 Compose & render → 07 Automated QA
  → [G3 Final approval] → OUT Delivery package (MP4, thumbnail, SRT/VTT, manifests, QA report)
```

Two entry modes at step 02:

- **Script mode** — producer supplies full narration text. System builds beats, sources visuals, produces/accepts voiceover, renders.
- **Brief mode** — producer supplies topic, audience, tone, duration, format. System researches, drafts a script, and *stops for G1 approval* before continuing — brief mode never skips the script gate.

### 3.2 Beat-level review (the actual UI surface at G2)

Every beat is a card: narration text, timestamps, visual-intent query, selected asset + alternatives, source/license, confidence score, and accept/replace/regenerate controls. This is a **contact-sheet review**, not a timeline editor — a full NLE is explicitly deferred (see Non-goals).

### 3.3 Failure and retry

A failed beat (bad asset, provider timeout, malformed generation) retries **independently** — it must never force the whole job to restart. Every state transition is idempotent and audit-logged.

---

## 4. Features

Grouped by the layer they live in (see §8 Architecture for the layer diagram):

**Planning**
- Brief → researched, structured storyboard (VideoSpec) via one planner call
- Script → beat segmentation with visual-intent extraction per beat

**Media**
- Multi-provider stock search (Pexels primary; Pixabay/Openverse/Wikimedia/Internet Archive as configured fallbacks), run in parallel, deduplicated, rights-tagged
- Internal reusable asset library with semantic search (grows over time — every downloaded/uploaded/generated asset is normalized into one catalog)
- TTS + word-level alignment (narration is the timing authority, never a model's estimated duration)
- Word-level captions in 99 languages, sidecar SRT/VTT, optional burn-in
- AI-generated video/image as an explicit fallback only, behind the same `VideoProvider` contract as every other source

**Rendering**
- Remotion as primary composition runtime (React, deterministic, JSON-driven) — **pending a license review before this becomes a hard dependency (see Q&B-1)**
- FFmpeg-only compositor as a portable fallback that does not require Remotion

**Quality**
- Four enforced gates: script, storyboard/assets, pre-render, post-render — each is a state-machine transition, not a soft recommendation
- Golden-video regression suite

**Admin**
- Provider/model configuration from one screen (`Admin > AI Providers`) — add/edit/enable/disable providers, set aliases, priority, fallback, cost caps, timeouts — with **zero backend redeploy**
- Cost dashboard, per-project spend limits, provider health monitoring

---

## 5. Functional Requirements

| ID | Requirement |
|---|---|
| FR-1 | System shall accept either a full script or a brief (topic/audience/tone/duration/format) as project input. |
| FR-2 | System shall generate a `VideoSpec` (see §7) from the input, with one beat per narration segment. |
| FR-3 | System shall require explicit human approval at three gates (script, asset/contact-sheet, final) before proceeding past each. |
| FR-4 | System shall retrieve visual candidates from at least one configured stock provider per beat, honoring `source_policy` order: team-owned media → licensed stock → public-domain/CC → AI-generated. |
| FR-5 | System shall never call an unapproved external source (arbitrary web scraping, social platform scraping) for reusable footage. |
| FR-6 | System shall deduplicate retrieved assets by provider-ID before download and by file hash after download. |
| FR-7 | System shall store full rights metadata (license, attribution, allowed use, source URL, retrieval time) for every asset. |
| FR-8 | System shall align narration audio and use the resulting word timestamps — not any model's estimated duration — as the authoritative timing for captions and cuts. |
| FR-9 | System shall render through a locked runtime (`remotion` or `ffmpeg`) selected at proposal time; runtime shall not silently change mid-job. |
| FR-10 | System shall run automated post-render checks (stream/codec/duration validity, black-frame detection, audio clipping, caption bounds) before a job can reach final approval. |
| FR-11 | System shall persist every job/beat state transition as an idempotent, audit-logged event; a retried beat shall not re-run already-successful beats. |
| FR-12 | System shall let an admin change AI provider/model routing from a UI without a code change or redeploy. |
| FR-13 | System shall estimate cost before execution and record actual cost after, broken down by planning/TTS/stock/generated-footage/storage/render. |
| FR-14 | System shall enforce a per-project spend limit and refuse (or pause for approval) work that would exceed it. |
| FR-15 | System shall support at least three output profiles: 9:16, 16:9, 1:1. |

---

## 6. Non-Functional Requirements

| Category | Requirement |
|---|---|
| **Reliability** | Every state transition idempotent and independently retryable at beat level. No single provider failure invalidates a whole job. |
| **Reproducibility** | Every job stores a frozen configuration snapshot (resolved provider/model/routing at time of run) so re-running from a saved manifest produces the same decisions even if admin config changes later. |
| **Auditability** | Every provider call, cost, decision, and approval is logged with enough context to answer "why did the system pick this?" after the fact. |
| **Security** | Provider API keys and secrets live server-side only, never in the browser bundle. Signed URLs with expiry for private media. |
| **Observability** | Queue health, worker status, render latency, API error rate, and provider failures are monitored; job traceability is Project → Job → Beat → Provider → Render → QA. |
| **Performance** | Interactive planning calls should complete in a few seconds (cheap/fast model alias); rendering is async and streamed/polled, not a blocking request. |
| **Cost governance** | No per-word LLM calls for routine visual selection — the planner makes one structured call per storyboard; scene-level calls are reserved for regeneration only. |
| **Portability** | Core contracts and orchestration logic remain provider-neutral; no Remotion/FFmpeg/vendor-specific shape leaks past its adapter. |

---

## 7. Data / Entities

### 7.1 Canonical contracts (never let vendor JSON leak past these)

**VideoSpec** — schema_version, project_id, title, format (width/height/fps), language, duration_target_s, brand_profile_id, source_policy, narration (provider/voice_id), beats[] (id, narration, start_s, end_s, visual_intent, search_queries[], shot_type, asset_id, overlay, confidence).

**AssetRecord** — asset_id, provider, provider_asset_id, source_url, local_uri, media_type, width, height, duration_s, license, attribution, allowed_use, downloaded_at, file_hash, embedding_uri, quality_score.

### 7.2 Job state machine

`Draft → Planning → [G1 Script approval] → Sourcing → [G2 Asset approval] → Audio+compose → Rendering → QA → [G3 Final approval] → Complete`

### 7.3 Core tables (PostgreSQL)

`projects, jobs, beats, approvals, providers, costs, audit_events, ai_providers, model_routes, model_aliases, provider_health, provider_usage, provider_costs, prompt_versions`

### 7.4 Internal asset library record

Every asset (uploaded, stock-retrieved, or generated) is normalized into one catalog: asset ID, SHA-256 hash, source, provider asset ID, rights metadata, duration, dimensions, orientation, semantic tags, embedding, quality score, usage count, project history, storage URI.

---

## 8. Integrations

| Capability | First adapter | Fallback(s) |
|---|---|---|
| Text planning/scripting | Discounted frontier-model gateway | Second compatible provider; local model later |
| TTS | ElevenLabs / discounted TTS API | Kokoro, Piper, Edge-TTS |
| Alignment | WhisperX | Whisper, Deepgram |
| Stock video | Pexels | Pixabay, Openverse, Wikimedia Commons, Internet Archive, internal library |
| Rendering | Remotion | FFmpeg-only |
| Storage | S3/MinIO | Local disk (dev only) |
| Queue | Redis + BullMQ/Celery | Postgres-backed queue (small installs) |
| Local analysis | WhisperX, PySceneDetect, ffprobe | optional CLIP/VLM |

All access via a generic `AssetProvider` / `TTSProvider` / `ASRProvider` / `VideoProvider` / `Renderer` interface — see §4 and §8 of the dossier for exact method signatures, carried forward unchanged.

---

## 9. Security

- Provider keys and secrets: server/worker environment or a secrets manager only; the server encrypts and stores a *reference*, never displays a raw key after saving.
- Signed URLs with expiry for all private media.
- Every user-provided path, URL, and provider response is sanitized (path traversal protection at the job-dispatch boundary).
- Web pages, transcripts, and uploaded documents are **untrusted input** — planner prompts must be protected from injection originating in this content.
- Explicit consent required before voice cloning, portrait, avatar, or face-replacement features are used (none are in v1 scope).
- No automatic publishing — a human approves the final render every time.
- Dependency/model licensing (Remotion's special license, FFmpeg's LGPL/GPL build configuration, every model checkpoint) reviewed before any production or wider-team use.

---

## 10. Permissions (RBAC)

| Role | Can | Cannot |
|---|---|---|
| Admin | Manage providers/models/routing, budgets, org membership, all project actions | — |
| Producer | Create projects, submit brief/script, approve/replace/regenerate at G1/G2, request final review | Change provider/model config, approve their own G3 (recommend 4-eyes for final approval — **flagged for your decision, §15**) |
| Reviewer/QA | Approve/reject at any gate, view audit trail | Change provider config |
| Viewer | Read project status and finished output | Any write action |

---

## 11. UI/UX Requirements

- **Dashboard** (Next.js): project list, create-project flow, per-project status.
- **Storyboard / contact sheet**: one card per beat — narration, timestamps, visual-intent query, selected asset thumbnail, alternatives, source/license badge, confidence, accept/replace/regenerate controls.
- **Player preview**: Remotion `<Player>`-style in-browser preview, no export needed to see the composition.
- **Approval controls**: explicit accept/reject at G1/G2/G3, with a required note on reject.
- **Admin > AI Providers** screen: provider, base URL, API key (write-only), model alias, actual model, priority, fallback, enabled toggle, max cost, timeout.
- Components consuming `VideoSpec` + resolved asset URLs must render **deterministically** — no external API calls during render.

---

## 12. Acceptance Criteria (product-level)

The v1 product is accepted when, for a script or brief input:

1. A producer can go from brief to a delivered MP4 through the UI alone, passing all three approval gates, without touching a config file.
2. A failed beat (simulated provider timeout or bad asset) retries independently and does not restart the job.
3. The same saved manifest re-renders to the same output after a restart.
5. An admin can change which model backs `planner_fast` from the UI and the next job uses it — with zero code change or redeploy.
6. Every delivered asset has a complete, inspectable rights record.
7. Estimated cost is shown before a job starts to consume paid providers, and actual cost is recorded after.

---

## 13. What We Are Deliberately Not Building First

(Carried forward from the dossier, section 16 — still binding.)

A full nonlinear editor · automatic social publishing · arbitrary web scraping · a custom video-generation model · ten provider integrations before one pipeline is reliable · billing/subscriptions/public accounts · per-word LLM calls for visual selection · a single opaque "agent prompt" that hides state and decisions.

---

## 14. Future Scope

Deferred until the core pipeline (through M6 in the roadmap) is stable and proven:

- Avatar/host videos with explicit consent
- Long-form (10–30 minute) production
- Multi-language dubbing
- Transcript-based timeline editor
- Platform publishing integrations (YouTube/TikTok/Instagram)
- Local GPU video generation
- Collaborative simultaneous editing
- Provider scoring/evaluation engine using accumulated production evidence instead of static preference

---

*This document supersedes the informal notes in the original dossier PDFs for day-to-day development decisions. Update it in the same PR as any change it should reflect — do not let it drift.*
