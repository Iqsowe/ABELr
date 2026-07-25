"""PLAN step S0 — validation harness (`app.tools.validate_seed_matching`).

Pure error-aggregation helpers (`mae`/`circular_mae`/`split_by_median`) and the
two orchestration entry points (`run_seeds_loocv`/`run_embedded_validation`)
exercised on synthetic data — no dependency on the real `ABELr_cache.db`
(that file only exists on the user's machine and is opened read-only by the
CLI, cf. module docstring).
"""

from __future__ import annotations

import pytest

from app.core import cache, response
from app.core.pipeline import RenderAnalysis
from app.core.render_metrics import NeutralStats, ToneStats
from app.core.response import WBResponse
from app.tests.conftest import make_seed as _seed
from app.tools import validate_seed_matching as vsm


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_mae_empty_is_none():
    assert vsm.mae([]) is None


def test_mae_basic():
    assert vsm.mae([(10.0, 12.0), (5.0, 5.0)]) == pytest.approx(1.0)


def test_circular_mae_wraps_around_360():
    # 359 vs 1: shortest arc is 2 degrees, not 358.
    assert vsm.circular_mae([(359.0, 1.0)]) == pytest.approx(2.0)


def test_circular_mae_empty_is_none():
    assert vsm.circular_mae([]) is None


def test_split_by_median_near_far():
    triples = [(1.0, 10.0, 10.0), (2.0, 20.0, 22.0), (10.0, 30.0, 40.0)]
    near, far = vsm.split_by_median(triples)
    assert (10.0, 10.0) in near
    assert (30.0, 40.0) in far


def test_split_by_median_empty():
    assert vsm.split_by_median([]) == ([], [])


# --------------------------------------------------------------------------- #
# Seeds-mode LOOCV (synthetic pool, no DB)
# --------------------------------------------------------------------------- #
def test_run_seeds_loocv_perfect_pair_gives_zero_error():
    # Two near-identical seeds (twins): each one's LOOCV prediction should match
    # the other almost exactly (small pool, k=1 by construction).
    a = _seed("a", 0.5, 0.5, 50.0, temp=5500.0, tint=2.0, tone_l=60.0)
    b = _seed("b", 0.5, 0.5, 50.0, temp=5500.0, tint=2.0, tone_l=60.0)
    r = vsm.run_seeds_loocv([a, b])
    assert r.n_seeds == 2
    expo_pairs = [(p, act) for _, p, act in r.expo_triples]
    assert vsm.mae(expo_pairs) == pytest.approx(0.0, abs=1e-6)
    temp_pairs = [(p, act) for _, p, act in r.temp_triples]
    assert vsm.mae(temp_pairs) == pytest.approx(0.0, abs=1e-6)


def test_run_seeds_loocv_detects_prediction_gap():
    # "near" is the only usable match for "target" (k=1 on a 2-seed pool) and
    # disagrees on Temperature by 500K -> LOOCV must surface that as error.
    target = _seed("t", 0.5, 0.5, 50.0, temp=5000.0, tone_l=40.0)
    near = _seed("n", 0.51, 0.5, 50.0, temp=5500.0, tone_l=40.0)
    r = vsm.run_seeds_loocv([target, near])
    temp_pairs = [(p, act) for _, p, act in r.temp_triples]
    # Each seed's LOOCV prediction is simply the other seed's Temperature (k=1,
    # pool of 2) -> both directions miss by the same 500K gap.
    assert vsm.mae(temp_pairs) == pytest.approx(500.0)


def test_run_seeds_loocv_calibration_field_tracked():
    a = _seed("a", 0.5, 0.5, 50.0, shadow_tint=-10.0)
    b = _seed("b", 0.5, 0.5, 50.0, shadow_tint=-10.0)
    r = vsm.run_seeds_loocv([a, b])
    assert vsm.mae(r.calib_pairs["shadow_tint"]) == pytest.approx(0.0, abs=1e-6)


def test_run_seeds_loocv_empty_pool():
    r = vsm.run_seeds_loocv([])
    assert r.n_seeds == 0
    assert r.expo_triples == []


# --------------------------------------------------------------------------- #
# Embedded-mode validation (throwaway on-disk cache, not the real user DB)
# --------------------------------------------------------------------------- #
def test_run_embedded_validation_resolves_and_predicts_exposure(tmp_path):
    conn = cache.open_cache(tmp_path / "fake.lrcat")
    uuid = "photo-1"
    cache.put_picture(
        conn, uuid, path="x.arw", catalog_path=None, exif=None,
        current_develop={"Exposure2012": 1.0},
    )
    t_tone = ToneStats(70.0, 70.0, 60.0, 80.0, 0.0, 0.0, 1.0)
    n_tone = ToneStats(50.0, 50.0, 40.0, 60.0, 0.0, 0.0, 1.0)
    cache.put_in_camera_jpeg(
        conn, uuid, "hj", sharp=RenderAnalysis(tone=t_tone, neutral=None, bands=[])
    )
    cache.put_neutral_preview(
        conn, uuid, "hs", sharp=RenderAnalysis(tone=n_tone, neutral=None, bands=[]),
        asshot_temp=5500.0, asshot_tint=0.0,
    )

    r = vsm.run_embedded_validation(conn)
    assert r.n_candidates == 1
    assert r.n_resolved == 1
    assert len(r.expo_pairs) == 1
    # The target (T) is brighter than the anchor (N): a positive EV is predicted.
    pred_ev, actual_ev = r.expo_pairs[0]
    assert pred_ev > 0.0
    assert actual_ev == pytest.approx(1.0)


