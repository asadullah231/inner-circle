"""Staging and render-packaging tests.

These cover the parts that actually move files around: staging a downloaded
clip, building the render workspace, and packaging a finished render.

They use FakeStore (see conftest.py) so they need no MinIO and no network.
ffprobe is stubbed out, because what is being tested here is the staging
logic, not ffprobe itself.
"""

from __future__ import annotations

import json
import os

import pytest

from packages.storage import paths, staging
from packages.storage.errors import ImmutableRenderError, StorageError
from packages.storage.probe import MediaInfo

FAKE_INFO = MediaInfo(
    media_type="video",
    duration_s=4.25,
    width=1920,
    height=1080,
    codec="h264",
    has_audio=True,
    container="mov,mp4,m4a",
)


@pytest.fixture(autouse=True)
def stub_probe(monkeypatch):
    """Replace ffprobe with a fixed answer. ffprobe has its own tests."""
    monkeypatch.setattr(staging, "probe", lambda path, ffprobe_path=None: FAKE_INFO)


# --- staging a downloaded asset --------------------------------------------


def test_stage_asset_returns_the_fields_the_asset_record_needs(store, sample_video):
    record = staging.stage_asset(
        store, "proj_1", "beat_001", sample_video, "pexels-cat-12345.mp4"
    )

    for field in (
        "local_uri",
        "storage_key",
        "file_hash",
        "downloaded_at",
        "media_type",
        "width",
        "height",
        "duration_s",
        "size_bytes",
        "deduplicated",
    ):
        assert field in record, f"missing field: {field}"

    assert record["media_type"] == "video"
    assert record["width"] == 1920
    assert record["duration_s"] == 4.25
    assert record["deduplicated"] is False
    assert len(record["file_hash"]) == 64


def test_stage_asset_key_is_content_addressed(store, sample_video):
    record = staging.stage_asset(
        store, "proj_1", "beat_001", sample_video, "pexels-cat-12345.mp4"
    )
    expected = paths.asset_key("proj_1", "beat_001", record["file_hash"], ".mp4")
    assert record["storage_key"] == expected
    # The provider's filename must not survive into the key.
    assert "pexels" not in record["storage_key"]


def test_staging_the_same_file_twice_does_not_upload_twice(store, sample_video):
    first = staging.stage_asset(
        store, "proj_1", "beat_001", sample_video, "clip.mp4"
    )
    assert store.put_calls == 1
    assert first["deduplicated"] is False

    second = staging.stage_asset(
        store, "proj_1", "beat_001", sample_video, "renamed-by-provider.mp4"
    )
    assert store.put_calls == 1, "the second identical file was uploaded again"
    assert second["deduplicated"] is True
    assert second["storage_key"] == first["storage_key"]


def test_different_bytes_produce_a_different_key(store, tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"clip one")
    b.write_bytes(b"clip two")

    ra = staging.stage_asset(store, "proj_1", "beat_001", str(a), "a.mp4")
    rb = staging.stage_asset(store, "proj_1", "beat_001", str(b), "b.mp4")
    assert ra["storage_key"] != rb["storage_key"]
    assert store.put_calls == 2


def test_stage_asset_rejects_a_disallowed_extension(store, sample_video):
    with pytest.raises(StorageError):
        staging.stage_asset(store, "proj_1", "beat_001", sample_video, "payload.exe")


def test_stage_asset_rejects_an_unsafe_beat_id(store, sample_video):
    with pytest.raises(StorageError):
        staging.stage_asset(store, "proj_1", "../../etc", sample_video, "clip.mp4")


# --- render workspace -------------------------------------------------------


def _spec():
    return {
        "schema_version": "1.0",
        "project_id": "proj_1",
        "beats": [{"id": "beat_001"}, {"id": "beat_002"}],
    }


def test_render_workspace_materialises_every_asset_locally(store, sample_video):
    r1 = staging.stage_asset(store, "proj_1", "beat_001", sample_video, "a.mp4")
    r2_src = sample_video + ".2"
    with open(r2_src, "wb") as fh:
        fh.write(b"second clip bytes")
    r2 = staging.stage_asset(store, "proj_1", "beat_002", r2_src, "b.mp4")

    render_id = paths.new_render_id()
    result = staging.prepare_render_workspace(
        store,
        "proj_1",
        render_id,
        _spec(),
        {"beat_001": r1["storage_key"], "beat_002": r2["storage_key"]},
    )

    assert result["asset_count"] == 2
    assert os.path.isdir(result["out_dir"])
    assert os.path.isfile(result["spec_path"])


