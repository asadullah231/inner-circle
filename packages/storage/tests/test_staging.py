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
from datetime import timedelta

import pytest

from packages.storage import paths, staging
from packages.storage.errors import (
    ImmutableRenderError,
    StorageError,
    UnrecoverableRenderError,
)
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
    expected = paths.asset_key(record["file_hash"], ".mp4")
    assert record["storage_key"] == expected
    # The provider's filename must not survive into the key.
    assert "pexels" not in record["storage_key"]


def test_staged_asset_is_not_scoped_to_a_project(store, sample_video):
    """Dedup is global. If assets sat under one project's prefix, deleting or
    expiring that project would remove a file other projects still use."""
    record = staging.stage_asset(store, "proj_1", "beat_001", sample_video, "c.mp4")
    assert record["storage_key"].startswith("assets/")
    assert "proj_1" not in record["storage_key"]


def test_the_same_file_in_two_projects_is_stored_once(store, sample_video):
    a = staging.stage_asset(store, "proj_A", "beat_001", sample_video, "cat.mp4")
    b = staging.stage_asset(store, "proj_B", "beat_009", sample_video, "cat.mp4")

    assert a["storage_key"] == b["storage_key"]
    assert b["deduplicated"] is True
    assert store.put_calls == 1, "the same clip was stored twice"


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


def test_stage_asset_rejects_an_unsafe_project_id(store, sample_video):
    with pytest.raises(StorageError):
        staging.stage_asset(store, "../../etc", "beat_001", sample_video, "clip.mp4")


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
                "beat_002": "assets/de/a_deadbeefdeadbeef.mp4",
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
                "beat_001": "assets/11/a_1111111111111111.mp4",
                "beat_002": "assets/22/a_2222222222222222.mp4",
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


def test_render_fails_before_starting_if_audio_is_missing(store, sample_video):
    """A missing narration is as fatal as a missing clip, and must be caught in
    the same pre-flight — the audio waveform is the timing authority."""
    record = staging.stage_asset(store, "proj_1", "beat_001", sample_video, "a.mp4")
    render_id = paths.new_render_id()

    with pytest.raises(StorageError) as exc:
        staging.prepare_render_workspace(
            store,
            "proj_1",
            render_id,
            {"beats": [{"id": "beat_001"}]},
            {"beat_001": record["storage_key"]},
            audio_key=paths.audio_key("proj_1"),  # never staged -> missing
        )
    assert "audio" in str(exc.value)


def test_missing_audio_and_beat_are_reported_together(store):
    """One failure names every missing input at once — a missing beat and a
    missing audio should not require two separate render attempts to discover."""
    render_id = paths.new_render_id()

    with pytest.raises(StorageError) as exc:
        staging.prepare_render_workspace(
            store,
            "proj_1",
            render_id,
            {"beats": [{"id": "beat_001"}]},
            {"beat_001": "assets/11/a_1111111111111111.mp4"},  # missing beat
            audio_key=paths.audio_key("proj_1"),  # missing audio
            caption_key=paths.caption_key("proj_1"),  # missing captions
        )
    message = str(exc.value)
    assert "beat_001" in message
    assert "audio" in message
    assert "captions" in message


def test_render_workspace_stages_audio_and_captions_when_present(store, sample_video):
    record = staging.stage_asset(store, "proj_1", "beat_001", sample_video, "a.mp4")
    a_key = paths.audio_key("proj_1")
    c_key = paths.caption_key("proj_1")
    store.objects[a_key] = b"narration wav bytes"
    store.objects[c_key] = b"1\n00:00:00,000 --> 00:00:01,000\nhi\n"
    render_id = paths.new_render_id()

    result = staging.prepare_render_workspace(
        store,
        "proj_1",
        render_id,
        {"beats": [{"id": "beat_001"}]},
        {"beat_001": record["storage_key"]},
        audio_key=a_key,
        caption_key=c_key,
    )

    assert os.path.isfile(os.path.join(result["workspace"], "audio", "narration.wav"))
    assert os.path.isfile(os.path.join(result["workspace"], "captions", "narration.srt"))


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


# --- two-phase publish (the orphan-edge fix) --------------------------------


def test_package_render_produces_same_objects_dict_via_staging(store, finished_render):
    """The return contract is unchanged: same keys, same final paths, and NO
    staging files left behind."""
    mp4, thumb, srt = finished_render
    render_id = paths.new_render_id()

    result = staging.package_render(
        store, "proj_1", render_id, mp4,
        thumbnail_path=thumb, caption_path=srt,
        manifest={"render_id": render_id},
    )

    objects = result["objects"]
    assert set(objects) == {"mp4", "thumbnail", "captions", "manifest"}
    for key in objects.values():
        assert store.exists(key), f"final key not present: {key}"
        # Final keys must NOT contain .staging
        assert ".staging" not in key

    # No staging files should remain
    staging_keys = [k for k in store.objects if ".staging" in k]
    assert staging_keys == [], f"staging files left behind: {staging_keys}"


def test_concurrent_render_second_caller_gets_immutable_error(store, finished_render):
    """Two calls with the same render_id: the first succeeds, the second
    raises ImmutableRenderError, and no partial state is left."""
    mp4, _, _ = finished_render
    render_id = paths.new_render_id()

    staging.package_render(store, "proj_1", render_id, mp4)
    with pytest.raises(ImmutableRenderError):
        staging.package_render(store, "proj_1", render_id, mp4)

    # No staging files should remain from the failed second attempt
    staging_keys = [k for k in store.objects if ".staging" in k]
    assert staging_keys == [], f"staging files from failed attempt: {staging_keys}"


