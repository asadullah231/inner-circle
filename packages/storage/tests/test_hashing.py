"""Hashing and deduplication tests. No MinIO required."""

import hashlib
import io
import os

from packages.storage import hashing, paths


def test_hash_file_matches_hashlib(tmp_path):
    data = b"cat video bytes" * 1000
    f = tmp_path / "clip.bin"
    f.write_bytes(data)
    assert hashing.hash_file(str(f)) == hashlib.sha256(data).hexdigest()


def test_hash_stream_matches_hashlib():
    data = b"narration audio" * 500
    assert hashing.hash_stream(io.BytesIO(data)) == hashlib.sha256(data).hexdigest()


def test_identical_files_produce_identical_keys(tmp_path):
    """This is what makes deduplication work."""
    data = b"the same pexels clip downloaded twice"
    a = tmp_path / "from_pexels_1.mp4"
    b = tmp_path / "renamed_by_provider.mp4"
    a.write_bytes(data)
    b.write_bytes(data)

    ha = hashing.hash_file(str(a))
    hb = hashing.hash_file(str(b))
    assert ha == hb

    assert paths.asset_key(ha, ".mp4") == paths.asset_key(hb, ".mp4")


def test_different_files_produce_different_keys(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"clip one")
    b.write_bytes(b"clip two")
    ka = paths.asset_key(hashing.hash_file(str(a)), ".mp4")
    kb = paths.asset_key(hashing.hash_file(str(b)), ".mp4")
    assert ka != kb


def test_spool_and_hash_writes_and_hashes(tmp_path):
    data = b"streamed upload payload" * 100
    chunks = iter([data[i : i + 64] for i in range(0, len(data), 64)])
    path, sha, size = hashing.spool_and_hash(chunks, tmp_dir=str(tmp_path))
    try:
        assert size == len(data)
        assert sha == hashlib.sha256(data).hexdigest()
        assert open(path, "rb").read() == data
    finally:
        hashing.safe_unlink(path)


def test_spool_cleans_up_on_failure(tmp_path):
    def exploding():
        yield b"partial"
        raise RuntimeError("network dropped")

    before = set(os.listdir(tmp_path))
    try:
        hashing.spool_and_hash(exploding(), tmp_dir=str(tmp_path))
    except RuntimeError:
        pass
    after = set(os.listdir(tmp_path))
    assert before == after, "a partial temp file was left behind"


def test_safe_unlink_is_idempotent(tmp_path):
    f = tmp_path / "gone.bin"
    f.write_bytes(b"x")
    hashing.safe_unlink(str(f))
    hashing.safe_unlink(str(f))  # must not raise
