"""Path safety tests.

This file IS the Phase 1 security evidence for §15 "sanitize every
user-provided path". Show the passing output to Arslan Ahmad.

Run with:  pytest tests/test_paths.py -v
These tests need no MinIO, no network and no ffmpeg.
"""

import pytest

from packages.storage import paths
from packages.storage.errors import UnsafePathError, UnsupportedFileTypeError

SHA = "a" * 64


# --- attack cases -----------------------------------------------------------

ATTACK_IDS = [
    "../../etc/passwd",
    "..",
    "../",
    "proj/../../root",
    "proj/sub",
    "proj\\sub",
    "proj\x00null",
    "proj\nnewline",
    "proj id with spaces",
    "",
    "a" * 65,
    "/absolute",
    "./relative",
    "proj;rm -rf /",
    "proj$(whoami)",
    "%2e%2e%2f",
]


@pytest.mark.parametrize("bad_id", ATTACK_IDS)
def test_malicious_project_ids_are_rejected(bad_id):
    with pytest.raises(UnsafePathError):
        paths.project_prefix(bad_id)


@pytest.mark.parametrize("bad_id", ATTACK_IDS)
def test_malicious_beat_ids_are_rejected(bad_id):
    with pytest.raises(UnsafePathError):
        paths.asset_key("proj_123", bad_id, SHA, ".mp4")


def test_traversal_in_filename_cannot_escape():
    # The filename is reduced to its basename before use.
    key = paths.source_key("proj_123", "../../../etc/passwd.txt")
    assert key == "projects/proj_123/source/passwd.txt"
    assert ".." not in key


def test_windows_traversal_in_filename_cannot_escape():
    key = paths.source_key("proj_123", r"..\..\windows\system32\evil.txt")
    assert key.startswith("projects/proj_123/source/")
    assert ".." not in key
    assert "\\" not in key


def test_assert_safe_key_blocks_absolute_paths():
    with pytest.raises(UnsafePathError):
        paths.assert_safe_key("/projects/proj_123/source/a.txt")


def test_assert_safe_key_blocks_double_slash():
    with pytest.raises(UnsafePathError):
        paths.assert_safe_key("projects//proj_123/source/a.txt")


def test_assert_safe_key_blocks_control_characters():
    with pytest.raises(UnsafePathError):
        paths.assert_safe_key("projects/proj_123/source/a\x01.txt")


def test_assert_safe_key_blocks_overlong_keys():
    with pytest.raises(UnsafePathError):
        paths.assert_safe_key("projects/p/source/" + "x" * 2000 + ".txt")


# --- file type allowlist ----------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["evil.exe", "script.sh", "payload.php", "noextension", "archive.zip", "a.", ".env"],
)
def test_disallowed_extensions_are_rejected(filename):
    with pytest.raises((UnsupportedFileTypeError, UnsafePathError)):
        paths.classify_extension(filename)


@pytest.mark.parametrize(
    "filename,kind",
    [
        ("clip.MP4", "video"),
        ("photo.JPG", "image"),
        ("voice.wav", "audio"),
        ("subs.srt", "caption"),
        ("spec.json", "text"),
    ],
)
def test_allowed_extensions_are_classified(filename, kind):
    ext, got_kind = paths.classify_extension(filename)
    assert got_kind == kind
    assert ext == ext.lower()


def test_double_extension_uses_the_last_one():
    with pytest.raises(UnsupportedFileTypeError):
        paths.classify_extension("clip.mp4.exe")


# --- correct key construction ----------------------------------------------


def test_asset_key_is_content_addressed():
    key = paths.asset_key("proj_123", "beat_001", SHA, ".mp4")
    assert key == "projects/proj_123/assets/beat_001/a_aaaaaaaaaaaaaaaa.mp4"


def test_same_bytes_produce_the_same_key():
    a = paths.asset_key("proj_123", "beat_001", SHA, ".mp4")
    b = paths.asset_key("proj_123", "beat_001", SHA, ".mp4")
    assert a == b


def test_asset_filename_rejects_bad_hash():
    for bad in ["", "xyz", "A" * 64, "a" * 63, "a" * 65]:
        with pytest.raises(UnsafePathError):
            paths.asset_filename(bad, ".mp4")


def test_render_id_format():
    rid = paths.new_render_id()
    assert rid.startswith("r_")
    assert rid.endswith("Z")
    paths.validate_id(rid, "render_id")  # must survive our own validator


def test_render_keys():
    rid = "r_20260817T1030Z"
    assert paths.render_key("proj_123", rid, "final.mp4") == (
        "projects/proj_123/renders/r_20260817T1030Z/final.mp4"
    )
    assert paths.thumb_key("proj_123", rid) == (
        "projects/proj_123/thumbs/r_20260817T1030Z/thumb.jpg"
    )
    assert paths.manifest_key("proj_123", rid) == (
        "projects/proj_123/manifests/r_20260817T1030Z/manifest.json"
    )


def test_unknown_category_is_rejected():
    with pytest.raises(UnsafePathError):
        paths._category_key("proj_123", "secrets", "a.txt")


def test_workspace_dir_validates_ids():
    with pytest.raises(UnsafePathError):
        paths.workspace_dir("/workspace", "../etc", "r_20260817T1030Z")
