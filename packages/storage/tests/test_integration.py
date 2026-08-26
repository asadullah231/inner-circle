"""Integration tests for client.py against a REAL MinIO.

Every other test in this suite uses FakeStore. This file is the ONLY place the
actual MinIO round-trip in client.py is exercised:

  * bucket creation + health
  * put / stat (size, content-type, etag) / download round-trip
  * the dedup skip (put an existing key -> uploaded: false)
  * the size-limit guard
  * the S3Error -> typed-error mapping (a missing object really does surface as
    ObjectNotFoundError, and exists() really returns False) -- this branch was
    previously asserted only by assumption, never run
  * signed GET and PUT URLs, driven by a real HTTP client
  * render write-once immutability
  * the retention delete path (list -> select -> remove_object), including that
    a render is never touched and an in-use asset is spared

It is SKIPPED unless RUN_STORAGE_INTEGRATION=1 and a reachable MinIO is
configured through the usual S3_* environment variables. In CI a throwaway
MinIO container is started for exactly this job (see
.github/workflows/ci.yml, job: storage-integration).

To run it locally against the standalone minio.exe (PowerShell):

    $env:RUN_STORAGE_INTEGRATION="1"
    $env:S3_ENDPOINT="http://127.0.0.1:9000"
    $env:S3_ACCESS_KEY="avgadmin"
    $env:S3_SECRET_KEY="<your dev secret>"
    pytest -m integration packages/storage/tests/test_integration.py -v

The buckets it uses are unique per run and removed on teardown, so it never
touches your real dev buckets.
"""

from __future__ import annotations

import dataclasses
import os
import urllib.request
import uuid

import pytest

from packages.storage import paths, retention
from packages.storage.client import MediaStore
from packages.storage.config import load_settings
from packages.storage.errors import (
    FileTooLargeError,
    ImmutableRenderError,
    ObjectNotFoundError,
    StorageError,
)

# The whole module is opt-in: it needs a live MinIO. Without the flag every
# test here is skipped, so the ordinary (FakeStore) suite stays runnable with
# no network.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_STORAGE_INTEGRATION") != "1",
        reason="set RUN_STORAGE_INTEGRATION=1 and run a MinIO to enable",
    ),
]


def _http(method: str, url: str, data: bytes | None = None) -> tuple[int, bytes]:
    """Tiny HTTP client for driving presigned URLs. Local MinIO only."""
    req = urllib.request.Request(url, data=data, method=method)
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - local MinIO only
        return resp.status, resp.read()


@pytest.fixture(scope="module")
def store():
    """A MediaStore pointed at real MinIO, using throwaway buckets.

    Buckets are named per-run and dropped on teardown, so this is safe to run
    against a shared dev MinIO without clobbering real dev data.
    """
    base = load_settings()
    suffix = uuid.uuid4().hex[:8]
    settings = dataclasses.replace(
        base,
        s3_bucket=f"avg-it-{suffix}-media",
        s3_uploads_bucket=f"avg-it-{suffix}-uploads",
        s3_tmp_bucket=f"avg-it-{suffix}-tmp",
    )
    st = MediaStore(settings)
    st.ensure_buckets()
    try:
        yield st
    finally:
        _drop_buckets(st, settings)


def _drop_buckets(st: MediaStore, settings) -> None:
    for bucket in (
        settings.s3_bucket,
        settings.s3_uploads_bucket,
        settings.s3_tmp_bucket,
    ):
        try:
            for obj in st.client.list_objects(bucket, recursive=True):
                st.client.remove_object(bucket, obj.object_name)
            st.client.remove_bucket(bucket)
        except Exception:  # noqa: BLE001 - teardown must never fail the run
            pass


@pytest.fixture
def local_file(tmp_path):
    def _make(name: str, data: bytes) -> str:
        p = tmp_path / name
        p.write_bytes(data)
        return str(p)

    return _make


# --- buckets + health -------------------------------------------------------


def test_ensure_buckets_is_idempotent_and_healthy(store):
    # ensure_buckets already ran in the fixture; a second call creates nothing.
    assert store.ensure_buckets() == []
    assert store.healthy() is True


# --- put / stat / download --------------------------------------------------


def test_put_stat_download_roundtrip(store, local_file, tmp_path):
    data = b"integration video bytes " * 10
    key = paths.asset_key("c" * 64, ".mp4")
    src = local_file("clip.mp4", data)

    result = store.put_file(key, src, kind="video", content_type="video/mp4")
    assert result["uploaded"] is True
    assert store.exists(key) is True

    meta = store.stat(key)
    assert meta["size_bytes"] == len(data)
    assert meta["content_type"] == "video/mp4"
    assert meta["etag"]

    dest = str(tmp_path / "roundtrip.mp4")
    store.download_to(key, dest)
    with open(dest, "rb") as fh:
        assert fh.read() == data


