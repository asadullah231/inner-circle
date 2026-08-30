"""
M0 demo — NOT a video pipeline (that starts at M4).
Shows the frozen VideoSpec / AssetRecord contracts actually working:
build a spec, round-trip it through dict (the shape that will move
through Postgres/API/queue from M1 onward), attach an asset record.
"""

from packages.contracts.asset_record import AllowedUse, AssetRecord, MediaType
from packages.contracts.video_spec import Beat, Format, Narration, VideoSpec

spec = VideoSpec(
    schema_version="1.0",
    project_id="proj_demo_001",
    title="Why Cats Purr",
    format=Format(width=1080, height=1920, fps=30),
    language="en-US",
    duration_target_s=30,
    brand_profile_id="brand_default",
    source_policy="licensed_stock_then_internal_then_generated",
    narration=Narration(provider="elevenlabs", voice_id="voice_demo"),
    beats=[
        Beat(
            id="beat_001",
            narration="Cats purr at a frequency that can heal bone.",
            start_s=0.0,
            end_s=4.5,
            visual_intent="close-up cat purring, soft light, shallow depth of field",
            search_queries=["cat purring close up", "cat resting sunlight"],
            shot_type="b_roll",
        ),
        Beat(
            id="beat_002",
            narration="It is not just comfort. It is a survival tool.",
            start_s=4.5,
            end_s=8.0,
            visual_intent="cat stretching after injury, recovery, calm environment",
            search_queries=["cat stretching", "cat recovery calm"],
            shot_type="b_roll",
        ),
    ],
)

print("=== VideoSpec (planner output shape) ===")
print(f"project: {spec.title} ({spec.project_id})")
print(f"format: {spec.format.width}x{spec.format.height} @ {spec.format.fps}fps")
print(f"beats: {len(spec.beats)}")
for b in spec.beats:
    print(f"  [{b.id}] {b.start_s}s-{b.end_s}s :: {b.narration}")
    print(f"           visual_intent: {b.visual_intent}")

print("\n=== Round-trip through dict (Postgres/API/queue shape from M1) ===")
d = spec.to_dict()
restored = VideoSpec.from_dict(d)
assert restored.beats[0].id == "beat_001"
print("round-trip OK — same beat IDs, same timing, nothing lost")

print("\n=== AssetRecord (what M3's media worker will attach per beat) ===")
asset = AssetRecord(
    asset_id="pexels_4210842",
    provider="pexels",
    provider_asset_id="4210842",
    source_url="https://www.pexels.com/video/4210842",
    local_uri="/data/assets/pexels_4210842.mp4",
    media_type=MediaType.VIDEO,
    width=1920,
    height=1080,
    duration_s=8.2,
    license="Pexels License",
    attribution="Video by Example Creator on Pexels",
    allowed_use=AllowedUse.COMMERCIAL,
    downloaded_at="2026-08-17T12:00:00Z",
    file_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4",
    search_query="cat purring close up",
)
print(f"  asset for beat_001: {asset.provider}/{asset.provider_asset_id}")
print(f"  license: {asset.license} | allowed_use: {asset.allowed_use.value}")
print(f"  rights record complete: "
      f"{all([asset.license, asset.allowed_use, asset.downloaded_at, asset.file_hash])}")

print("\nThis is what M0 actually is: the shape everything downstream will move")
print("through. A real end-to-end demo (brief -> approved script) lands at M2;")
print("a rendered MP4 lands at M4.")
