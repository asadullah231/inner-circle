"""SHA-256 hashing for deduplication (contract §7).

Files can be 2 GB. Everything here streams; nothing loads a whole file into
memory.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from typing import BinaryIO, Iterator

CHUNK_SIZE = 1024 * 1024  # 1 MiB


def hash_file(path: str) -> str:
    """SHA-256 of a file on disk, as 64 lowercase hex characters."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_stream(stream: BinaryIO) -> str:
    """SHA-256 of an open binary stream, from its current position."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
        digest.update(chunk)
    return digest.hexdigest()


def spool_and_hash(chunks: Iterator[bytes], tmp_dir: str | None = None) -> tuple[str, str, int]:
    """Write an incoming byte stream to a temp file while hashing it.

    Returns (temp_path, sha256_hex, size_bytes).

    The caller is responsible for deleting temp_path once the object has been
    uploaded or discarded.
    """
    digest = hashlib.sha256()
    size = 0
    fd, temp_path = tempfile.mkstemp(prefix="avg_upload_", dir=tmp_dir)
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                if not chunk:
                    continue
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
    except Exception:
        # Never leave a partial file behind on failure.
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return temp_path, digest.hexdigest(), size


def safe_unlink(path: str | None) -> None:
    """Delete a temp file, ignoring the case where it is already gone."""
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def copy_into(src_path: str, dest_path: str) -> None:
    """Copy a staged file into the render workspace, creating parent dirs."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    shutil.copyfile(src_path, dest_path)
