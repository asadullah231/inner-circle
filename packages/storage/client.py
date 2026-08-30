"""MinIO / S3 wrapper.

Nothing outside this module talks to object storage directly.
Implements contract §5 (signed URLs), §7 (dedup), §11 (idempotency).
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta

from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error

from . import paths
from .config import Settings
from .errors import (
    FileTooLargeError,
    ImmutableRenderError,
    ObjectNotFoundError,
    StorageError,
)

log = logging.getLogger("storage")


class MediaStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = Minio(
            settings.endpoint_hostport,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
            region=settings.s3_region,
        )

    # --- setup -------------------------------------------------------------

    def ensure_buckets(self) -> list[str]:
        """Create the buckets from contract §1 if they do not exist."""
        created = []
        for bucket in (
            self.settings.s3_bucket,
            self.settings.s3_uploads_bucket,
            self.settings.s3_tmp_bucket,
        ):
            if not self.client.bucket_exists(bucket):
                self.client.make_bucket(bucket)
                created.append(bucket)
                log.info("created bucket %s", bucket)
        return created

    def healthy(self) -> bool:
        try:
            self.client.bucket_exists(self.settings.s3_bucket)
            return True
        except Exception as exc:  # noqa: BLE001 - health check must never raise
            log.warning("storage health check failed: %s", exc)
            return False

    # --- existence ---------------------------------------------------------

    def exists(self, key: str, bucket: str | None = None) -> bool:
        bucket = bucket or self.settings.s3_bucket
        paths.assert_safe_key(key)
        try:
            self.client.stat_object(bucket, key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return False
            raise StorageError(f"stat failed for {key}: {exc.code}") from exc

    def stat(self, key: str, bucket: str | None = None) -> dict:
        bucket = bucket or self.settings.s3_bucket
        paths.assert_safe_key(key)
        try:
            info = self.client.stat_object(bucket, key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                raise ObjectNotFoundError(f"No such object: {key}") from exc
            raise StorageError(f"stat failed for {key}: {exc.code}") from exc
        return {
            "key": key,
            "bucket": bucket,
            "size_bytes": info.size,
            "content_type": info.content_type,
            "etag": info.etag,
            "last_modified": info.last_modified.isoformat() if info.last_modified else None,
        }

    # --- write -------------------------------------------------------------

    def put_file(
        self,
        key: str,
        local_path: str,
        kind: str,
        content_type: str | None = None,
        bucket: str | None = None,
        overwrite: bool = False,
    ) -> dict:
        """Upload a local file to a validated key.

        Idempotent by default: if the key already exists it is left alone,
        because keys for assets are content-addressed (identical key means
        identical bytes).
        """
        bucket = bucket or self.settings.s3_bucket
        paths.assert_safe_key(key)

        size = os.path.getsize(local_path)
        limit = paths.MAX_SIZE_BYTES.get(kind)
        if limit is not None and size > limit:
            raise FileTooLargeError(
                f"{kind} file is {size} bytes, limit is {limit} bytes"
            )

        if not overwrite and self.exists(key, bucket):
            log.info("skip upload, key already present: %s", key)
            return {"key": key, "bucket": bucket, "size_bytes": size, "uploaded": False}

        with open(local_path, "rb") as handle:
            self.client.put_object(
                bucket,
                key,
                handle,
                length=size,
                content_type=content_type or "application/octet-stream",
            )
        log.info("uploaded %s (%d bytes)", key, size)
        return {"key": key, "bucket": bucket, "size_bytes": size, "uploaded": True}

    def put_render_file(
        self, project_id: str, render_id: str, filename: str, local_path: str, kind: str
    ) -> dict:
        """Renders are write-once (contract §2, §11)."""
        key = paths.render_key(project_id, render_id, filename)
        if self.exists(key):
            raise ImmutableRenderError(
                f"Render object already exists and cannot be overwritten: {key}"
            )
        return self.put_file(key, local_path, kind=kind)

    def copy_object(self, source_key: str, dest_key: str, bucket: str | None = None) -> None:
        """Server-side copy within the primary bucket.

        Both keys pass through assert_safe_key. The destination is silently
        overwritten if it already exists — the caller must guard against that.
        """
        bucket = bucket or self.settings.s3_bucket
        paths.assert_safe_key(source_key)
        paths.assert_safe_key(dest_key)
        try:
            self.client.copy_object(
                bucket,
                dest_key,
                CopySource(bucket, source_key),
            )
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                raise ObjectNotFoundError(
                    f"copy source does not exist: {source_key}"
                ) from exc
            raise StorageError(
                f"copy failed {source_key} -> {dest_key}: {exc.code}"
            ) from exc
        log.info("copied %s -> %s", source_key, dest_key)

    def remove_object(self, key: str, bucket: str | None = None) -> None:
        """Delete a single object.  Idempotent — no error if the key is absent."""
        bucket = bucket or self.settings.s3_bucket
        paths.assert_safe_key(key)
        try:
            self.client.remove_object(bucket, key)
        except S3Error as exc:
            # MinIO remove_object is already idempotent, but defensive.
            if exc.code not in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                raise StorageError(f"delete failed for {key}: {exc.code}") from exc
        log.info("removed %s", key)

    # --- read --------------------------------------------------------------

    def download_to(self, key: str, dest_path: str, bucket: str | None = None) -> str:
        bucket = bucket or self.settings.s3_bucket
        paths.assert_safe_key(key)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        try:
            self.client.fget_object(bucket, key, dest_path)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                raise ObjectNotFoundError(f"No such object: {key}") from exc
            raise StorageError(f"download failed for {key}: {exc.code}") from exc
        return dest_path

    # --- signed URLs -------------------------------------------------------

    def signed_get_url(self, key: str, purpose: str = "preview", bucket: str | None = None) -> dict:
        """Time-limited GET URL. TTL comes from config, never from the caller."""
        bucket = bucket or self.settings.s3_bucket
        paths.assert_safe_key(key)
        ttl = self._ttl_for(purpose)
        if not self.exists(key, bucket):
            raise ObjectNotFoundError(f"No such object: {key}")
        url = self.client.presigned_get_object(
            bucket, key, expires=timedelta(seconds=ttl)
        )
        # The URL itself is a bearer credential. Log the key, never the URL.
        log.info("issued signed GET url for %s (purpose=%s ttl=%ds)", key, purpose, ttl)
        return {"url": url, "expires_in_s": ttl, "key": key}

    def signed_put_url(self, key: str, bucket: str | None = None) -> dict:
        bucket = bucket or self.settings.s3_uploads_bucket
        paths.assert_safe_key(key)
        ttl = self.settings.ttl_upload_s
        url = self.client.presigned_put_object(
            bucket, key, expires=timedelta(seconds=ttl)
        )
        log.info("issued signed PUT url for %s (ttl=%ds)", key, ttl)
        return {"url": url, "expires_in_s": ttl, "key": key}

    def _ttl_for(self, purpose: str) -> int:
        table = {
            "preview": self.settings.ttl_preview_s,
            "download": self.settings.ttl_download_s,
            "worker": self.settings.ttl_worker_s,
        }
        if purpose not in table:
            raise StorageError(f"Unknown signed URL purpose: {purpose}")
        return table[purpose]