def test_concurrent_race_belt_and_braces_check(store, finished_render):
    """If another caller publishes between our staging phase and our publish
    phase, _publish_render's re-check catches it and cleans up staging."""
    mp4, _, _ = finished_render
    render_id = "r_20260827T100000Z"
    final_mp4_key = paths.render_key("proj_1", render_id, "final.mp4")

    # Simulate: we have staged files, but another caller published in between.
    staging_mp4 = paths.render_staging_key("proj_1", render_id, "final.mp4")
    store.objects[staging_mp4] = b"our staged mp4"
    store.objects[final_mp4_key] = b"the other caller's published mp4"

    staged = {"mp4": staging_mp4}
    with pytest.raises(ImmutableRenderError):
        staging._publish_render(store, "proj_1", render_id, staged)

    # Our staging files must be cleaned up
    assert staging_mp4 not in store.objects
    # The other caller's published MP4 must survive
    assert store.exists(final_mp4_key)


def test_belt_and_braces_check_precedes_first_copy(store, finished_render):
    """The re-check in _publish_render must happen BEFORE any copy_object call.
    Patch copy_object to record calls and verify the exists() check ran first."""
    mp4, _, _ = finished_render
    render_id = "r_20260827T110000Z"
    final_mp4_key = paths.render_key("proj_1", render_id, "final.mp4")

    staging_mp4 = paths.render_staging_key("proj_1", render_id, "final.mp4")
    store.objects[staging_mp4] = b"staged bytes"
    store.objects[final_mp4_key] = b"already published"

    copy_calls = []
    original_copy = store.copy_object

    def recording_copy(src, dst, bucket=None):
        copy_calls.append((src, dst))
        return original_copy(src, dst, bucket)

    store.copy_object = recording_copy

    with pytest.raises(ImmutableRenderError):
        staging._publish_render(store, "proj_1", render_id, {"mp4": staging_mp4})

    # copy_object should never have been called — the exists() check caught it
    assert copy_calls == [], "copy_object was called before the immutability check"


def test_mid_publish_crash_leaves_staging_for_recovery(store, finished_render):
    """Simulate a crash after the MP4 copy but before the manifest copy.
    Assert:
      (a) final MP4 exists
      (b) final manifest does NOT exist
      (c) staging copies still exist
      (d) resume_publish completes it
    """
    mp4, thumb, srt = finished_render
    render_id = "r_20260827T120000Z"

    # Stage everything manually
    staging_mp4 = paths.render_staging_key("proj_1", render_id, "final.mp4")
    staging_thumb = paths.render_staging_key("proj_1", render_id, "thumb.jpg")
    staging_manifest = paths.render_staging_key("proj_1", render_id, "manifest.json")

    store.objects[staging_mp4] = b"rendered video bytes"
    store.objects[staging_thumb] = b"thumbnail bytes"
    store.objects[staging_manifest] = b'{"render_id": "r_20260827T120000Z"}'

    # Simulate partial publish: MP4 copied to final, then crash.
    final_mp4 = paths.render_key("proj_1", render_id, "final.mp4")
    store.objects[final_mp4] = store.objects[staging_mp4]
    # Staging MP4 was deleted after copy (normal behaviour), but thumb and
    # manifest staging copies remain.
    del store.objects[staging_mp4]

    final_manifest = paths.manifest_key("proj_1", render_id)

    # (a) Final MP4 exists
    assert store.exists(final_mp4)
    # (b) Final manifest does NOT exist
    assert not store.exists(final_manifest)
    # (c) Staging copies still exist
    assert store.exists(staging_thumb)
    assert store.exists(staging_manifest)

    # (d) resume_publish completes it
    result = staging.resume_publish(store, "proj_1", render_id)
    assert "manifest" in result["objects"]
    assert store.exists(result["objects"]["manifest"])
    # Staging files should be cleaned up after resume
    staging_keys = [k for k in store.objects if ".staging" in k]
    assert staging_keys == []


def test_resume_publish_rollback_when_no_manifest_and_old(store):
    """If staging files exist but the manifest was never uploaded and the files
    are older than the threshold, resume_publish cleans up and raises
    UnrecoverableRenderError."""
    render_id = "r_20260827T130000Z"
    staging_mp4 = paths.render_staging_key("proj_1", render_id, "final.mp4")
    store.objects[staging_mp4] = b"orphaned mp4"

    # Fake stat to return an old last_modified
    from datetime import timezone as _tz
    from datetime import datetime as _dt

    old_time = (_dt.now(_tz.utc) - timedelta(hours=25)).isoformat()
    original_stat = store.stat

    def fake_stat(key, bucket=None):
        result = original_stat(key, bucket)
        if ".staging" in key:
            result["last_modified"] = old_time
        return result

    store.stat = fake_stat

    with pytest.raises(UnrecoverableRenderError):
        staging.resume_publish(store, "proj_1", render_id, max_staging_age_hours=24.0)

    # Staging files should be cleaned up
    assert not store.exists(staging_mp4)


def test_resume_publish_noop_when_already_published(store, finished_render):
    """If the manifest is already at the final path, resume_publish returns
    the existing objects without changing anything."""
    mp4, _, _ = finished_render
    render_id = paths.new_render_id()

    original = staging.package_render(
        store, "proj_1", render_id, mp4,
        manifest={"render_id": render_id},
    )

    result = staging.resume_publish(store, "proj_1", render_id)
    assert result["objects"]["manifest"] == original["objects"]["manifest"]
