"""Configuration for the Media Storage Service.

All values come from environment variables. Nothing is hard-coded, and no
secret is ever returned by an endpoint or written to a log.
"""

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- object storage ---
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    s3_uploads_bucket: str
    s3_tmp_bucket: str
    s3_region: str
    s3_secure: bool

    # --- signed URL lifetimes (seconds) ---
    ttl_preview_s: int
    ttl_download_s: int
    ttl_worker_s: int
    ttl_upload_s: int

    # --- local paths ---
    ffprobe_path: str
    workspace_root: str

    @property
    def endpoint_hostport(self) -> str:
        """The minio client wants 'host:port' without a scheme."""
        return (
            self.s3_endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        )


def load_settings() -> Settings:
    return Settings(
        s3_endpoint=_env("S3_ENDPOINT", "http://localhost:9000"),
        s3_access_key=_env("S3_ACCESS_KEY", required=True),
        s3_secret_key=_env("S3_SECRET_KEY", required=True),
        s3_bucket=_env("S3_BUCKET", "avg-media"),
        s3_uploads_bucket=_env("S3_UPLOADS_BUCKET", "avg-uploads"),
        s3_tmp_bucket=_env("S3_TMP_BUCKET", "avg-tmp"),
        s3_region=_env("S3_REGION", "us-east-1"),
        s3_secure=_env_bool("S3_SECURE", False),
        ttl_preview_s=_env_int("SIGNED_URL_TTL_PREVIEW_S", 900),
        ttl_download_s=_env_int("SIGNED_URL_TTL_DOWNLOAD_S", 3600),
        ttl_worker_s=_env_int("SIGNED_URL_TTL_WORKER_S", 1800),
        ttl_upload_s=_env_int("SIGNED_URL_TTL_UPLOAD_S", 900),
        ffprobe_path=_env("FFPROBE_PATH", "ffprobe"),
        workspace_root=_env("WORKSPACE_ROOT", "/workspace"),
    )
