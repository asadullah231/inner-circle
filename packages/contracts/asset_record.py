"""
Canonical AssetRecord contract.

Every provider adapter (workers/media/providers/*, landing in M3) must
translate its native response into this shape. Vendor-specific JSON must
never leak past the adapter that produced it.

Source: docs/PRD.md section 7.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class MediaType(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"


class AllowedUse(str, Enum):
    COMMERCIAL = "commercial"
    EDITORIAL_ONLY = "editorial_only"
    ATTRIBUTION_REQUIRED = "attribution_required"
    UNKNOWN = "unknown"


@dataclass
class AssetRecord:
    asset_id: str
    provider: str
    provider_asset_id: str
    source_url: str
    local_uri: Optional[str]

    media_type: MediaType
    width: Optional[int]
    height: Optional[int]
    duration_s: Optional[float]

    license: str
    attribution: Optional[str]
    allowed_use: AllowedUse

    downloaded_at: Optional[str]
    file_hash: Optional[str]
    embedding_uri: Optional[str] = None
    quality_score: Optional[float] = None

    search_query: Optional[str] = None
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["media_type"] = self.media_type.value
        d["allowed_use"] = self.allowed_use.value
        return d
