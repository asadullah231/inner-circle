"""ffprobe wrapper.

Fills the AssetRecord fields this layer owns (contract §9):
media_type, width, height, duration_s.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass

from .errors import ProbeError

PROBE_TIMEOUT_S = 60


@dataclass
class MediaInfo:
    media_type: str  # "video" | "audio" | "image" | "unknown"
    duration_s: float | None
    width: int | None
    height: int | None
    codec: str | None
    has_audio: bool
    container: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def probe(path: str, ffprobe_path: str = "ffprobe") -> MediaInfo:
    """Run ffprobe and return normalised metadata."""
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProbeError(
            "ffprobe binary not found. Set FFPROBE_PATH or install ffmpeg."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("ffprobe timed out") from exc

    if result.returncode != 0:
        raise ProbeError(f"ffprobe failed: {result.stderr.strip()[:500]}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe returned invalid JSON") from exc

    return _normalise(data)


def _normalise(data: dict) -> MediaInfo:
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _to_float(fmt.get("duration"))
    if duration is None and video is not None:
        duration = _to_float(video.get("duration"))

    if video is not None:
        # A single-frame "video" stream is really a still image.
        nb_frames = _to_int(video.get("nb_frames"))
        is_image = nb_frames == 1 or (duration is not None and duration < 0.04)
        media_type = "image" if is_image else "video"
    elif audio is not None:
        media_type = "audio"
    else:
        media_type = "unknown"

    return MediaInfo(
        media_type=media_type,
        duration_s=round(duration, 3) if duration is not None else None,
        width=_to_int(video.get("width")) if video else None,
        height=_to_int(video.get("height")) if video else None,
        codec=video.get("codec_name") if video else (audio.get("codec_name") if audio else None),
        has_audio=audio is not None,
        container=fmt.get("format_name"),
    )


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
