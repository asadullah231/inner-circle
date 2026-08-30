# Media & Object Storage Layer

The single layer that every file in the pipeline passes through: uploaded
scripts, downloaded provider clips, narration audio, caption sidecars, final
renders, thumbnails and manifests. **Nothing else in the system talks to
MinIO/S3 directly.**

The full design contract lives in
[`docs/runbooks/storage-contract.md`](../../docs/runbooks/storage-contract.md).
This README is the short "how do I run and test it" version.

## The one rule everything else follows from

> **Callers never build storage paths. They pass IDs (`project_id`, `beat_id`,
> `render_id`); this layer builds and validates the key.**

That is what makes path traversal structurally impossible rather than defended
case by case. See [`paths.py`](paths.py).

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Settings from env into a frozen dataclass; required vars fail fast. |
| `errors.py` | Typed errors, each with an HTTP status + stable code. |
| `paths.py` | Key construction + every path-safety rule. Security-critical. |
| `hashing.py` | Streaming SHA-256 (files can be 2 GB; nothing is loaded whole). |
| `probe.py` | `ffprobe` wrapper → normalised `MediaInfo`. |
| `client.py` | The only module that talks to MinIO/S3 (`MediaStore`). |
| `staging.py` | Stage a downloaded asset; build a render workspace; package a render. |
| `retention.py` | Bucket lifecycle rules + the "last use" cleanup job. |
| `service.py` | FastAPI app exposing the layer over HTTP. |

## Run it locally

Needs Python 3.11+, `ffmpeg`/`ffprobe` on `PATH`, and a MinIO to talk to.

### 1. Start MinIO

Standalone binary (no Docker required):

```bash
minio server ./minio-data --console-address ":9001"
```

or via Docker:

```bash
docker run -d -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=avgadmin -e MINIO_ROOT_PASSWORD=change-me \
  minio/minio server /data --console-address ":9001"
```

S3 API on `:9000`, console on `:9001`.

### 2. Start the service

From the repo root:

```bash
python -m venv .venv
. .venv/bin/activate            # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r packages/storage/requirements.txt
cp packages/storage/.env.example packages/storage/.env   # then edit the secret
uvicorn packages.storage.service:app --reload --port 8080
```

Interactive docs at `http://localhost:8080/docs`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/healthz` | Liveness + storage reachability |
| `POST` | `/v1/uploads` | Upload a source file: hash, dedup, store |
| `POST` | `/v1/assets/stage` | Store a downloaded provider clip |
| `POST` | `/v1/signed-url` | Time-limited GET URL (`preview`/`download`/`worker`) |
| `GET`  | `/v1/objects/meta?key=...` | Size, content type, hash, etag |
| `GET`  | `/v1/renders/new-id` | Mint a fresh render id |
| `POST` | `/v1/renders/package` | Store MP4 + thumb + captions + manifest (immutable) |
| `POST` | `/v1/retention/cleanup` | Find, and optionally delete, expired assets |
| `POST` | `/v1/retention/lifecycle` | Apply bucket expiry rules |
| `GET`  | `/v1/retention/policy` | Report active policy + TTLs |

## Design invariants (do not silently break these)

- **Renders are immutable.** A re-render mints a new `render_id`; the old one
  survives (needed to reproduce a render from its manifest).
- **Dedup is global and content-addressed** (SHA-256). Assets live at
  `assets/{shard}/a_{hash}.{ext}`, never under a project prefix.
- **Signed-URL TTLs come from config, never from the caller.**
- **Cleanup fails closed:** unrecognised key shapes are treated as protected;
  renders/thumbs/manifests/captions are never deleted; `dry_run` defaults to
  `True` and there is a hard per-run deletion cap.
- **Render publication is atomic** from the caller's perspective.
  `package_render()` uploads every file to a `.staging/` prefix first, then
  copies them to their final paths in one publish step.  The manifest is
  copied last and acts as the commit marker — a render is considered published
  if and only if its manifest exists at the final path.  A crash mid-publish
  leaves staging files in place for diagnosis and recovery via
  `resume_publish()`.  Orphaned staging files older than 24 hours are treated
  as crash residue and cleaned up by the retention job.
  **Concurrency note:** S3/MinIO has no conditional-write primitive for
  `copy_object`, so the belt-and-braces check in `_publish_render` cannot
  fully close the TOCTOU window.  **The orchestration layer is responsible
  for not dispatching the same `render_id` to two workers.**  If it does, the
  early guard and the re-check will catch the race in all but a vanishingly
  narrow window, and the manifest-last ordering means a partial second
  publish is detectable.  True mutual exclusion requires an external lock
  (DB advisory lock, distributed lock service) — that belongs in the
  orchestration layer, not here.

## Tests

Unit + logic tests use an in-memory `FakeStore` (no network, no MinIO):

```bash
pytest -v
```

The **integration** tests exercise the real MinIO round-trip in `client.py`.
They are skipped unless you opt in with a live MinIO:

```bash
# PowerShell
$env:RUN_STORAGE_INTEGRATION="1"
$env:S3_ENDPOINT="http://127.0.0.1:9000"
$env:S3_ACCESS_KEY="avgadmin"
$env:S3_SECRET_KEY="<your dev secret>"
pytest -m integration packages/storage/tests/test_integration.py -v
```

They use throwaway buckets (`avg-it-<random>-*`) and clean up after themselves,
so they never touch your real dev buckets. CI runs them in the
`storage-integration` job against a MinIO container.

## Known gaps

- **Database adapter not yet wired.** Dedup currently asks MinIO whether the
  object exists; the target is a `file_hash` lookup in the `assets` table, which
  is also what will let the cleanup job source a real in-use set and stop
  defaulting to dry-run. Blocked on the DB schema / repo merge.
- **No HTTP endpoint for the signed PUT upload URL** yet (`signed_put_url`
  exists in the client).
