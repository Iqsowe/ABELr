"""PLAN.md COV2 — `app/core/cache.py` put_*/get_* round-trips + schema version-bump.

Previously only `develop_hash`/`style_hash`/`raw_signature` were exercised
elsewhere. This adds direct coverage of the actual read/write paths (seeds,
SourceRAW, InCameraJPEG, PreviewJPEG, NeutralPreviewJPEG) and the
DROP-and-recreate schema path that wiped the real cache at W3, with no
direct unit test before this.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core import cache
from app.core.analysis import ExposureStats
from app.core.pipeline import RenderAnalysis
from app.core.render_metrics import BandStats, NeutralStats, ToneStats


@pytest.fixture
def conn(tmp_path):
    catalog_path = tmp_path / "Catalog.lrcat"
    c = cache.open_cache(catalog_path)
    yield c
    c.close()


def _tone(l=50.0):
    return ToneStats(median_l=l, mean_l=l, p05_l=10.0, p95_l=90.0, clipped_hi=0.01, clipped_lo=0.02, tonal_frac=0.8)


def _neutral(frac=0.05):
    return NeutralStats(a_bias=0.5, b_bias=-1.2, chroma=2.0, neutral_frac=frac, n_neutral=1234)


def _bands():
    return [
        BandStats(name="Red", frac=0.1, median_hue=10.0, median_chroma=20.0, median_sat=0.4, sat_clip_frac=0.0, median_l=50.0),
        BandStats(name="Blue", frac=0.2, median_hue=230.0, median_chroma=15.0, median_sat=0.3, sat_clip_frac=0.01, median_l=40.0),
    ]


def _analysis():
    return RenderAnalysis(tone=_tone(), neutral=_neutral(), bands=_bands())


# --------------------------------------------------------------------------- #
# Schema / version
# --------------------------------------------------------------------------- #
def test_open_cache_creates_schema_at_current_version(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == cache.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(cache._TABLES) <= tables


def test_reopen_same_version_is_noop_preserves_data(tmp_path):
    catalog_path = tmp_path / "Catalog.lrcat"
    c1 = cache.open_cache(catalog_path)
    cache.put_picture(c1, "u1", path="p", catalog_path=str(catalog_path), exif={}, current_develop={})
    c1.close()

    c2 = cache.open_cache(catalog_path)
    assert cache.get_picture(c2, "u1") is not None
    c2.close()


def test_schema_version_bump_drops_and_recreates(tmp_path, monkeypatch):
    catalog_path = tmp_path / "Catalog.lrcat"
    c1 = cache.open_cache(catalog_path)
    cache.put_picture(c1, "u1", path="p", catalog_path=str(catalog_path), exif={}, current_develop={})
    assert cache.get_picture(c1, "u1") is not None
    c1.close()

    monkeypatch.setattr(cache, "SCHEMA_VERSION", cache.SCHEMA_VERSION + 1)
    c2 = cache.open_cache(catalog_path)
    assert conn_version(c2) == cache.SCHEMA_VERSION
    # DROP+recreate, no migration -> the old row is gone.
    assert cache.get_picture(c2, "u1") is None
    c2.close()


def conn_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


# --------------------------------------------------------------------------- #
# LightroomPicture / seeds
# --------------------------------------------------------------------------- #
def test_put_get_picture_roundtrip(conn):
    cache.put_picture(
        conn, "u1", path="C:/p.ARW", catalog_path="C:/cat.lrcat",
        exif={"camera": "ILCE-7M4", "iso": 800}, current_develop={"Exposure2012": 0.5},
        profile_capture="Neutral",
    )
    got = cache.get_picture(conn, "u1")
    assert got["path"] == "C:/p.ARW"
    assert got["current_develop"] == {"Exposure2012": 0.5}
    assert got["profile_capture"] == "Neutral"
    assert got["is_seed"] is False


def test_get_picture_missing_returns_none(conn):
    assert cache.get_picture(conn, "missing") is None


def test_put_picture_upsert_preserves_is_seed(conn):
    cache.put_picture(conn, "u1", path="p", catalog_path="c", exif={}, current_develop={})
    cache.set_seed(conn, "u1", True)
    assert cache.is_seed(conn, "u1") is True
    # re-analysis (put_picture again) must not clear the seed flag
    cache.put_picture(conn, "u1", path="p2", catalog_path="c", exif={}, current_develop={"Exposure2012": 1.0})
    assert cache.is_seed(conn, "u1") is True
    assert cache.get_picture(conn, "u1")["path"] == "p2"


def test_set_seed_creates_row_if_missing(conn):
    cache.set_seed(conn, "new-uuid", True)
    assert cache.is_seed(conn, "new-uuid") is True


def test_is_seed_false_for_unknown_uuid(conn):
    assert cache.is_seed(conn, "unknown") is False


def test_list_seed_uuids(conn):
    cache.set_seed(conn, "a", True)
    cache.set_seed(conn, "b", False)
    cache.set_seed(conn, "c", True)
    assert set(cache.list_seed_uuids(conn)) == {"a", "c"}


# --------------------------------------------------------------------------- #
# SourceRAW
# --------------------------------------------------------------------------- #
def test_put_get_source_raw_roundtrip(conn):
    exp = ExposureStats(mean_luma=0.2, median_luma=0.18, clipped_highlights=0.001, clipped_shadows=0.0)
    cache.put_source_raw(
        conn, "u1", "hash-abc",
        asshot_rg=1.9, asshot_bg=1.4,
        exposure_global=exp, exposure_sharp=exp,
        grayworld_global=(1.8, 1.3), grayworld_sharp=(1.85, 1.35),
        mask_sharp_frac=0.4, ev100=8.5, profile_capture="IN",
        tone=_tone(), bands=_bands(),
    )
    got = cache.get_source_raw(conn, "u1", "hash-abc")
    assert got is not None
    assert got["asshot_rg"] == 1.9
    assert got["ev100"] == 8.5
    assert got["profile_capture"] == "IN"
    assert got["exposure"].mean_luma == 0.2
    assert got["tone"].median_l == 50.0
    assert [b.name for b in got["bands"]] == ["Red", "Blue"]


def test_get_source_raw_hash_mismatch_returns_none(conn):
    cache.put_source_raw(conn, "u1", "hash-abc", asshot_rg=1.9, asshot_bg=1.4)
    assert cache.get_source_raw(conn, "u1", "different-hash") is None


def test_get_source_raw_latest_ignores_hash(conn):
    cache.put_source_raw(conn, "u1", "hash-old", asshot_rg=1.9, asshot_bg=1.4)
    got = cache.get_source_raw_latest(conn, "u1")
    assert got is not None
    assert got["asshot_rg"] == 1.9


def test_put_source_raw_all_optional_fields_none(conn):
    cache.put_source_raw(conn, "u1", "h", asshot_rg=None, asshot_bg=None)
    got = cache.get_source_raw(conn, "u1", "h")
    assert got["exposure"] is None
    assert got["tone"] is None
    assert got["bands"] is None


# --------------------------------------------------------------------------- #
# InCameraJPEG
# --------------------------------------------------------------------------- #
def test_put_get_in_camera_jpeg_roundtrip(conn):
    cache.put_in_camera_jpeg(
        conn, "u1", "hash-jpeg", sharp=_analysis(), glob=_analysis(),
        mask_sharp_frac=0.6, profile_capture="Neutral",
    )
    got = cache.get_in_camera_jpeg(conn, "u1", "hash-jpeg")
    assert got is not None
    assert got["sharp"].tone.median_l == 50.0
    assert got["global"].bands[0].name == "Red"
    assert got["tone"].median_l == 50.0  # sharp-zone convenience alias
    assert got["profile_capture"] == "Neutral"


def test_get_in_camera_jpeg_hash_mismatch_returns_none(conn):
    cache.put_in_camera_jpeg(conn, "u1", "hash-jpeg", sharp=_analysis())
    assert cache.get_in_camera_jpeg(conn, "u1", "other") is None


def test_get_in_camera_jpeg_latest_ignores_hash(conn):
    cache.put_in_camera_jpeg(conn, "u1", "hash-old", sharp=_analysis())
    assert cache.get_in_camera_jpeg_latest(conn, "u1") is not None


def test_put_in_camera_jpeg_sharp_none(conn):
    cache.put_in_camera_jpeg(conn, "u1", "h", sharp=None, glob=None)
    got = cache.get_in_camera_jpeg(conn, "u1", "h")
    assert got["sharp"] is None
    assert got["global"] is None
    assert got["tone"] is None
    assert got["bands"] is None


# --------------------------------------------------------------------------- #
# PreviewJPEG
# --------------------------------------------------------------------------- #
def test_put_get_preview_jpeg_roundtrip(conn):
    cache.put_preview_jpeg(conn, "u1", "hash-preview", sharp=_analysis(), glob=_analysis(), mask_sharp_frac=0.7)
    got = cache.get_preview_jpeg(conn, "u1", "hash-preview")
    assert got is not None
    assert got.tone.median_l == 50.0
    assert got.neutral.a_bias == 0.5
    assert len(got.bands) == 2


def test_get_preview_jpeg_hash_mismatch_returns_none(conn):
    cache.put_preview_jpeg(conn, "u1", "hash-preview", sharp=_analysis())
    assert cache.get_preview_jpeg(conn, "u1", "stale-hash") is None


def test_get_preview_jpeg_latest_ignores_hash(conn):
    cache.put_preview_jpeg(conn, "u1", "hash-old", sharp=_analysis())
    got = cache.get_preview_jpeg_latest(conn, "u1")
    assert got is not None
    assert got.tone.median_l == 50.0


# --------------------------------------------------------------------------- #
# NeutralPreviewJPEG
# --------------------------------------------------------------------------- #
def test_put_get_neutral_preview_roundtrip(conn):
    cache.put_neutral_preview(
        conn, "u1", "hash-style", sharp=_analysis(), glob=_analysis(),
        mask_sharp_frac=0.3, asshot_temp=5200.0, asshot_tint=-3.0,
    )
    got = cache.get_neutral_preview(conn, "u1", "hash-style")
    assert got is not None
    assert got["asshot_temp"] == 5200.0
    assert got["asshot_tint"] == -3.0
    assert got["sharp"].tone.median_l == 50.0
    assert got["glob"].bands[1].name == "Blue"


def test_get_neutral_preview_hash_mismatch_returns_none(conn):
    cache.put_neutral_preview(conn, "u1", "hash-style", sharp=_analysis())
    assert cache.get_neutral_preview(conn, "u1", "other-style") is None


def test_get_neutral_preview_latest_ignores_hash(conn):
    cache.put_neutral_preview(conn, "u1", "hash-old", sharp=_analysis())
    assert cache.get_neutral_preview_latest(conn, "u1") is not None


# --------------------------------------------------------------------------- #
# raw_signature
# --------------------------------------------------------------------------- #
def test_raw_signature_existing_file(tmp_path):
    f = tmp_path / "photo.ARW"
    f.write_bytes(b"x" * 100)
    sig = cache.raw_signature(f)
    assert sig.startswith("100:")
    assert sig.endswith(cache.ANALYSIS_VERSION)


def test_raw_signature_missing_file_falls_back(tmp_path):
    sig = cache.raw_signature(tmp_path / "does-not-exist.ARW")
    assert sig == f"0:0:{cache.ANALYSIS_VERSION}"


# --------------------------------------------------------------------------- #
# commit=False batching
# --------------------------------------------------------------------------- #
def test_commit_false_defers_write_until_explicit_commit(tmp_path):
    catalog_path = tmp_path / "Catalog.lrcat"
    c = cache.open_cache(catalog_path)
    cache.put_picture(c, "u1", path="p", catalog_path="c", exif={}, current_develop={}, commit=False)
    # visible on the same connection before commit (same transaction)
    assert cache.get_picture(c, "u1") is not None
    c.commit()

    c2 = cache.open_cache(catalog_path)
    assert cache.get_picture(c2, "u1") is not None
    c.close()
    c2.close()
