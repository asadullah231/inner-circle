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
import tempfile
from datetime import datetime, timezone

from . import hashing, paths
from .client import MediaStore
from .errors import (
    ImmutableRenderError,
    ObjectNotFoundError,
    StorageError,
    UnrecoverableRenderError,
)
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

    def _fetch(key: str, dest: str) -> bool:
        """Download key to dest unless it is already there. Returns False if the
        object does not exist, so the caller can record the miss rather than
        failing one input at a time."""
        try:
            if not os.path.exists(dest):
                store.download_to(key, dest)
            return True
        except ObjectNotFoundError:
            return False

    for beat_id, key in asset_keys.items():
        dest = os.path.join(assets_dir, os.path.basename(key))
        if _fetch(key, dest):
            local_by_beat[beat_id] = dest
        else:
            missing.append(beat_id)

    # Audio and captions are staged the same way, and a missing narration is
    # just as fatal as a missing clip — the audio waveform is the timing
    # authority (plan §8). Check them in the SAME pre-flight so one failure
    # names everything that is missing, instead of the beats being validated
    # here and a missing audio blowing up separately once the renderer has
    # already started staging.
    if audio_key:
        dest = os.path.join(root, "audio", os.path.basename(audio_key))
        if not _fetch(audio_key, dest):
            missing.append("audio")
    if caption_key:
        dest = os.path.join(root, "captions", os.path.basename(caption_key))
        if not _fetch(caption_key, dest):
            missing.append("captions")

    if missing:
        raise StorageError(
            "Cannot start render, missing required inputs: " + ", ".join(sorted(missing))
        )

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
    """Upload a finished render as an immutable set of objects.

    Two-phase publish: every file is uploaded to a .staging/ prefix first, then
    copied to its final path in one publish step.  The manifest is copied last
    and acts as the commit marker — a render is considered published if and only
    if its manifest exists at the final path.  A crash mid-publish leaves
    staging files in place for diagnosis and recovery (see resume_publish).
    """
    paths.validate_id(project_id, "project_id")
    paths.validate_id(render_id, "render_id")

    # --- early guard: has this render already been published? ----------------
    final_mp4_key = paths.render_key(project_id, render_id, "final.mp4")
    if store.exists(final_mp4_key):
        raise ImmutableRenderError(
            f"Render already published, cannot overwrite: {final_mp4_key}"
        )

    # --- phase 1: upload everything to .staging/ ----------------------------
    staged: dict[str, str] = {}  # label -> staging key

    staging_mp4 = paths.render_staging_key(project_id, render_id, "final.mp4")
    store.put_file(staging_mp4, mp4_path, kind="video", overwrite=True)
    staged["mp4"] = staging_mp4

    if thumbnail_path:
        staging_thumb = paths.render_staging_key(project_id, render_id, "thumb.jpg")
        store.put_file(
            staging_thumb, thumbnail_path, kind="image",
            content_type="image/jpeg", overwrite=True,
        )
        staged["thumbnail"] = staging_thumb

    if caption_path:
        name = os.path.basename(caption_path)
        staging_cap = paths.render_staging_key(project_id, render_id, name)
        store.put_file(staging_cap, caption_path, kind="caption", overwrite=True)
        staged["captions"] = staging_cap

    if manifest is not None:
        staging_manifest = paths.render_staging_key(
            project_id, render_id, "manifest.json"
        )
        # Write the manifest to a system temp file, not to WORKSPACE_ROOT.
        # The workspace is owned by the render worker; package_render should
        # not depend on it being writable.
        fd, tmp = tempfile.mkstemp(prefix="avg_manifest_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2)
            store.put_file(
                staging_manifest, tmp, kind="text",
                content_type="application/json", overwrite=True,
            )
        finally:
            hashing.safe_unlink(tmp)
        staged["manifest"] = staging_manifest

    # --- phase 2: publish (copy staging -> final, then delete staging) -------
    return _publish_render(store, project_id, render_id, staged)


# Maps staging labels to (final_key_builder, args_for_builder) so the publish
# step can be driven by a loop rather than per-file if-branches.
_FINAL_KEY_BUILDERS = {
    "mp4": lambda pid, rid, _skey: paths.render_key(pid, rid, "final.mp4"),
    "thumbnail": lambda pid, rid, _skey: paths.thumb_key(pid, rid, "thumb.jpg"),
    "captions": lambda pid, rid, skey: paths.render_key(
        pid, rid, os.path.basename(skey)
    ),
    "manifest": lambda pid, rid, _skey: paths.manifest_key(pid, rid),
}

# The publish order.  Manifest is LAST — it is the commit marker.
_PUBLISH_ORDER = ("mp4", "thumbnail", "captions", "manifest")


def _publish_render(
    store: MediaStore,
    project_id: str,
    render_id: str,
    staged: dict[str, str],
    *,
    _resuming: bool = False,
) -> dict:
    """Copy staged files to final paths and delete the staging copies.

    The manifest is copied last so its presence at the final path is the commit
    marker for a fully-published render.

    When ``_resuming`` is True (called from ``resume_publish``), the
    immutability guard is skipped and files that already exist at their final
    path are accepted rather than treated as a conflict — the crash that
    triggered the resume may have already copied some files before dying.

    NOTE on concurrency: the re-check of the final MP4 key immediately before
    the first copy shrinks the TOCTOU window but does not close it completely —
    two callers could both pass the check and race through the copy sequence.
    S3/MinIO has no conditional-write primitive for copy_object, so true mutual
    exclusion requires an external lock (DB advisory lock, distributed lock
    service).  The orchestration layer is responsible for not dispatching the
    same render_id to two workers; this re-check is belt-and-braces.
    """
    final_mp4_key = paths.render_key(project_id, render_id, "final.mp4")

    if not _resuming:
        # Belt-and-braces: re-check immediately before the first copy.
        if store.exists(final_mp4_key):
            # Another caller published while we were staging. Clean up and fail.
            for staging_key in staged.values():
                store.remove_object(staging_key)
            raise ImmutableRenderError(
                f"Render was published concurrently: {final_mp4_key}"
            )

    published: dict[str, str] = {}  # label -> final key

    for label in _PUBLISH_ORDER:
        staging_key = staged.get(label)
        if staging_key is None:
            continue
        builder = _FINAL_KEY_BUILDERS[label]
        final_key = builder(project_id, render_id, staging_key)
        if _resuming and store.exists(final_key):
            # Already copied before the crash — just clean up the staging copy.
            store.remove_object(staging_key)
        else:
            store.copy_object(staging_key, final_key)
            store.remove_object(staging_key)
        published[label] = final_key

    log.info("packaged render project=%s render=%s", project_id, render_id)
    return {"render_id": render_id, "objects": published}


def resume_publish(
    store: MediaStore,
    project_id: str,
    render_id: str,
    max_staging_age_hours: float = 24.0,
) -> dict:
    """Complete a partially-published render, or roll it back.

    Operator-only recovery tool — not called automatically, not wired to an
    HTTP endpoint.  Same reason retention.run_cleanup defaults to dry_run=True:
    automating destructive behaviour before we have DB-authoritative state is
    how you lose data.

    Three scenarios:

    1. **Manifest at staging, not at final:** the publish crashed mid-copy.
       Complete it — copy each staging file that hasn't been published yet,
       then clean up staging.

    2. **No manifest anywhere (staging or final) AND staging is older than
       max_staging_age_hours:** the upload itself was interrupted.  The render
       is unrecoverable — clean up staging and raise UnrecoverableRenderError.

    3. **Manifest already at final:** nothing to do — the render is published.
       Return the existing objects.
    """
    paths.validate_id(project_id, "project_id")
    paths.validate_id(render_id, "render_id")

    final_manifest = paths.manifest_key(project_id, render_id)
    staging_manifest = paths.render_staging_key(
        project_id, render_id, "manifest.json"
    )

    manifest_published = store.exists(final_manifest)
    manifest_staged = store.exists(staging_manifest)

    log.warning(
        "resume_publish invoked project=%s render=%s "
        "manifest_published=%s manifest_staged=%s",
        project_id, render_id, manifest_published, manifest_staged,
    )

    # Scenario 3: already fully published.
    if manifest_published:
        log.warning("render already published, nothing to resume")
        published: dict[str, str] = {"manifest": final_manifest}
        # Collect whichever final files exist.
        for label, builder in _FINAL_KEY_BUILDERS.items():
            if label == "manifest":
                continue
            # Use a dummy staging key — only "captions" uses it for the
            # basename, and we fall back to a common name.
            candidate = builder(project_id, render_id, "narration.srt")
            if store.exists(candidate):
                published[label] = candidate
        return {"render_id": render_id, "objects": published}

    # Scenario 2: no manifest anywhere — check age.
    if not manifest_staged:
        # Check whether any staging files exist at all.
        staging_mp4 = paths.render_staging_key(
            project_id, render_id, "final.mp4"
        )
        if store.exists(staging_mp4):
            info = store.stat(staging_mp4)
            lm = info.get("last_modified")
            if lm is not None:
                from datetime import timezone as _tz

                now = datetime.now(_tz.utc)
                if isinstance(lm, str):
                    lm = datetime.fromisoformat(lm)
                hours_old = (now - lm).total_seconds() / 3600.0
                if hours_old > max_staging_age_hours:
                    # Clean up orphaned staging files.
                    _cleanup_staging(store, project_id, render_id)
                    raise UnrecoverableRenderError(
                        f"Staging files for render {render_id} are "
                        f"{hours_old:.1f}h old with no manifest — "
                        "the render must be re-run with a new render_id"
                    )
            # Staging exists but is too young — tell the operator to wait.
            raise StorageError(
                f"Staging files exist for render {render_id} but the manifest "
                "was never uploaded. Wait for the staging age threshold, or "
                "re-run the render with a new render_id."
            )
        # Nothing staged at all — nothing to do.
        raise StorageError(
            f"No staged or published files found for render {render_id}"
        )

    # Scenario 1: manifest staged but not published — complete the publish.
    # _resuming=True so the immutability guard is skipped and files that were
    # already copied before the crash are accepted rather than conflicting.
    staged = _discover_staged(store, project_id, render_id)
    return _publish_render(store, project_id, render_id, staged, _resuming=True)


def _discover_staged(
    store: MediaStore, project_id: str, render_id: str
) -> dict[str, str]:
    """Build a staged dict from what exists at the staging prefix."""
    candidates = {
        "mp4": paths.render_staging_key(project_id, render_id, "final.mp4"),
        "thumbnail": paths.render_staging_key(project_id, render_id, "thumb.jpg"),
        "manifest": paths.render_staging_key(
            project_id, render_id, "manifest.json"
        ),
    }
    # Captions could be .srt or .vtt — check both.
    for ext in (".srt", ".vtt"):
        cap_key = paths.render_staging_key(
            project_id, render_id, f"narration{ext}"
        )
        if store.exists(cap_key):
            candidates["captions"] = cap_key
            break

    return {
        label: key for label, key in candidates.items() if store.exists(key)
    }


def _cleanup_staging(
    store: MediaStore, project_id: str, render_id: str
) -> None:
    """Remove all known staging files for a render."""
    for label, key in _discover_staged(store, project_id, render_id).items():
        store.remove_object(key)
        log.warning("cleaned up staging file %s (%s)", key, label)
