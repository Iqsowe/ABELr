"""WB seed model (`core.wb_model`) — physical slope + seed-calibrated intercept,
and the neutral-cast refinement (`refine_temp_tint`).

PLAN.md step W5: close the test-coverage gap (OLD_PLAN.md backlog item 4) —
`calibrate`/`refine_temp_tint`'s no-op branches were previously untested.
"""

from __future__ import annotations

import pytest

from app.core import wb_model as wbm
from app.core.render_metrics import NeutralStats
from app.core.response import WBResponse
from app.core.wb_model import Seed, TEMP_MAX, TEMP_MIN


# --------------------------------------------------------------------------- #
# slope_for_camera
# --------------------------------------------------------------------------- #
def test_slope_for_camera_known():
    assert wbm.slope_for_camera("ILCE-7M4") == wbm.CAMERA_SLOPE_RG["ILCE-7M4"]


def test_slope_for_camera_unknown_falls_back_to_default():
    assert wbm.slope_for_camera("SomeOtherCamera") == wbm.DEFAULT_SLOPE_RG
    assert wbm.slope_for_camera(None) == wbm.DEFAULT_SLOPE_RG


# --------------------------------------------------------------------------- #
# calibrate
# --------------------------------------------------------------------------- #
def test_calibrate_raises_on_empty_seeds():
    with pytest.raises(ValueError):
        wbm.calibrate([])


def test_calibrate_intercept_is_median_offset_at_fixed_slope():
    slope = 2450.0
    # Two seeds on the exact line (intercept=1000) -> intercept recovered exactly.
    seeds = [
        Seed("a", asshot_rg=0.5, asshot_bg=0.6, temperature=slope * 0.5 + 1000.0, tint=0.0, exposure=0.0),
        Seed("b", asshot_rg=0.6, asshot_bg=0.6, temperature=slope * 0.6 + 1000.0, tint=0.0, exposure=0.0),
    ]
    cal = wbm.calibrate(seeds, slope)
    assert cal.slope_rg == slope
    assert cal.intercept == pytest.approx(1000.0, abs=1e-6)
    assert cal.residual_k == pytest.approx(0.0, abs=1e-6)
    assert cal.n_seeds == 2


def test_calibrate_single_seed_zero_residual_and_spread():
    seeds = [Seed("a", asshot_rg=0.5, asshot_bg=0.6, temperature=5500.0, tint=2.0, exposure=0.1)]
    cal = wbm.calibrate(seeds)
    assert cal.residual_k == 0.0
    assert cal.temp_spread_k == 0.0
    assert cal.n_seeds == 1
    assert cal.tint == pytest.approx(2.0)
    assert cal.exposure == pytest.approx(0.1)
    assert cal.median_temp_k == pytest.approx(5500.0)


def test_calibrate_tint_and_exposure_are_medians():
    seeds = [
        Seed("a", 0.5, 0.6, 5500.0, tint=0.0, exposure=0.0),
        Seed("b", 0.5, 0.6, 5500.0, tint=10.0, exposure=1.0),
        Seed("c", 0.5, 0.6, 5500.0, tint=20.0, exposure=2.0),
    ]
    cal = wbm.calibrate(seeds)
    assert cal.tint == pytest.approx(10.0)
    assert cal.exposure == pytest.approx(1.0)


