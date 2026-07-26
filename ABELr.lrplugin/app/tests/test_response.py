"""Calibrated response model (`core.response`) — ∂render/∂slider inversion.

These functions translate a measured gap into a slider delta: a wrong sign or an
unstable division here would corrupt every correction. Pure (no disk I/O tested).
"""

from __future__ import annotations

import pytest

from app.core import response as rsp


# --- Exposure ----------------------------------------------------------------- #
def test_exposure_falls_back_to_nominal_when_uncalibrated():
    er = rsp.ExposureResponse()
    assert er.slope_at(50.0) == pytest.approx(rsp.NOMINAL_DL_DEV)
    # ΔEV for +17 L* at the 17 L*/EV prior = +1 EV.
    assert er.solve_dev(50.0, 67.0) == pytest.approx(1.0, abs=1e-6)


def test_exposure_slope_from_samples():
    er = rsp.ExposureResponse(ev=[-1.0, 0.0, 1.0], lstar=[33.0, 50.0, 67.0])
    assert er.slope_at(50.0) == pytest.approx(17.0, abs=1e-6)
    assert er.solve_dev(50.0, 67.0) == pytest.approx(1.0, abs=1e-6)


def test_exposure_slope_never_below_one():
    flat = rsp.ExposureResponse(ev=[0.0, 1.0], lstar=[50.0, 50.0])
    assert flat.slope_at(50.0) >= 1.0
    assert flat.slope_at(10.0) >= 1.0


def test_exposure_slope_non_monotonic_segment_picks_containing_interval():
    # Roll-off: slope drops sharply in the last segment (highlight compression).
    er = rsp.ExposureResponse(ev=[-1.0, 0.0, 1.0, 2.0], lstar=[33.0, 50.0, 67.0, 72.0])
    assert er.slope_at(70.0) == pytest.approx(5.0, abs=1e-6)   # [1,2] segment: (72-67)/1
    assert er.slope_at(60.0) == pytest.approx(17.0, abs=1e-6)  # [0,1] segment


def test_exposure_slope_out_of_range_uses_nearest_segment():
    er = rsp.ExposureResponse(ev=[-1.0, 0.0, 1.0], lstar=[33.0, 50.0, 67.0])
    # l_value above every probed L* -> nearest segment is the last one, [0,1].
    assert er.slope_at(120.0) == pytest.approx(17.0, abs=1e-6)
    # l_value below every probed L* -> nearest segment is the first one, [-1,0].
    assert er.slope_at(-50.0) == pytest.approx(17.0, abs=1e-6)


# --- Clipped-sample rejection (X1) --------------------------------------------- #
def test_clip_ok_accepts_clean_sample():
    assert rsp.clip_ok(0.0, 0.0)
    assert rsp.clip_ok(0.02, 0.01)


def test_clip_ok_rejects_over_threshold():
    assert not rsp.clip_ok(0.06, 0.0)
    assert not rsp.clip_ok(0.0, 0.10)


def test_clip_ok_boundary_is_inclusive():
    assert rsp.clip_ok(rsp.MAX_CLIP_FRAC, 0.0)


# --- White balance ------------------------------------------------------------ #
def test_wb_uncalibrated_returns_zero():
    wb = rsp.WBResponse()
    assert not wb.is_calibrated()
    assert wb.solve(2.0, 3.0) == (0.0, 0.0)


def test_wb_identity_jacobian_inverts_sign():
    # J = [[1,0],[0,1]] (a* ~ Temp/100, b* ~ Tint) → cancels a measured bias.
    wb = rsp.WBResponse(da_dtemp=1.0, db_dtemp=0.0, da_dtint=0.0, db_dtint=1.0)
    assert wb.is_calibrated()
    dtemp, dtint = wb.solve(2.0, 3.0)
    assert dtemp == pytest.approx(-200.0)  # (−a_bias)·100 K
    assert dtint == pytest.approx(-3.0)


def test_wb_singular_jacobian_returns_zero():
    wb = rsp.WBResponse(da_dtemp=1.0, db_dtemp=1.0, da_dtint=1.0, db_dtint=1.0)  # det=0
    assert wb.solve(2.0, 3.0) == (0.0, 0.0)


# --- HSL probing: slope fit (H2) ----------------------------------------------- #
def test_fit_linear_response_recovers_known_slope():
    # Known slider delta → expected slope: measured = 0.6*delta + constant offset
    # (offset absorbed by the free intercept, only the slope must be recovered).
    deltas = [-15.0, -8.0, 0.0, 8.0, 15.0]
    measured = [5.0 + 0.6 * d for d in deltas]
    assert rsp.fit_linear_response(deltas, measured) == pytest.approx(0.6, abs=1e-9)


def test_fit_linear_response_negative_slope():
    deltas = [-10.0, -5.0, 5.0, 10.0]
    measured = [-0.35 * d for d in deltas]
    assert rsp.fit_linear_response(deltas, measured) == pytest.approx(-0.35, abs=1e-9)


def test_fit_linear_response_needs_at_least_two_samples():
    assert rsp.fit_linear_response([5.0], [3.0]) == 0.0
    assert rsp.fit_linear_response([], []) == 0.0


def test_fit_linear_response_zero_when_deltas_not_dispersed():
    # All probed deltas identical → slope not identifiable.
    assert rsp.fit_linear_response([8.0, 8.0, 8.0], [1.0, 2.0, 3.0]) == 0.0


def test_fit_linear_response_mismatched_lengths_returns_zero():
    assert rsp.fit_linear_response([1.0, 2.0], [1.0]) == 0.0


# --- Full model + cache keys --------------------------------------------------- #
def test_band_fallback_is_default_response():
    m = rsp.ResponseModel(camera="ILCE-7M4", profile="Adobe Color")
    b = m.band("Red")
    assert (b.dchroma_dsat, b.dl_dlum, b.dhue_dhue) == (0.0, 0.0, 0.0)


def test_key_and_cache_file_sanitize_separators():
    assert rsp._key(None, None) == "unknown|unknown"
    # The internal '|' separator is neutralized within the components.
    assert rsp._key("a|b", "c") == "a_b|c"
    f = rsp._cache_file("ILCE 7M4", "Adobe/Color")
    assert f.suffix == ".json"
    assert "|" not in f.name and "/" not in f.name and " " not in f.name
