"""Media Storage Service — HTTP interface.

Deliberately a standalone service so it does not depend on the unresolved
FastAPI-vs-NestJS decision in plan §4. The main API calls these endpoints.
"""

from __future__ import annotations

import logging
import os
import uuid

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import hashing, paths, retention, staging
from .client import MediaStore
from .config import load_settings
from .errors import StorageError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("api")

settings = load_settings()
store = MediaStore(settings)
app = FastAPI(title="AVG Media Storage Service", version="0.1.0")


@app.on_event("startup")
def startup() -> None:
    created = store.ensure_buckets()
    log.info("startup complete, buckets created: %s", created or "none")


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


@app.exception_handler(StorageError)
async def storage_error_handler(request: Request, exc: StorageError):
    # Security-relevant rejections are logged loudly (§15).
    if exc.code == "unsafe_path":
        log.warning("SECURITY unsafe path rejected: %s path=%s", exc, request.url.path)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


# --- health -----------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    ok = store.healthy()
    return {"status": "ok" if ok else "degraded", "storage_reachable": ok}


# --- uploads ----------------------------------------------------------------


@app.post("/v1/uploads")
async def upload(
    project_id: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """Upload a source file. Hashed, deduplicated, stored."""
    ext, kind = paths.classify_extension(file.filename or "")

    async def chunks():
        while True:
            data = await file.read(hashing.CHUNK_SIZE)
            if not data:
                break
            yield data

    # Spool to disk while hashing so we never hold a 2 GB file in memory.
    tmp_path = None
    try:
        import tempfile

        fd, tmp_path = tempfile.mkstemp(prefix="avg_up_")
        import hashlib

        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as out:
            async for chunk in chunks():
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        sha = digest.hexdigest()

        key = paths.source_key(project_id, file.filename or f"upload{ext}")
        result = store.put_file(key, tmp_path, kind=kind)
        return {
            "key": key,
            "file_hash": sha,
            "size_bytes": size,
            "kind": kind,
            "uploaded": result["uploaded"],
        }
    finally:
        hashing.safe_unlink(tmp_path)


# --- staging ----------------------------------------------------------------


class StageRequest(BaseModel):
    project_id: str
    beat_id: str
    local_path: str = Field(..., description="Path where the retrieval worker saved the download")
    original_filename: str


@app.post("/v1/assets/stage")
def stage(req: StageRequest) -> dict:
    return staging.stage_asset(
        store, req.project_id, req.beat_id, req.local_path, req.original_filename
    )


# --- signed URLs ------------------------------------------------------------


class SignedUrlRequest(BaseModel):
    key: str
    purpose: str = Field("preview", pattern="^(preview|download|worker)$")


@app.post("/v1/signed-url")
def signed_url(req: SignedUrlRequest) -> dict:
    return store.signed_get_url(req.key, purpose=req.purpose)


# --- metadata ---------------------------------------------------------------


@app.get("/v1/objects/meta")
def object_meta(key: str) -> dict:
    return store.stat(key)


# --- render packaging -------------------------------------------------------


class PackageRequest(BaseModel):
    project_id: str
    render_id: str
    mp4_path: str
    thumbnail_path: str | None = None
    caption_path: str | None = None
    manifest: dict | None = None


@app.post("/v1/renders/package")
def package(req: PackageRequest) -> dict:
    return staging.package_render(
        store,
        req.project_id,
        req.render_id,
        req.mp4_path,
        req.thumbnail_path,
        req.caption_path,
        req.manifest,
    )


@app.get("/v1/renders/new-id")
def new_render_id() -> dict:
    return {"render_id": paths.new_render_id()}



# --- retention --------------------------------------------------------------


class CleanupRequest(BaseModel):
    in_use_keys: list[str] = Field(default_factory=list)
    dry_run: bool = Field(True)


@app.post("/v1/retention/cleanup")
def cleanup(req: CleanupRequest) -> dict:
    """Find (and optionally delete) expired assets.

    Renders, thumbnails, manifests and captions are never touched.
    """
    policy = retention.RetentionPolicy.from_settings(settings)
    return retention.run_cleanup(
        store, policy, in_use_keys=req.in_use_keys, dry_run=req.dry_run
    )


@app.post("/v1/retention/lifecycle")
def apply_lifecycle() -> dict:
    """Apply expiry rules to the temp and uploads buckets."""
    policy = retention.RetentionPolicy.from_settings(settings)
    return {"applied": retention.apply_lifecycle_rules(store, policy)}


@app.get("/v1/retention/policy")
def retention_policy() -> dict:
    policy = retention.RetentionPolicy.from_settings(settings)
    return {
        "tmp_days": policy.tmp_days,
        "uploads_days": policy.uploads_days,
        "asset_days": policy.asset_days,
        "render_staging_hours": policy.render_staging_hours,
        "max_deletions_per_run": policy.max_deletions_per_run,
        "signed_url_ttl_s": {
            "preview": settings.ttl_preview_s,
            "download": settings.ttl_download_s,
            "worker": settings.ttl_worker_s,
        },
    }