def test_run_embedded_validation_skips_photo_with_no_anchor(tmp_path):
    conn = cache.open_cache(tmp_path / "fake.lrcat")
    uuid = "photo-2"
    cache.put_picture(
        conn, uuid, path="x.arw", catalog_path=None, exif=None, current_develop={},
    )
    # No InCameraJPEG / NeutralPreviewJPEG rows written -> not a candidate at all
    # (NeutralPreviewJPEG drives the uuid list).
    r = vsm.run_embedded_validation(conn)
    assert r.n_candidates == 0
    assert r.n_resolved == 0


def test_run_embedded_validation_reports_uncalibrated_wb_honestly(tmp_path):
    # response_cache/ is empty in real usage (PLAN.md evidence) -> WB predictions
    # must never be silently fabricated; a manually-white-balanced photo with a
    # real, measurable Custom WB cast beyond the dead zone must be counted as
    # "not predictable" (wb_n_uncalibrated), not averaged into wb_temp_pairs.
    conn = cache.open_cache(tmp_path / "fake.lrcat")
    uuid = "photo-3"
    cache.put_picture(
        conn, uuid, path="x.arw", catalog_path=None, exif=None,
        current_develop={"WhiteBalance": "Custom", "Temperature": 6000.0, "Tint": 5.0},
    )
    tone = ToneStats(50.0, 50.0, 40.0, 60.0, 0.0, 0.0, 1.0)
    t_neutral = NeutralStats(a_bias=0.0, b_bias=0.0, chroma=0.0, neutral_frac=0.5, n_neutral=100)
    n_neutral = NeutralStats(a_bias=5.0, b_bias=5.0, chroma=7.0, neutral_frac=0.5, n_neutral=100)
    cache.put_in_camera_jpeg(
        conn, uuid, "hj", sharp=RenderAnalysis(tone=tone, neutral=t_neutral, bands=[])
    )
    cache.put_neutral_preview(
        conn, uuid, "hs", sharp=RenderAnalysis(tone=tone, neutral=n_neutral, bands=[]),
        asshot_temp=5500.0, asshot_tint=0.0,
    )
    r = vsm.run_embedded_validation(conn)
    assert r.wb_temp_pairs == []  # no calibrated response -> nothing to compare
    assert r.wb_n_uncalibrated == 1  # deviant, measurable cast — flagged, not silently dropped


def test_run_embedded_validation_uses_response_keyed_by_profile_capture(tmp_path, monkeypatch):
    # Regression guard: the model lookup must key on `profile_capture` (the
    # in-camera creative profile, e.g. "IN"/"Neutral") like
    # calibrate_hsl_response.py/calibrate_wb_response.py save it — NOT on
    # `current_develop["CameraProfile"]` (the Lr DCP profile, a different axis
    # entirely; cf. autocorrect_worker.py fix in the same session). A group with
    # a calibrated WBResponse for its (camera, profile_capture) pair must
    # actually get its WB correction written, not stay "uncalibrated".
    monkeypatch.setattr(response, "_CACHE_DIR", tmp_path / "response_cache")
    model = response.load("TestCam", "TestProfile")
    model.wb = WBResponse(da_dtemp=0.5, db_dtemp=0.2, da_dtint=-0.3, db_dtint=0.4)
    response.save(model)

    conn = cache.open_cache(tmp_path / "fake.lrcat")
    uuid = "photo-4"
    cache.put_picture(
        conn, uuid, path="x.arw", catalog_path=None,
        exif={"camera": "TestCam"},
        current_develop={"WhiteBalance": "Custom", "Temperature": 6000.0, "Tint": 5.0},
        profile_capture="TestProfile",
    )
    tone = ToneStats(50.0, 50.0, 40.0, 60.0, 0.0, 0.0, 1.0)
    t_neutral = NeutralStats(a_bias=0.0, b_bias=0.0, chroma=0.0, neutral_frac=0.5, n_neutral=100)
    n_neutral = NeutralStats(a_bias=5.0, b_bias=5.0, chroma=7.0, neutral_frac=0.5, n_neutral=100)
    cache.put_in_camera_jpeg(
        conn, uuid, "hj", sharp=RenderAnalysis(tone=tone, neutral=t_neutral, bands=[])
    )
    cache.put_neutral_preview(
        conn, uuid, "hs", sharp=RenderAnalysis(tone=tone, neutral=n_neutral, bands=[]),
        asshot_temp=5500.0, asshot_tint=0.0,
    )
    r = vsm.run_embedded_validation(conn)
    assert r.wb_n_uncalibrated == 0
    assert len(r.wb_temp_pairs) == 1  # the calibrated model was found and used
