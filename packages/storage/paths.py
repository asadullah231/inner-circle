"""Object key construction and path safety.

RULE: callers never build keys. They pass IDs, this module builds the key.
Every ID is validated before it is allowed anywhere near a key.

Implements §15 "sanitize every user-provided path" and the path scheme in
docs/storage-contract.md §2.
"""

from __future__ import annotations

import posixpath
import re
from datetime import datetime, timezone

from .errors import UnsafePathError, UnsupportedFileTypeError

# --- identifier rules -------------------------------------------------------

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Characters that must never appear in a key, even after joining.
_FORBIDDEN_SUBSTRINGS = ("..", "//", "\\", "\x00", "\r", "\n")

# --- file type allowlist (contract §4) --------------------------------------

ALLOWED_EXTENSIONS: dict[str, str] = {
    # video
    ".mp4": "video",
    ".mov": "video",
    ".webm": "video",
    ".mkv": "video",
    # image
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    # audio
    ".wav": "audio",
    ".mp3": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    # captions
    ".srt": "caption",
    ".vtt": "caption",
    # text / data
    ".txt": "text",
    ".md": "text",
    ".json": "text",
}

MAX_SIZE_BYTES: dict[str, int] = {
    "video": 2 * 1024**3,
    "image": 50 * 1024**2,
    "audio": 500 * 1024**2,
    "caption": 5 * 1024**2,
    "text": 10 * 1024**2,
}

CATEGORIES = frozenset(
    {"source", "assets", "audio", "captions", "renders", "thumbs", "manifests"}
)


# --- validation -------------------------------------------------------------


def validate_id(value: str, label: str = "id") -> str:
    """Validate a project_id / beat_id / render_id style identifier."""
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise UnsafePathError(
            f"Invalid {label}: must be 1-64 chars of A-Z a-z 0-9 _ - only"
        )
    return value


def classify_extension(filename: str) -> tuple[str, str]:
    """Return (lowercase_extension, kind). Raises if not allowed."""
    if not isinstance(filename, str) or not filename.strip():
        raise UnsafePathError("Filename is empty")

    # Take the basename only. A caller sending "../../etc/passwd" loses the path.
    base = posixpath.basename(filename.replace("\\", "/")).strip()
    if not base or base in {".", ".."}:
        raise UnsafePathError("Filename resolves to no usable name")

    dot = base.rfind(".")
    if dot <= 0:
        raise UnsupportedFileTypeError("Filename has no extension")

    ext = base[dot:].lower()
    kind = ALLOWED_EXTENSIONS.get(ext)
    if kind is None:
        raise UnsupportedFileTypeError(f"Extension not allowed: {ext}")
    return ext, kind


def assert_safe_key(key: str) -> str:
    """Final gate. Nothing leaves this module without passing here."""
    if not key or key.startswith("/"):
        raise UnsafePathError("Key must be relative and non-empty")
    for bad in _FORBIDDEN_SUBSTRINGS:
        if bad in key:
            raise UnsafePathError(f"Key contains forbidden sequence: {bad!r}")
    if any(ord(ch) < 32 for ch in key):
        raise UnsafePathError("Key contains control characters")
    # Normalising must not change the key. If it does, someone tried to escape.
    if posixpath.normpath(key) != key:
        raise UnsafePathError("Key is not in normalised form")
    if len(key) > 1024:
        raise UnsafePathError("Key exceeds 1024 characters")
    return key


# --- key builders -----------------------------------------------------------


def project_prefix(project_id: str) -> str:
    validate_id(project_id, "project_id")
    return assert_safe_key(f"projects/{project_id}")


def _category_key(project_id: str, category: str, *parts: str) -> str:
    if category not in CATEGORIES:
        raise UnsafePathError(f"Unknown category: {category}")
    key = posixpath.join(project_prefix(project_id), category, *parts)
    return assert_safe_key(key)


def asset_filename(sha256_hex: str, extension: str) -> str:
    """Content-addressed name: a_<first 16 hex chars><ext>."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256_hex or ""):
        raise UnsafePathError("sha256 must be 64 lowercase hex characters")
    if extension not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileTypeError(f"Extension not allowed: {extension}")
    return f"a_{sha256_hex[:16]}{extension}"


def asset_key(project_id: str, beat_id: str, sha256_hex: str, extension: str) -> str:
    validate_id(beat_id, "beat_id")
    return _category_key(
        project_id, "assets", beat_id, asset_filename(sha256_hex, extension)
    )


def source_key(project_id: str, filename: str) -> str:
    ext, _kind = classify_extension(filename)
    safe_name = posixpath.basename(filename.replace("\\", "/"))
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", safe_name)
    if not safe_name.lower().endswith(ext):
        safe_name = f"{safe_name}{ext}"
    return _category_key(project_id, "source", safe_name)


def audio_key(project_id: str, filename: str = "narration.wav") -> str:
    classify_extension(filename)
    return _category_key(project_id, "audio", posixpath.basename(filename))


def caption_key(project_id: str, filename: str = "narration.srt") -> str:
    classify_extension(filename)
    return _category_key(project_id, "captions", posixpath.basename(filename))


def new_render_id(now: datetime | None = None) -> str:
    """Renders are immutable; each one gets a fresh UTC-stamped id."""
    now = now or datetime.now(timezone.utc)
    return "r_" + now.strftime("%Y%m%dT%H%M%SZ")


def render_key(project_id: str, render_id: str, filename: str) -> str:
    validate_id(render_id, "render_id")
    classify_extension(filename)
    return _category_key(
        project_id, "renders", render_id, posixpath.basename(filename)
    )


def thumb_key(project_id: str, render_id: str, filename: str = "thumb.jpg") -> str:
    validate_id(render_id, "render_id")
    classify_extension(filename)
    return _category_key(project_id, "thumbs", render_id, posixpath.basename(filename))


def manifest_key(project_id: str, render_id: str) -> str:
    validate_id(render_id, "render_id")
    return _category_key(project_id, "manifests", render_id, "manifest.json")


def render_prefix(project_id: str, render_id: str) -> str:
    validate_id(render_id, "render_id")
    return _category_key(project_id, "renders", render_id)


# --- local workspace (render worker) ---------------------------------------


def workspace_dir(workspace_root: str, project_id: str, render_id: str) -> str:
    """Local staging directory. Same validation as remote keys."""
    validate_id(project_id, "project_id")
    validate_id(render_id, "render_id")
    return posixpath.join(workspace_root, project_id, render_id)
