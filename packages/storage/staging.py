"""Media staging.

Two directions:

1. IN  - a provider asset downloaded by the retrieval worker is hashed,
         deduplicated, probed and stored.  (contract §7, §9)
2. OUT - every asset a render needs is copied to local disk before the
         renderer starts, so the Remotion composition never touches the
         network mid-render.  (plan §9, contract §10)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from . import hashing, paths
from .client import MediaStore
from .errors import ObjectNotFoundError, StorageError
from .probe import probe

log = logging.getLogger("staging")

CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


def stage_asset(
    store: MediaStore,
    project_id: str,
    beat_id: str,
    local_path: str,
    original_filename: str,
) -> dict:
    """Hash, dedup, probe and store one downloaded asset.

    Returns the AssetRecord fields this layer owns (contract §9).
    Safe to call twice for the same file: the second call is a no-op.
    """
    paths.validate_id(project_id, "project_id")
    paths.validate_id(beat_id, "beat_id")
    ext, kind = paths.classify_extension(original_filename)
    sha = hashing.hash_file(local_path)
    # Global key: the same bytes get the same key no matter which project or
    # beat asked for them.
    key = paths.asset_key(sha, ext)

    already_present = store.exists(key)
    info = probe(local_path, store.settings.ffprobe_path)

    result = store.put_file(
        key,
        local_path,
        kind=kind,
        content_type=CONTENT_TYPES.get(ext),
    )

    record = {
        "local_uri": f"s3://{result['bucket']}/{key}",
        "storage_key": key,
        "file_hash": sha,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "media_type": info.media_type,
        "width": info.width,
        "height": info.height,
        "duration_s": info.duration_s,
        "size_bytes": result["size_bytes"],
        "deduplicated": already_present,
    }
    log.info(
        "staged asset project=%s beat=%s hash=%s dedup=%s",
        project_id,
        beat_id,
        sha[:16],
        already_present,
    )
    return record


def prepare_render_workspace(
    store: MediaStore,
    project_id: str,
    render_id: str,
    video_spec: dict,
    asset_keys: dict[str, str],
    audio_key: str | None = None,
    caption_key: str | None = None,
) -> dict:
    """Materialise everything a render needs on local disk.

    asset_keys maps beat_id -> storage key.

    If ANY asset is missing, this raises before the renderer is ever started,
    and reports every missing beat at once rather than failing one at a time.
    """
    root = paths.workspace_dir(store.settings.workspace_root, project_id, render_id)
    assets_dir = os.path.join(root, "assets")
    out_dir = os.path.join(root, "out")
    os.makedirs(assets_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    missing: list[str] = []
    local_by_beat: dict[str, str] = {}

    for beat_id, key in asset_keys.items():
        try:
            filename = os.path.basename(key)
            dest = os.path.join(assets_dir, filename)
            if not os.path.exists(dest):
                store.download_to(key, dest)
            local_by_beat[beat_id] = dest
        except ObjectNotFoundError:
            missing.append(beat_id)

    if missing:
        raise StorageError(
            "Cannot start render, assets missing for beats: " + ", ".join(sorted(missing))
        )

    if audio_key:
        dest = os.path.join(root, "audio", os.path.basename(audio_key))
        store.download_to(audio_key, dest)
    if caption_key:
        dest = os.path.join(root, "captions", os.path.basename(caption_key))
        store.download_to(caption_key, dest)

    # Rewrite the spec so every beat points at a local file, not a URL.
    resolved = json.loads(json.dumps(video_spec))
    for beat in resolved.get("beats", []):
        local = local_by_beat.get(beat.get("id"))
        if local:
            beat["local_asset_path"] = local

    spec_path = os.path.join(root, "spec.json")
    with open(spec_path, "w", encoding="utf-8") as handle:
        json.dump(resolved, handle, indent=2)

    log.info("workspace ready %s (%d assets)", root, len(local_by_beat))
    return {
        "workspace": root,
        "spec_path": spec_path,
        "out_dir": out_dir,
        "asset_count": len(local_by_beat),
    }


def package_render(
    store: MediaStore,
    project_id: str,
    render_id: str,
    mp4_path: str,
    thumbnail_path: str | None = None,
    caption_path: str | None = None,
    manifest: dict | None = None,
) -> dict:
    """Upload a finished render as an immutable set of objects."""
    stored: dict[str, str] = {}

    result = store.put_render_file(
        project_id, render_id, "final.mp4", mp4_path, kind="video"
    )
    stored["mp4"] = result["key"]

    if thumbnail_path:
        key = paths.thumb_key(project_id, render_id, "thumb.jpg")
        store.put_file(key, thumbnail_path, kind="image", content_type="image/jpeg")
        stored["thumbnail"] = key

    if caption_path:
        name = os.path.basename(caption_path)
        key = paths.render_key(project_id, render_id, name)
        store.put_file(key, caption_path, kind="caption")
        stored["captions"] = key

    if manifest is not None:
        key = paths.manifest_key(project_id, render_id)
        tmp = os.path.join(store.settings.workspace_root, f"{render_id}_manifest.json")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
        store.put_file(key, tmp, kind="text", content_type="application/json")
        hashing.safe_unlink(tmp)
        stored["manifest"] = key

    log.info("packaged render project=%s render=%s", project_id, render_id)
    return {"render_id": render_id, "objects": stored}
