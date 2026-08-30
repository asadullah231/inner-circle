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

Project working files follow this shape:

```
projects/{project_id}/{category}/{filename}
```

**Sourced/staged assets are the one deliberate exception — they are global, not
project-scoped (see below).**

### Project categories

| Category | Example key | Written by |
|---|---|---|
| `source` | `projects/proj_123/source/script.txt` | API (user upload) |
| `audio` | `projects/proj_123/audio/narration.wav` | Audio worker |
| `captions` | `projects/proj_123/captions/narration.srt` | Audio worker |
| `renders` | `projects/proj_123/renders/r_20260817T103000Z/final.mp4` | Render worker |
| `thumbs` | `projects/proj_123/thumbs/r_20260817T103000Z/thumb.jpg` | Render worker |
| `manifests` | `projects/proj_123/manifests/r_20260817T103000Z/manifest.json` | Render worker |

### Content-addressed assets (deduplication) — GLOBAL

Sourced/downloaded assets are named from their SHA-256 hash, not from the
provider's filename, and they live under a **global** prefix, sharded by the
first two hex characters of the hash:

```
assets/{first 2 hex}/a_{first 16 hex of sha256}.{ext}
```

Example: `assets/9f/a_9f2c4e1b7d0a5c33.mp4`

**Why global, not `projects/{id}/assets/...`:** dedup is global — the same
Pexels clip used in three projects is stored once. If that single copy sat under
one project's prefix, deleting or expiring that project would break the other
two. Which project uses which asset is recorded in the database, not in the
path. The shard keeps any one prefix from holding every asset in the system.

### Render IDs

`r_{UTC timestamp, compact ISO}` — e.g. `r_20260817T103000Z`.
Renders are **immutable**. A re-render creates a new `render_id`. We never
overwrite a previous render, because §12 Phase 4 requires reproducing a prior
render from its manifest.

### Render staging prefix (internal — callers never see this)

During `package_render()`, every file is uploaded to a `.staging/` prefix
before being published to its final path:

```
projects/{project_id}/renders/{render_id}/.staging/final.mp4
projects/{project_id}/renders/{render_id}/.staging/thumb.jpg
projects/{project_id}/renders/{render_id}/.staging/narration.srt
projects/{project_id}/renders/{render_id}/.staging/manifest.json
```

Once all files are staged, they are server-side-copied to their final paths
(§2 project categories above), with the manifest copied **last** as the commit
marker. A render is considered published if and only if its manifest exists at
the final path.

**This is an internal implementation detail.** External callers never see
`.staging/` keys — not in signed URLs, not in the metadata endpoint, not in
the `package_render` return value. If `.staging/` keys are visible in the
bucket, it means a publish crashed mid-way. They can be inspected, and the
publish completed, via `resume_publish()` (a library-only recovery tool, not
an HTTP endpoint).

Orphaned staging files are crash residue. The retention job recognises them
as category `render_staging` and cleans them up after 24 hours (configurable
via `RETENTION_RENDER_STAGING_HOURS`). They are the only new positively-
cleanable shape added by this change — every other unrecognised key shape
is still treated as protected (fail-closed rule unchanged).

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
| Dashboard preview (contact sheet, Player) | **30 minutes** | GET |
| Final render download | **5 hours** | GET |
| Worker-to-worker fetch | **30 minutes** | GET |
| Browser direct upload | **15 minutes** | PUT |

Rules:

- TTL values live in config, not in code.
- Signed URLs are never written to logs, and never placed in a page that is
  cached or shared.
- A signed URL is issued only after the API has checked RBAC. This layer trusts
  the caller's authorization decision; it does not implement RBAC itself.

> **Status:** these are the team-approved values and are what the code issues
> today (see §12 for the env vars and defaults). They live in config, so ops can
> change them without a code change. Preview was widened from 15→30 min and
> download from 60 min→5 hours versus the original draft; flagging that here so
> the change is on the record. If security wants them tightened, say so.

---

## 6. Retention (proposed — needs sign-off)

| Data | Retention |
|---|---|
| `avg-tmp` scratch | 24 hours |
| `avg-uploads` raw uploads | 7 days after validation |
| Sourced/staged assets | 30 days after last project use |
| Render `.staging/` files | 24 hours (`RETENTION_RENDER_STAGING_HOURS`) |
| Final renders + manifests + captions | Permanent until project deletion |
| Deleted project | Soft-delete 30 days, then hard delete |