def test_calibrate_robust_to_an_outlier_seed():
    # Median offset should ignore a single wild outlier that a mean would not.
    slope = 2450.0
    seeds = [
        Seed("a", 0.5, 0.6, slope * 0.5 + 1000.0, 0.0, 0.0),
        Seed("b", 0.5, 0.6, slope * 0.5 + 1000.0, 0.0, 0.0),
        Seed("c", 0.5, 0.6, slope * 0.5 + 1000.0, 0.0, 0.0),
        Seed("outlier", 0.5, 0.6, slope * 0.5 + 9000.0, 0.0, 0.0),  # way off
    ]
    cal = wbm.calibrate(seeds, slope)
    assert cal.intercept == pytest.approx(1000.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# WBCalibration.predict_temperature
# --------------------------------------------------------------------------- #
def test_predict_temperature_bounded_to_lr_range():
    cal = wbm.WBCalibration(
        slope_rg=1_000_000.0, intercept=0.0, tint=0.0, exposure=0.0,
        n_seeds=1, residual_k=0.0, temp_spread_k=0.0,
    )
    assert cal.predict_temperature(10.0) == pytest.approx(TEMP_MAX)
    assert cal.predict_temperature(-10.0) == pytest.approx(TEMP_MIN)


def test_predict_temperature_within_range_unclamped():
    cal = wbm.WBCalibration(
        slope_rg=2450.0, intercept=1000.0, tint=0.0, exposure=0.0,
        n_seeds=1, residual_k=0.0, temp_spread_k=0.0,
    )
    assert cal.predict_temperature(1.0) == pytest.approx(2450.0 + 1000.0)


# --------------------------------------------------------------------------- #
# refine_temp_tint
# --------------------------------------------------------------------------- #
def _neutral(a=5.0, b=5.0, frac=0.1, n=500):
    return NeutralStats(a_bias=a, b_bias=b, chroma=7.0, neutral_frac=frac, n_neutral=n)


def _calibrated_wb():
    return WBResponse(da_dtemp=0.5, db_dtemp=0.2, da_dtint=-0.3, db_dtint=0.4)


def test_refine_temp_tint_keeps_seed_prediction_when_no_neutral_pixels():
    n = NeutralStats(a_bias=5.0, b_bias=5.0, chroma=7.0, neutral_frac=0.0, n_neutral=0)
    temp, tint, reason = wbm.refine_temp_tint(5500.0, 0.0, n, _calibrated_wb())
    assert temp == 5500.0 and tint == 0.0
    assert "insufficient neutrals" in reason


def test_refine_temp_tint_keeps_seed_prediction_when_neutral_frac_too_low():
    n = _neutral(frac=0.001)  # below MIN_NEUTRAL_FRAC=0.005
    temp, tint, reason = wbm.refine_temp_tint(5500.0, 0.0, n, _calibrated_wb())
    assert temp == 5500.0 and tint == 0.0
    assert "insufficient neutrals" in reason


def test_refine_temp_tint_keeps_seed_prediction_when_response_uncalibrated():
    temp, tint, reason = wbm.refine_temp_tint(5500.0, 0.0, _neutral(), WBResponse())
    assert temp == 5500.0 and tint == 0.0
    assert "not calibrated" in reason


def test_refine_temp_tint_applies_calibrated_delta():
    temp, tint, reason = wbm.refine_temp_tint(5500.0, 0.0, _neutral(a=1.0, b=1.0), _calibrated_wb())
    assert (temp, tint) != (5500.0, 0.0)
    assert "neutrals" in reason and "ΔTemp" in reason


def test_refine_temp_tint_clamps_dtemp_to_max_delta():
    # Huge bias would demand a huge correction -- must clip to max_dtemp_k.
    n = _neutral(a=1000.0, b=1000.0)
    temp, _tint, _reason = wbm.refine_temp_tint(5500.0, 0.0, n, _calibrated_wb(), max_dtemp_k=600.0)
    assert abs(temp - 5500.0) <= 600.0 + 1e-6


def test_refine_temp_tint_clamps_dtint_to_max_delta():
    n = _neutral(a=1000.0, b=1000.0)
    _temp, tint, _reason = wbm.refine_temp_tint(5500.0, 0.0, n, _calibrated_wb(), max_dtint=10.0)
    assert abs(tint - 0.0) <= 10.0 + 1e-6


def test_refine_temp_tint_clamps_final_temperature_to_lr_bounds():
    n = _neutral(a=1000.0, b=1000.0)
    temp, _tint, _reason = wbm.refine_temp_tint(TEMP_MIN + 100.0, 0.0, n, _calibrated_wb(), max_dtemp_k=100000.0)
    assert TEMP_MIN <= temp <= TEMP_MAX


def test_refine_temp_tint_clamps_final_tint_to_150():
    n = _neutral(a=1000.0, b=1000.0)
    _temp, tint, _reason = wbm.refine_temp_tint(5500.0, 140.0, n, _calibrated_wb(), max_dtint=100000.0)
    assert -150.0 <= tint <= 150.0