def test_render_spec_gets_local_paths_not_urls(store, sample_video):
    record = staging.stage_asset(store, "proj_1", "beat_001", sample_video, "a.mp4")
    render_id = paths.new_render_id()

    result = staging.prepare_render_workspace(
        store,
        "proj_1",
        render_id,
        {"beats": [{"id": "beat_001"}]},
        {"beat_001": record["storage_key"]},
    )

    with open(result["spec_path"], encoding="utf-8") as fh:
        written = json.load(fh)

    local = written["beats"][0]["local_asset_path"]
    assert os.path.isfile(local), "the renderer was given a path that does not exist"
    assert not local.startswith("http"), "the renderer must never get a URL"
    assert not local.startswith("s3://")


def test_render_fails_before_starting_if_an_asset_is_missing(store, sample_video):
    record = staging.stage_asset(store, "proj_1", "beat_001", sample_video, "a.mp4")
    render_id = paths.new_render_id()

    with pytest.raises(StorageError) as exc:
        staging.prepare_render_workspace(
            store,
            "proj_1",
            render_id,
            _spec(),
            {
                "beat_001": record["storage_key"],
                "beat_002": "projects/proj_1/assets/beat_002/a_deadbeefdeadbeef.mp4",
            },
        )
    assert "beat_002" in str(exc.value)


def test_missing_assets_are_all_reported_at_once(store):
    render_id = paths.new_render_id()
    with pytest.raises(StorageError) as exc:
        staging.prepare_render_workspace(
            store,
            "proj_1",
            render_id,
            _spec(),
            {
                "beat_001": "projects/proj_1/assets/beat_001/a_1111111111111111.mp4",
                "beat_002": "projects/proj_1/assets/beat_002/a_2222222222222222.mp4",
            },
        )
    message = str(exc.value)
    assert "beat_001" in message and "beat_002" in message


def test_workspace_rejects_an_unsafe_project_id(store):
    with pytest.raises(StorageError):
        staging.prepare_render_workspace(
            store, "../../etc", paths.new_render_id(), _spec(), {}
        )


def test_preparing_the_workspace_twice_does_not_redownload(store, sample_video):
    record = staging.stage_asset(store, "proj_1", "beat_001", sample_video, "a.mp4")
    render_id = paths.new_render_id()
    args = (
        store,
        "proj_1",
        render_id,
        {"beats": [{"id": "beat_001"}]},
        {"beat_001": record["storage_key"]},
    )

    staging.prepare_render_workspace(*args)
    after_first = store.download_calls
    staging.prepare_render_workspace(*args)
    assert store.download_calls == after_first, "the asset was downloaded twice"


# --- packaging a finished render -------------------------------------------


@pytest.fixture
def finished_render(tmp_path):
    mp4 = tmp_path / "final.mp4"
    thumb = tmp_path / "thumb.jpg"
    srt = tmp_path / "narration.srt"
    mp4.write_bytes(b"rendered video bytes")
    thumb.write_bytes(b"thumbnail bytes")
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    return str(mp4), str(thumb), str(srt)


def test_package_render_stores_every_output(store, finished_render):
    mp4, thumb, srt = finished_render
    render_id = paths.new_render_id()

    result = staging.package_render(
        store,
        "proj_1",
        render_id,
        mp4,
        thumbnail_path=thumb,
        caption_path=srt,
        manifest={"render_id": render_id, "model": "planner_fast"},
    )

    stored = result["objects"]
    assert set(stored) == {"mp4", "thumbnail", "captions", "manifest"}
    for key in stored.values():
        assert store.exists(key), f"not stored: {key}"


def test_manifest_is_stored_as_readable_json(store, finished_render):
    mp4, _, _ = finished_render
    render_id = paths.new_render_id()
    manifest = {"render_id": render_id, "beats": 2, "provider": "pexels"}

    result = staging.package_render(store, "proj_1", render_id, mp4, manifest=manifest)
    raw = store.objects[result["objects"]["manifest"]]
    assert json.loads(raw.decode("utf-8")) == manifest


def test_a_render_cannot_be_overwritten(store, finished_render):
    """Reproducing an old render is impossible if renders are mutable."""
    mp4, _, _ = finished_render
    render_id = paths.new_render_id()

    staging.package_render(store, "proj_1", render_id, mp4)
    with pytest.raises(ImmutableRenderError):
        staging.package_render(store, "proj_1", render_id, mp4)


def test_rerendering_creates_a_separate_render_id(store, finished_render):
    mp4, _, _ = finished_render
    first = staging.package_render(store, "proj_1", "r_20260101T000000Z", mp4)
    second = staging.package_render(store, "proj_1", "r_20260102T000000Z", mp4)

    assert first["objects"]["mp4"] != second["objects"]["mp4"]
    assert store.exists(first["objects"]["mp4"]), "the earlier render was lost"


def test_package_render_rejects_an_unsafe_render_id(store, finished_render):
    mp4, _, _ = finished_render
    with pytest.raises(StorageError):
        staging.package_render(store, "proj_1", "../../etc", mp4)
