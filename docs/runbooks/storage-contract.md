# Storage Contract — Media & Object Storage Layer

**Owner:** M. Ghufran
**Status:** DRAFT v0.1 — review requested
**Reviewers required:** Mubashir Nadeem (API/DB), Asadullah (media retrieval), Ammar (renderer/frontend), Kaif Shaikh (infra)
**Related plan sections:** §4 Core Architecture, §5 Canonical Data Contracts, §7 Asset Retrieval, §12 Phase 1, §15 Security

---

## 0. Purpose

This document defines how every file in the Automated Video Generator is stored,
named, secured, deduplicated and handed to other services.

Nobody outside this layer talks to MinIO/S3 directly. All access goes through the
Media Storage Service described here.

**If you need a file, you ask this service. You never build an S3 path yourself.**

---

## 1. Buckets

| Bucket | Contents | Retention |
|---|---|---|
| `avg-media` | All project media: sourced assets, audio, renders, thumbnails, captions | See §6 |
| `avg-uploads` | Raw user uploads before validation | 7 days |
| `avg-tmp` | Worker scratch space | 24 hours (lifecycle rule) |

Development uses a single MinIO instance. Production uses the same bucket names
against S3-compatible storage, switched only by `S3_ENDPOINT`.

---

## 2. Path scheme

All object keys follow this shape. **No exceptions.**

```
projects/{project_id}/{category}/{filename}
```

### Categories

| Category | Example key | Written by |
|---|---|---|
| `source` | `projects/proj_123/source/script.txt` | API (user upload) |
| `assets` | `projects/proj_123/assets/beat_001/a_9f2c4e1b.mp4` | Media staging worker |
| `audio` | `projects/proj_123/audio/narration.wav` | Audio worker |
| `captions` | `projects/proj_123/captions/narration.srt` | Audio worker |
| `renders` | `projects/proj_123/renders/r_20260817T1030Z/final.mp4` | Render worker |
| `thumbs` | `projects/proj_123/thumbs/r_20260817T1030Z/thumb.jpg` | Render worker |
| `manifests` | `projects/proj_123/manifests/r_20260817T1030Z/manifest.json` | Render worker |

### Content-addressed assets (deduplication)

Sourced/downloaded assets are named from their SHA-256 hash, not from the
provider's filename:

```
a_{first16charsOfSha256}.{ext}
```

Example: `a_9f2c4e1b7d0a5c33.mp4`

**Reason:** the same Pexels clip used in three projects is downloaded and stored
once. Provider filenames are unstable, unsafe and often collide.

### Render IDs

`r_{UTC timestamp, compact ISO}` — e.g. `r_20260817T1030Z`.
Renders are **immutable**. A re-render creates a new `render_id`. We never
overwrite a previous render, because §12 Phase 4 requires reproducing a prior
render from its manifest.

---

## 3. Path safety rules (§15)

The service rejects any request where:

- `project_id` does not match `^[a-zA-Z0-9_-]{1,64}$`
- `beat_id` does not match `^[a-zA-Z0-9_-]{1,64}$`
- the resulting key contains `..`, a leading `/`, a backslash, a null byte, or
  any control character
- the filename extension is not in the allowlist (§4)

Rejection raises `UnsafePathError` and is logged as a security event.
**Callers never construct keys. They pass IDs; this layer builds the key.**

---

## 4. Allowed file types

| Kind | Extensions | Max size |
|---|---|---|
| Video | `.mp4 .mov .webm .mkv` | 2 GB |
| Image | `.jpg .jpeg .png .webp` | 50 MB |
| Audio | `.wav .mp3 .m4a .aac` | 500 MB |
| Caption | `.srt .vtt` | 5 MB |
| Text/data | `.txt .md .json` | 10 MB |

Anything else is rejected at the boundary, before it reaches storage.

---

## 5. Signed URLs

Private media is never public. Access is via time-limited signed URLs.

| Use | TTL | Method |
|---|---|---|
| Dashboard preview (contact sheet, Player) | **15 minutes** | GET |
| Final render download | **60 minutes** | GET |
| Worker-to-worker fetch | **30 minutes** | GET |
| Browser direct upload | **15 minutes** | PUT |

Rules:

- TTL values live in config, not in code.
- Signed URLs are never written to logs, and never placed in a page that is
  cached or shared.
- A signed URL is issued only after the API has checked RBAC. This layer trusts
  the caller's authorization decision; it does not implement RBAC itself.

> **OPEN QUESTION for Hamza / security owner:** the delivery plan does not state
> TTL or retention numbers anywhere. The values above are my proposal. Please
> confirm or correct before Phase 1 exit.

---

## 6. Retention (proposed — needs sign-off)

