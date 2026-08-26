"""Shared test fixtures.

FakeStore is an in-memory stand-in for MediaStore. It lets the staging and
render-packaging logic be tested with no MinIO, no network and no credentials,
so these tests run everywhere including CI.

The real MinIO round-trip is covered by test_integration.py, which is skipped
unless RUN_STORAGE_INTEGRATION=1 and a live MinIO is configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from packages.storage import paths
from packages.storage.errors import (
    FileTooLargeError,
    ImmutableRenderError,
    ObjectNotFoundError,
)


@dataclass
class FakeSettings:
    s3_bucket: str = "avg-media"
    s3_uploads_bucket: str = "avg-uploads"
    s3_tmp_bucket: str = "avg-tmp"
    ffprobe_path: str = "ffprobe"
    workspace_root: str = "/tmp/avg-workspace"
    ttl_preview_s: int = 1800
    ttl_download_s: int = 18000
    ttl_worker_s: int = 1800
    ttl_upload_s: int = 900
    retention_tmp_days: int = 1
    retention_uploads_days: int = 7
    retention_asset_days: int = 30
    retention_max_deletions: int = 500


class FakeStore:
    """Mimics the parts of MediaStore that staging.py actually uses."""

    def __init__(self, settings: FakeSettings):
        self.settings = settings
        # key -> bytes
        self.objects: dict[str, bytes] = {}
        # call counters so tests can assert dedup actually skipped an upload
        self.put_calls = 0
        self.download_calls = 0

    def exists(self, key: str, bucket: str | None = None) -> bool:
        paths.assert_safe_key(key)
        return key in self.objects

    def stat(self, key: str, bucket: str | None = None) -> dict:
        if key not in self.objects:
            raise ObjectNotFoundError(f"No such object: {key}")
        return {
            "key": key,
            "bucket": self.settings.s3_bucket,
            "size_bytes": len(self.objects[key]),
            "content_type": "application/octet-stream",
            "etag": "fake",
            "last_modified": None,
        }

    def put_file(
        self,
        key: str,
        local_path: str,
        kind: str,
        content_type: str | None = None,
        bucket: str | None = None,
        overwrite: bool = False,
    ) -> dict:
        paths.assert_safe_key(key)
        size = os.path.getsize(local_path)
        limit = paths.MAX_SIZE_BYTES.get(kind)
        if limit is not None and size > limit:
            raise FileTooLargeError(f"{kind} file is {size} bytes, limit is {limit}")

        if not overwrite and key in self.objects:
            return {
                "key": key,
                "bucket": self.settings.s3_bucket,
                "size_bytes": size,
                "uploaded": False,
            }

        with open(local_path, "rb") as handle:
            self.objects[key] = handle.read()
        self.put_calls += 1
        return {
            "key": key,
            "bucket": self.settings.s3_bucket,
            "size_bytes": size,
            "uploaded": True,
        }

    def put_render_file(
        self, project_id: str, render_id: str, filename: str, local_path: str, kind: str
    ) -> dict:
        key = paths.render_key(project_id, render_id, filename)
        if key in self.objects:
            raise ImmutableRenderError(f"Render object already exists: {key}")
        return self.put_file(key, local_path, kind=kind)

    def download_to(self, key: str, dest_path: str, bucket: str | None = None) -> str:
        if key not in self.objects:
            raise ObjectNotFoundError(f"No such object: {key}")
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as handle:
            handle.write(self.objects[key])
        self.download_calls += 1
        return dest_path


@pytest.fixture
def store(tmp_path):
    settings = FakeSettings(workspace_root=str(tmp_path / "workspace"))
    return FakeStore(settings)


@pytest.fixture
def sample_video(tmp_path):
    """A small file standing in for a downloaded clip."""
    path = tmp_path / "pexels-cat-12345.mp4"
    path.write_bytes(b"fake mp4 bytes " * 100)
    return str(path)