def test_put_is_idempotent_when_key_exists(store, local_file):
    key = paths.asset_key("d" * 64, ".mp4")
    src = local_file("dupe.mp4", b"same content-addressed bytes")
    first = store.put_file(key, src, kind="video")
    second = store.put_file(key, src, kind="video")
    assert first["uploaded"] is True
    assert second["uploaded"] is False  # skipped, not re-uploaded


def test_put_file_enforces_size_limit(store, local_file, monkeypatch):
    monkeypatch.setitem(paths.MAX_SIZE_BYTES, "caption", 8)
    key = "projects/it_proj/captions/narration.srt"
    src = local_file("big.srt", b"well over eight bytes")
    with pytest.raises(FileTooLargeError):
        store.put_file(key, src, kind="caption")
    # The oversized object must not have been created.
    assert store.exists(key) is False


# --- the S3Error -> typed-error mapping (previously never executed) ---------


def test_stat_missing_object_raises_object_not_found(store):
    with pytest.raises(ObjectNotFoundError):
        store.stat(paths.asset_key("e" * 64, ".mp4"))


def test_exists_is_false_for_missing_object(store):
    assert store.exists(paths.asset_key("f" * 64, ".mp4")) is False


def test_download_missing_object_raises_object_not_found(store, tmp_path):
    with pytest.raises(ObjectNotFoundError):
        store.download_to(
            paths.asset_key("0" * 64, ".mp4"), str(tmp_path / "missing.mp4")
        )


# --- signed URLs, exercised with a real HTTP client -------------------------


def test_signed_get_url_serves_the_object(store, local_file):
    data = b"downloadable bytes"
    key = paths.asset_key("1" * 64, ".mp4")
    store.put_file(
        key, local_file("g.mp4", data), kind="video", content_type="video/mp4"
    )

    signed = store.signed_get_url(key, purpose="preview")
    assert signed["expires_in_s"] == store.settings.ttl_preview_s

    status, body = _http("GET", signed["url"])
    assert status == 200
    assert body == data


def test_signed_get_url_missing_object_raises(store):
    with pytest.raises(ObjectNotFoundError):
        store.signed_get_url(paths.asset_key("2" * 64, ".mp4"))


def test_signed_get_url_rejects_unknown_purpose(store):
    # _ttl_for rejects the purpose before anything touches storage.
    with pytest.raises(StorageError):
        store.signed_get_url(paths.asset_key("3" * 64, ".mp4"), purpose="forever")


def test_signed_put_url_accepts_an_upload(store):
    key = paths.source_key("it_proj", "hello.txt")
    signed = store.signed_put_url(key)
    assert signed["expires_in_s"] == store.settings.ttl_upload_s

    status, _ = _http("PUT", signed["url"], data=b"uploaded via presigned put")
    assert status in (200, 204)
    # It must land in the uploads bucket, not the media bucket.
    assert store.exists(key, bucket=store.settings.s3_uploads_bucket) is True
    assert store.exists(key, bucket=store.settings.s3_bucket) is False


# --- render immutability ----------------------------------------------------


def test_put_render_file_is_write_once(store, local_file):
    src = local_file("final.mp4", b"rendered bytes")
    store.put_render_file(
        "it_proj", "r_20260101T000000Z", "final.mp4", src, kind="video"
    )
    with pytest.raises(ImmutableRenderError):
        store.put_render_file(
            "it_proj", "r_20260101T000000Z", "final.mp4", src, kind="video"
        )


# --- retention delete path --------------------------------------------------


def test_cleanup_deletes_unused_assets_but_never_renders(store, local_file):
    # asset_days=-1 makes every real object "older than the window", so age is
    # not what this test is about -- protection, the in-use guard, and the real
    # remove_object call are.
    policy = dataclasses.replace(
        retention.RetentionPolicy.from_settings(store.settings), asset_days=-1
    )

    asset_key = paths.asset_key("4" * 64, ".mp4")
    kept_asset_key = paths.asset_key("5" * 64, ".mp4")
    render_id = "r_20260101T010101Z"
    render_key = paths.render_key("it_proj", render_id, "final.mp4")

    store.put_file(asset_key, local_file("a.mp4", b"aaa"), kind="video")
    store.put_file(kept_asset_key, local_file("b.mp4", b"bbb"), kind="video")
    store.put_render_file(
        "it_proj", render_id, "final.mp4", local_file("c.mp4", b"ccc"), kind="video"
    )

    report = retention.run_cleanup(
        store, policy, in_use_keys=[kept_asset_key], dry_run=False
    )

    assert asset_key in report["keys"]  # unused + expired -> deleted
    assert kept_asset_key not in report["keys"]  # in use -> spared
    assert store.exists(asset_key) is False
    assert store.exists(kept_asset_key) is True
    assert store.exists(render_key) is True  # renders are never touched