| Data | Retention |
|---|---|
| `avg-tmp` scratch | 24 hours |
| `avg-uploads` raw uploads | 7 days after validation |
| Sourced/staged assets | 30 days after last project use |
| Final renders + manifests + captions | Permanent until project deletion |
| Deleted project | Soft-delete 30 days, then hard delete |

Hard deletion is a separate, explicitly-triggered job. Nothing is hard-deleted
automatically by a worker.

---

## 7. Deduplication

1. Compute SHA-256 of the file while streaming it (never load whole file in RAM).
2. Look up the hash in the `assets` table.
3. **Hit:** do not upload. Return the existing key and mark `deduplicated=true`.
4. **Miss:** upload to the content-addressed key, then insert the record.

Dedup is global across projects, because the underlying bytes are identical and
rights metadata is attached to the asset record, not to the object copy.

---

## 8. Service interface

The Media Storage Service exposes HTTP so it is independent of the API's final
language choice (§4 leaves FastAPI vs NestJS open).

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/v1/uploads` | Upload a file, hash it, dedup, store. Returns key + metadata. |
| `POST` | `/v1/assets/stage` | Stage a downloaded provider asset for a beat. Returns AssetRecord fields. |
| `POST` | `/v1/signed-url` | Issue a time-limited GET or PUT URL for a known key. |
| `GET` | `/v1/objects/{key}/meta` | Size, content type, hash, created time. |
| `POST` | `/v1/renders/{project_id}/package` | Store the final MP4 + thumb + captions + manifest as one immutable render. |
| `GET` | `/healthz` | Liveness + MinIO reachability. |

Every response includes `request_id` for tracing.

---

## 9. What this layer returns to the AssetRecord (§5)

This layer is the authoritative source for these `AssetRecord` fields:

`local_uri`, `file_hash`, `downloaded_at`, `media_type`, `width`, `height`,
`duration_s`

It does **not** own: `license`, `attribution`, `allowed_use`, `provider`,
`quality_score`, `embedding_uri`. Those come from Asadullah's retrieval worker
and the vision/ranking workers.

---

## 10. Render worker contract

Per §9 of the plan, a Remotion composition must not call any external API during
render. Therefore:

1. Before a render job starts, this layer **stages every required asset to local
   disk** in the render worker's workspace.
2. The render worker receives absolute local paths, not URLs.
3. If any asset fails to stage, the job fails **before** the renderer starts, with
   a list of missing `asset_id`s.

Workspace layout given to the renderer:

```
/workspace/{project_id}/{render_id}/
  ├── assets/a_9f2c4e1b7d0a5c33.mp4
  ├── audio/narration.wav
  ├── captions/narration.srt
  ├── spec.json          # resolved VideoSpec with local paths
  └── out/               # renderer writes here
```

After render completes, this layer uploads `out/` into the immutable render
prefix and emits `RENDER_COMPLETED` (§21.1).

---

## 11. Idempotency

- Staging the same asset for the same beat twice is a no-op.
- Uploading identical bytes twice returns the same key.
- Packaging the same `render_id` twice is rejected (renders are immutable).

This is what makes beat-level retry safe (§12 Phase 3).

---

## 12. Configuration

| Variable | Meaning |
|---|---|
| `S3_ENDPOINT` | MinIO/S3 endpoint URL |
| `S3_BUCKET` | Primary media bucket |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | Credentials — server-side only, never in browser |
| `S3_REGION` | Region string (MinIO accepts any) |
| `S3_SECURE` | `true` in production (https) |
| `SIGNED_URL_TTL_PREVIEW_S` | Default 900 |
| `SIGNED_URL_TTL_DOWNLOAD_S` | Default 3600 |
| `FFPROBE_PATH` | Path to ffprobe binary |
| `WORKSPACE_ROOT` | Local staging root for render workers |

Credentials are never returned by any endpoint and never logged.

---

## 13. Explicitly out of scope for this layer

- Authentication and RBAC — Mubashir Nadeem
- Choosing which asset to use — Asadullah / ranking worker
- Rights and licence evaluation — Adan Malik / security owner
- Rendering itself — Ammar / render worker
- Embeddings and semantic search — Phase 4, owner **currently unassigned**

> **OPEN QUESTION for Hamza:** Phase 4 deliverables include "internal asset
> library and semantic search" and "reproduce a prior render from its manifest".
> Both are storage-layer work, but my name is not in the Phase 4 lane in §11.
> Please confirm ownership.

---

## 14. Review sign-off

| Reviewer | Area | Status |
|---|---|---|
| Mubashir Nadeem | DB fields, API integration | ☐ |
| Asadullah | Staging interface | ☐ |
| Ammar | Render workspace + signed URLs | ☐ |
| Kaif Shaikh | Docker, config, secrets | ☐ |
| Hamza Arshad | TTL/retention decision, Phase 4 ownership | ☐ |