Render staging files are crash residue from an interrupted `package_render()`
(see §2 "Render staging prefix"). They are the only new cleanable category —
every other unrecognised key shape is still treated as protected.

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

**DB is authoritative (as of the `packages/storage/db.py` adapter).** The
`assets` table holds a `UNIQUE (file_hash)` constraint, and
`insert_or_get_asset()` upserts against it — returning the existing `id` and
`is_new=False` on a hit. That `is_new` is the source of truth for dedup; the
`deduplicated` flag `stage_asset()` returns (from a MinIO `exists()` check) is
only an upload-skip optimisation and can lag the DB, so new code should key off
`is_new`. `storage_key` and `size_bytes` are persisted columns on that table,
not just transient response fields. Retention's in-use set now comes from
`list_in_use_asset_keys()` (a query over `beats.asset_id`); the cleanup job's
`dry_run` still defaults to true until a `last_used_at` column exists and the job
is reworked to delete DB rows alongside objects.

---

## 8. Service interface

The Media Storage Service exposes HTTP so it is independent of the API's final
language choice (§4 leaves FastAPI vs NestJS open).

| Method | Endpoint | Purpose |
|---|---|---|
| `GET`  | `/healthz` | Liveness + MinIO reachability. |
| `POST` | `/v1/uploads` | Upload a source file (multipart). Hash, dedup, store. Returns key + metadata. |
| `POST` | `/v1/assets/stage` | Stage a downloaded provider asset. Returns the AssetRecord fields this layer owns. |
| `POST` | `/v1/signed-url` | Issue a time-limited **GET** URL for a known key (`purpose`: preview / download / worker). |
| `GET`  | `/v1/objects/meta?key=...` | Size, content type, hash (etag), last-modified for a key. |
| `GET`  | `/v1/renders/new-id` | Mint a fresh, immutable `render_id`. |
| `POST` | `/v1/renders/package` | Store the final MP4 + thumb + captions + manifest as one immutable render. |
| `POST` | `/v1/retention/cleanup` | Find (and, only with `dry_run=false`, delete) expired staged assets. |
| `POST` | `/v1/retention/lifecycle` | Apply bucket expiry rules to the tmp and uploads buckets. |
| `GET`  | `/v1/retention/policy` | Report the active retention windows and signed-URL TTLs. |

The signed **PUT** upload URL (`signed_put_url`) exists in the client but is not
yet wired to an HTTP endpoint. Every response includes `request_id` for tracing.

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

| Variable | Meaning | Default |
|---|---|---|
| `S3_ENDPOINT` | MinIO/S3 endpoint URL | `http://localhost:9000` |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | Credentials — server-side only, never in browser | *(required)* |
| `S3_BUCKET` | Primary media bucket | `avg-media` |
| `S3_UPLOADS_BUCKET` | Raw uploads bucket | `avg-uploads` |
| `S3_TMP_BUCKET` | Worker scratch bucket | `avg-tmp` |
| `S3_REGION` | Region string (MinIO accepts any) | `us-east-1` |
| `S3_SECURE` | `true` in production (https) | `false` |
| `SIGNED_URL_TTL_PREVIEW_S` | Preview GET TTL (seconds) | `1800` |
| `SIGNED_URL_TTL_DOWNLOAD_S` | Download GET TTL (seconds) | `18000` |
| `SIGNED_URL_TTL_WORKER_S` | Worker GET TTL (seconds) | `1800` |
| `SIGNED_URL_TTL_UPLOAD_S` | Upload PUT TTL (seconds) | `900` |
| `RETENTION_TMP_DAYS` / `RETENTION_UPLOADS_DAYS` / `RETENTION_ASSET_DAYS` | Retention windows (days) | `1` / `7` / `30` |
| `RETENTION_RENDER_STAGING_HOURS` | Staging crash-residue cleanup window (hours) | `24` |
| `RETENTION_MAX_DELETIONS` | Hard cap on deletions per cleanup run | `500` |
| `FFPROBE_PATH` | Path to ffprobe binary | `ffprobe` |
| `WORKSPACE_ROOT` | Local staging root for render workers | `/workspace` |

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
