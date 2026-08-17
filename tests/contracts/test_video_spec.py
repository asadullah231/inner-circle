import unittest

from packages.contracts.asset_record import AllowedUse, AssetRecord, MediaType
from packages.contracts.video_spec import Beat, Format, Narration, VideoSpec


class TestVideoSpecRoundTrip(unittest.TestCase):
    def test_to_dict_and_back(self):
        spec = VideoSpec(
            schema_version="1.0",
            project_id="proj_123",
            title="How Cats Hear the World",
            format=Format(width=1080, height=1920, fps=30),
            language="en-US",
            duration_target_s=45,
            brand_profile_id="brand_documentary_dark",
            source_policy="licensed_stock_then_internal_then_generated",
            narration=Narration(provider="elevenlabs", voice_id="voice_01"),
            beats=[
                Beat(
                    id="beat_001",
                    narration="A cat hears sounds far beyond our range.",
                    start_s=0.0,
                    end_s=3.2,
                    visual_intent="cat listening, close-up ears, natural light",
                    search_queries=["cat listening", "cat ears close-up"],
                )
            ],
        )
        d = spec.to_dict()
        restored = VideoSpec.from_dict(d)
        self.assertEqual(restored.project_id, "proj_123")
        self.assertEqual(len(restored.beats), 1)
        self.assertEqual(restored.beats[0].id, "beat_001")
        self.assertEqual(restored.format.width, 1080)

    def test_beat_defaults(self):
        beat = Beat(
            id="b1", narration="x", start_s=0, end_s=1, visual_intent="y"
        )
        self.assertEqual(beat.search_queries, [])
        self.assertEqual(beat.shot_type, "b_roll")
        self.assertIsNone(beat.asset_id)


class TestAssetRecord(unittest.TestCase):
    def test_to_dict_serializes_enums(self):
        record = AssetRecord(
            asset_id="pexels_123",
            provider="pexels",
            provider_asset_id="123",
            source_url="https://pexels.com/video/123",
            local_uri="/data/pexels_123.mp4",
            media_type=MediaType.VIDEO,
            width=1920,
            height=1080,
            duration_s=12.5,
            license="Pexels License",
            attribution="Video by X on Pexels",
            allowed_use=AllowedUse.COMMERCIAL,
            downloaded_at="2026-08-17T00:00:00Z",
            file_hash="abc123",
        )
        d = record.to_dict()
        self.assertEqual(d["media_type"], "video")
        self.assertEqual(d["allowed_use"], "commercial")
        self.assertEqual(d["provider"], "pexels")


if __name__ == "__main__":
    unittest.main()
