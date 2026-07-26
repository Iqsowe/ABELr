"""`gpu_jpeg.cleanup_if_export` — deletes the LrExportSession fallback JPEG
after a caller has decoded it, and only that one.

No GPU/torch needed for these — pure filesystem behavior.
"""

from __future__ import annotations

from app.core import gpu_jpeg


def test_export_file_is_deleted(tmp_path):
    p = tmp_path / "photo_1_export.jpg"
    p.write_bytes(b"fake jpeg bytes")

    gpu_jpeg.cleanup_if_export(str(p), True)

    assert not p.exists()


def test_non_export_file_is_left_alone(tmp_path):
    """A normal requestJpegThumbnail tier must survive: `fetch_thumbnails_chunked`
    may still want to read it from a different chunk (that ambiguity is why
    normal thumbnails are purged by age in Thumbnails.lua, not deleted eagerly)."""
    p = tmp_path / "photo_1_5.jpg"
    p.write_bytes(b"fake jpeg bytes")

    gpu_jpeg.cleanup_if_export(str(p), False)

    assert p.exists()


def test_none_path_is_a_no_op():
    gpu_jpeg.cleanup_if_export(None, True)  # must not raise


def test_missing_file_is_a_no_op(tmp_path):
    """Already swept, or never written — deleting twice must not raise."""
    gpu_jpeg.cleanup_if_export(str(tmp_path / "gone.jpg"), True)
