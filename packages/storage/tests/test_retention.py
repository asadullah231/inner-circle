"""Retention policy tests.

These cover the decision logic only — no MinIO, no network.
The most important tests here are the ones proving that finished renders
can never be deleted.
"""

from datetime import datetime, timedelta, timezone

import pytest

from packages.storage import retention
from packages.storage.retention import RetentionPolicy

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
POLICY = RetentionPolicy()


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


# --- category detection -----------------------------------------------------


def hours_ago(n: float) -> datetime:
    return NOW - timedelta(hours=n)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("assets/9f/a_9f2c4e1b7d0a5c33.mp4", "assets"),
        ("projects/p1/renders/r_20260819T1200Z/final.mp4", "renders"),
        ("projects/p1/source/script.txt", "source"),
        ("projects/p1/audio/narration.wav", "audio"),
        ("projects/p1/manifests/r_1/manifest.json", "manifests"),
        ("projects/p1/renders/r_1/.staging/final.mp4", "render_staging"),
        ("projects/p1/renders/r_1/.staging/thumb.jpg", "render_staging"),
        ("projects/p1/renders/r_1/.staging/manifest.json", "render_staging"),
        ("not-a-project-key", None),
        ("projects/p1", None),
        ("assets/9f", None),
        ("", None),
    ],
)
def test_category_of(key, expected):
    assert retention.category_of(key) == expected


# --- protection -------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "projects/p1/renders/r_1/final.mp4",
        "projects/p1/thumbs/r_1/thumb.jpg",
        "projects/p1/manifests/r_1/manifest.json",
        "projects/p1/captions/narration.srt",
    ],
)
def test_finished_output_is_protected(key):
    assert retention.is_protected(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "assets/9f/a_9f2c4e1b7d0a5c33.mp4",
        "projects/p1/source/script.txt",
        "projects/p1/audio/narration.wav",
    ],
)
def test_working_files_are_cleanable(key):
    assert retention.is_protected(key) is False


@pytest.mark.parametrize(
    "key",
    ["random/thing.mp4", "projects/p1/unknowncategory/x.mp4", "", "projects", "assets/9f"],
)
def test_unrecognised_keys_fail_closed(key):
    """Anything we do not recognise is protected, never deleted."""
    assert retention.is_protected(key) is True


def test_protected_key_is_never_deleted_even_when_ancient():
    key = "projects/p1/renders/r_1/final.mp4"
    assert (
        retention.should_delete(key, days_ago(9999), NOW, POLICY, frozenset()) is False
    )


# --- age and in-use ---------------------------------------------------------


def test_old_unused_asset_is_deleted():
    key = "assets/9f/a_9f2c4e1b7d0a5c33.mp4"
    assert retention.should_delete(key, days_ago(31), NOW, POLICY, frozenset()) is True


def test_recent_asset_is_kept():
    key = "assets/9f/a_9f2c4e1b7d0a5c33.mp4"
    assert retention.should_delete(key, days_ago(29), NOW, POLICY, frozenset()) is False


def test_asset_exactly_at_boundary_is_kept():
    key = "assets/9f/a_9f2c4e1b7d0a5c33.mp4"
    assert retention.should_delete(key, days_ago(30), NOW, POLICY, frozenset()) is False


def test_in_use_asset_is_kept_however_old():
    key = "assets/9f/a_9f2c4e1b7d0a5c33.mp4"
    assert (
        retention.should_delete(key, days_ago(500), NOW, POLICY, frozenset({key}))
        is False
    )


def test_naive_timestamp_is_treated_as_utc():
    naive = datetime(2026, 7, 1, 12, 0, 0)  # no tzinfo
    assert retention.age_days(naive, NOW) == pytest.approx(49.0, abs=0.01)


# --- selection --------------------------------------------------------------


def test_select_expired_picks_only_eligible_keys():
    objects = [
        ("assets/11/a_1111111111111111.mp4", days_ago(60)),
        ("assets/22/a_2222222222222222.mp4", days_ago(2)),
        ("projects/p1/renders/r_1/final.mp4", days_ago(400)),
        ("assets/33/a_3333333333333333.mp4", days_ago(90)),
    ]
    result = retention.select_expired(
        objects, POLICY, in_use_keys=["assets/33/a_3333333333333333.mp4"], now=NOW
    )
    assert result == ["assets/11/a_1111111111111111.mp4"]


def test_select_expired_respects_the_cap():
    policy = RetentionPolicy(max_deletions_per_run=3)
    objects = [
        (f"assets/{i:02d}/a_{i:016d}.mp4", days_ago(100)) for i in range(50)
    ]
    result = retention.select_expired(objects, policy, now=NOW)
    assert len(result) == 3


def test_empty_input_returns_empty():
    assert retention.select_expired([], POLICY, now=NOW) == []


def test_policy_defaults_match_approved_values():
    p = RetentionPolicy()
    assert p.tmp_days == 1
    assert p.uploads_days == 7
    assert p.asset_days == 30
    assert p.render_staging_hours == 24


# --- render staging retention -----------------------------------------------


def test_staging_key_is_recognised_as_render_staging():
    key = "projects/p1/renders/r_1/.staging/final.mp4"
    assert retention.category_of(key) == "render_staging"


def test_staging_keys_are_cleanable_not_protected():
    key = "projects/p1/renders/r_1/.staging/final.mp4"
    assert retention.is_protected(key) is False


def test_staging_uses_hour_based_rule_not_day_based():
    key = "projects/p1/renders/r_1/.staging/final.mp4"
    # 25 hours old, above the 24h default — should be deleted
    assert retention.should_delete(key, hours_ago(25), NOW, POLICY, frozenset()) is True
    # 23 hours old, below the threshold — should be kept
    assert retention.should_delete(key, hours_ago(23), NOW, POLICY, frozenset()) is False


def test_staging_at_exact_boundary_is_kept():
    key = "projects/p1/renders/r_1/.staging/final.mp4"
    assert retention.should_delete(key, hours_ago(24), NOW, POLICY, frozenset()) is False


def test_finished_render_is_still_protected_after_staging_change():
    """Adding the staging category must not weaken protection of published renders."""
    key = "projects/p1/renders/r_1/final.mp4"
    assert retention.is_protected(key) is True
    assert retention.should_delete(key, days_ago(9999), NOW, POLICY, frozenset()) is False


def test_staged_asset_key_is_still_cleanable_at_30_days():
    """Adding staging must not break the existing asset cleanup path."""
    key = "assets/9f/a_9f2c4e1b7d0a5c33.mp4"
    assert retention.should_delete(key, days_ago(31), NOW, POLICY, frozenset()) is True
