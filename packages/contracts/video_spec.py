"""
Canonical VideoSpec contract.

Output of the planner (M2). Every beat is the unit approval, retrieval,
and retry operate on. Never mutate this shape based on a specific render
runtime — Remotion/FFmpeg adapters consume this, they do not define it.

Source: docs/PRD.md section 7.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Format:
    width: int = 1080
    height: int = 1920
    fps: int = 30


@dataclass
class Narration:
    provider: str
    voice_id: str


@dataclass
class Beat:
    id: str
    narration: str
    start_s: float
    end_s: float
    visual_intent: str
    search_queries: list[str] = field(default_factory=list)
    shot_type: str = "b_roll"
    asset_id: Optional[str] = None
    overlay: Optional[str] = None
    confidence: Optional[float] = None


@dataclass
class VideoSpec:
    schema_version: str
    project_id: str
    title: str
    format: Format
    language: str
    duration_target_s: float
    brand_profile_id: Optional[str]
    source_policy: str
    narration: Narration
    beats: list[Beat] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "VideoSpec":
        fmt = Format(**d["format"])
        narr = Narration(**d["narration"])
        beats = [Beat(**b) for b in d.get("beats", [])]
        return VideoSpec(
            schema_version=d["schema_version"],
            project_id=d["project_id"],
            title=d["title"],
            format=fmt,
            language=d["language"],
            duration_target_s=d["duration_target_s"],
            brand_profile_id=d.get("brand_profile_id"),
            source_policy=d["source_policy"],
            narration=narr,
            beats=beats,
        )
