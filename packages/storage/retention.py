"""Retention and cleanup.

Approved retention policy:

    temp files          1 day
    raw uploads         7 days
    source media        30 days after last use
    renders/thumbs      permanent, never auto-deleted
    manifests/captions  permanent, never auto-deleted

Two mechanisms:

1. Bucket lifecycle rules on the tmp and uploads buckets. The object store
   enforces these itself, so nothing has to be running for them to work.

2. A cleanup job for staged assets. These cannot be a plain lifecycle rule
   because the rule is "30 days after last use", not "30 days after upload" —
   a clip used again last week must not be deleted just because it was
   downloaded two months ago.

Safety rules, in order of importance:

    * Renders, thumbnails, manifests and captions are NEVER deleted here.
      They are protected by prefix, checked before anything else.
    * The job is dry-run by default. Deleting requires an explicit flag.
    * Real deletions (dry_run=False) require a DB connection so the protected
      set is built from the database, not from a caller-supplied list.
    * A single run will not delete more than max_deletions objects, so a bad
      in-use list cannot wipe the bucket in one go.

Race window (DB-authoritative mode):

    There is a narrow race between reading the protected set from the database
    and scanning the object store.  An asset could be dereferenced (beat
    reassigned, last_used_at aged past the window) between the DB query and
    the S3 scan, causing it to be deleted in the same run where it stopped
    being protected.

    This is accepted because:
      1. The 30-day retention window means the asset must have been unused for
         30 days AND dereferenced in the last few seconds — an extremely narrow
         overlap for a 30-day-old asset.
      2. Assets are content-addressed: re-downloading the same file from the
         provider produces the same key, so a "lost" asset is recoverable.
      3. Closing this race completely would require a transactional lock
         spanning both Postgres and S3/MinIO, which neither supports atomically.

    run_cleanup logs every deleted key at INFO level before removing it, so
    any accidental deletion is traceable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

log = logging.getLogger("retention")

# Categories under projects/{id}/ that must never be auto-deleted.
PROTECTED_CATEGORIES = frozenset({"renders", "thumbs", "manifests", "captions"})

# Categories the cleanup job is allowed to consider.
# "assets" here refers to the global assets/ prefix, not a project subfolder.
# "render_staging" covers the .staging/ prefix under a render — crash residue
# that is safe and correct to clean up after 24 hours.
CLEANABLE_CATEGORIES = frozenset({"assets", "source", "audio", "render_staging"})


@dataclass(frozen=True)
class RetentionPolicy:
    tmp_days: int = 1
    uploads_days: int = 7
    asset_days: int = 30
    render_staging_hours: int = 24
    max_deletions_per_run: int = 500

    @classmethod
    def from_settings(cls, settings) -> "RetentionPolicy":
        return cls(
            tmp_days=getattr(settings, "retention_tmp_days", 1),
            uploads_days=getattr(settings, "retention_uploads_days", 7),
            asset_days=getattr(settings, "retention_asset_days", 30),
            render_staging_hours=getattr(settings, "retention_render_staging_hours", 24),
            max_deletions_per_run=getattr(settings, "retention_max_deletions", 500),
        )


# --- pure helpers (no network, fully unit-testable) -------------------------


def category_of(key: str) -> Optional[str]:
    """Classify a storage key, or return None if the shape is unrecognised.

    assets/9f/a_9f2c4e1b.mp4                                 ->  "assets"
    projects/proj_123/renders/r_1/final.mp4                   ->  "renders"
    projects/proj_123/renders/r_1/.staging/final.mp4          ->  "render_staging"

    Assets are global rather than project-scoped, because dedup is global.
    Everything else lives under a project.
    """
    if not key:
        return None
    parts = key.split("/")

    if parts[0] == "assets":
        # assets/<shard>/<filename>
        return "assets" if len(parts) == 3 else None

    if parts[0] == "projects" and len(parts) >= 4:
        # Detect .staging/ under a render prefix:
        # projects/{id}/renders/{render_id}/.staging/{filename}
        if (
            len(parts) >= 6
            and parts[2] == "renders"
            and parts[4] == ".staging"
        ):
            return "render_staging"
        return parts[2]

    return None


def is_protected(key: str) -> bool:
    """True if this object must never be deleted by the cleanup job.

    Anything we do not positively recognise as cleanable is treated as
    protected. Failing closed is the right default when the alternative is
    deleting someone's finished video.
    """
    category = category_of(key)
    if category is None:
        return True
    if category in PROTECTED_CATEGORIES:
        return True
    return category not in CLEANABLE_CATEGORIES


def age_days(last_modified: datetime, now: datetime) -> float:
    """Age of an object in days. Both timestamps must be timezone-aware."""
    if last_modified.tzinfo is None:
        last_modified = last_modified.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - last_modified).total_seconds() / 86400.0


def age_hours(last_modified: datetime, now: datetime) -> float:
    """Age of an object in hours. Both timestamps must be timezone-aware."""
    if last_modified.tzinfo is None:
        last_modified = last_modified.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - last_modified).total_seconds() / 3600.0


def should_delete(
    key: str,
    last_modified: datetime,
    now: datetime,
    policy: RetentionPolicy,
    in_use_keys: frozenset[str],
) -> bool:
    """Decide whether one object is eligible for deletion.

    Deletion requires all three:
      * the key is not protected
      * the key is not currently referenced by any project
      * the object is older than the retention window

    Render staging files use an hour-based window (default 24 h) — they are
    crash residue, not user data, and should not linger for 30 days.
    """
    if is_protected(key):
        return False
    if key in in_use_keys:
        return False
    category = category_of(key)
    if category == "render_staging":
        return age_hours(last_modified, now) > policy.render_staging_hours
    return age_days(last_modified, now) > policy.asset_days


def select_expired(
    objects: Iterable[tuple[str, datetime]],
    policy: RetentionPolicy,
    in_use_keys: Iterable[str] = (),
    now: Optional[datetime] = None,
) -> list[str]:
    """Given (key, last_modified) pairs, return the keys eligible for deletion.

    The result is capped at policy.max_deletions_per_run.
    """
    now = now or datetime.now(timezone.utc)
    in_use = frozenset(in_use_keys)
    selected: list[str] = []
    for key, last_modified in objects:
        if len(selected) >= policy.max_deletions_per_run:
            log.warning(
                "cleanup hit the per-run cap of %d, stopping scan",
                policy.max_deletions_per_run,
            )
            break
        if should_delete(key, last_modified, now, policy, in_use):
            selected.append(key)
    return selected


# --- object-store side ------------------------------------------------------


def build_lifecycle_config(days: int):
    """Build a whole-bucket expiry rule. Imported lazily so the pure helpers
    above can be tested without the minio package installed."""
    from minio.commonconfig import ENABLED, Filter
    from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

    return LifecycleConfig(
        [
            Rule(
                ENABLED,
                rule_filter=Filter(prefix=""),
                rule_id=f"expire-after-{days}d",
                expiration=Expiration(days=days),
            )
        ]
    )


def apply_lifecycle_rules(store, policy: RetentionPolicy) -> dict:
    """Set expiry rules on the tmp and uploads buckets.

    The media bucket deliberately gets no blanket rule — finished renders live
    there and must not expire.
    """
    applied = {}
    targets = [
        (store.settings.s3_tmp_bucket, policy.tmp_days),
        (store.settings.s3_uploads_bucket, policy.uploads_days),
    ]
    for bucket, days in targets:
        store.client.set_bucket_lifecycle(bucket, build_lifecycle_config(days))
        applied[bucket] = days
        log.info("lifecycle rule set on %s: expire after %d days", bucket, days)
    return applied


def list_cleanable_objects(store) -> list[tuple[str, datetime]]:
    """List every object in the media bucket that the cleanup job may consider."""
    results: list[tuple[str, datetime]] = []
    # Two prefixes now: global assets, and per-project working files.
    for prefix in ("assets/", "projects/"):
        for obj in store.client.list_objects(
            store.settings.s3_bucket, prefix=prefix, recursive=True
        ):
            if is_protected(obj.object_name):
                continue
            results.append((obj.object_name, obj.last_modified))
    return results


def build_protected_set(conn, policy: RetentionPolicy) -> frozenset[str]:
    """Build the DB-authoritative set of asset keys that must not be deleted.

    Unions two sources from the database:
      1. In-use: assets currently referenced by at least one beat
      2. Recently-used: assets with last_used_at within policy.asset_days

    Render, thumbnail, manifest, and caption keys are not included — they are
    already protected by is_protected() via their key prefix, independently of
    the in-use set.

    This replaces the caller-supplied in_use_keys list.  Using the DB means
    the protected set is always complete and current (within the race window
    documented at module level).
    """
    from . import db

    in_use = db.list_in_use_asset_keys(conn)
    recently_used = db.list_recently_used_asset_keys(conn, days=policy.asset_days)

    combined = frozenset(in_use) | frozenset(recently_used)
    log.info(
        "protected set: %d in-use + %d recently-used = %d unique keys",
        len(in_use), len(recently_used), len(combined),
    )
    return combined


def run_cleanup(
    store,
    policy: RetentionPolicy,
    in_use_keys: Iterable[str] = (),
    dry_run: bool = True,
    now: Optional[datetime] = None,
    conn=None,
) -> dict:
    """Find expired assets and, unless this is a dry run, delete them.

    When ``conn`` is provided (DB-authoritative mode), the protected set is
    built from the database via build_protected_set() and ``in_use_keys`` is
    ignored.  When ``conn`` is None, ``in_use_keys`` is used as-is.

    Real deletions (dry_run=False) require a DB connection so the protected
    set is always complete and current.  Without one, the function raises —
    an incomplete caller-supplied list could delete assets still in use.
    """
    if not dry_run and conn is None:
        raise ValueError(
            "run_cleanup(dry_run=False) requires a DB connection (conn=...) "
            "so the protected set is built from the database. "
            "Pass conn or use dry_run=True."
        )

    if conn is not None:
        protected = build_protected_set(conn, policy)
    else:
        protected = frozenset(in_use_keys)

    candidates = list_cleanable_objects(store)
    expired = select_expired(candidates, policy, protected, now)

    deleted: list[str] = []
    failed: list[str] = []

    if not dry_run:
        for key in expired:
            # Re-check immediately before deleting. Cheap, and it means a bug
            # in the scan cannot delete a protected object.
            if is_protected(key):
                log.error("refusing to delete protected key: %s", key)
                continue
            log.info("retention deleting %s", key)
            try:
                store.client.remove_object(store.settings.s3_bucket, key)
                deleted.append(key)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
                failed.append(key)
                log.warning("retention failed to delete %s: %s", key, exc)

    return {
        "dry_run": dry_run,
        "scanned": len(candidates),
        "expired": len(expired),
        "deleted": len(deleted),
        "failed": len(failed),
        "keys": expired if dry_run else deleted,
        "capped": len(expired) >= policy.max_deletions_per_run,
    }
