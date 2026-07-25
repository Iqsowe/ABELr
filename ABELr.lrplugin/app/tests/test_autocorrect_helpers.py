"""Pure helpers from `core.autocorrect`: circular hue difference, crop area,
robust setting read. Small but used in every correction plan.
"""

from __future__ import annotations

import pytest

from app.core import autocorrect as ac


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (10.0, 20.0, -10.0),
        (20.0, 10.0, 10.0),
        (350.0, 10.0, -20.0),   # 350 is "before" 10 on the circle
        (10.0, 350.0, 20.0),
        (0.0, 0.0, 0.0),
    ],
)
def test_hue_diff_circular(a, b, expected):
    assert ac._hue_diff(a, b) == pytest.approx(expected, abs=1e-9)


def test_hue_diff_in_range():
    # Implementation `(a-b+180)%360-180` → interval [−180, 180) (the antipode gives −180).
    for a in range(0, 360, 17):
        for b in range(0, 360, 23):
            d = ac._hue_diff(float(a), float(b))
            assert -180.0 <= d < 180.0


def test_crop_area_defaults_to_full_frame():
    assert ac._crop_area({}) == pytest.approx(1.0)


def test_crop_area_partial_and_inverted():
    assert ac._crop_area(
        {"CropLeft": 0.0, "CropRight": 0.5, "CropTop": 0.0, "CropBottom": 1.0}
    ) == pytest.approx(0.5)
    # Inverted bounds → area clamped to 0, never negative.
    assert ac._crop_area(
        {"CropLeft": 0.8, "CropRight": 0.2, "CropTop": 0.0, "CropBottom": 1.0}
    ) == pytest.approx(0.0)


def test_f_reads_float_with_default():
    assert ac._f({"Exposure2012": "0.5"}, "Exposure2012") == pytest.approx(0.5)
    assert ac._f({}, "Missing", default=1.0) == pytest.approx(1.0)
    assert ac._f({"Bad": "not-a-number"}, "Bad", default=-1.0) == pytest.approx(-1.0)
    assert ac._f(None, "Any", default=2.0) == pytest.approx(2.0)


def test_calib_develop_dict_empty_without_calibration():
    from app.core.seed_match import SeedTarget

    t = SeedTarget(
        temperature=None, tint=None, tone=None, bands=None,
        shadow_tint=None, red_hue=None, red_saturation=None,
        green_hue=None, green_saturation=None, blue_hue=None, blue_saturation=None,
        n_matched=1, seed_ids=["s"],
    )
    assert t.has_calibration() is False
    assert ac._calib_develop_dict(t) == {}


def test_calib_develop_dict_writes_present_fields_clamped_and_snapped():
    from app.core.seed_match import SeedTarget

    t = SeedTarget(
        temperature=None, tint=None, tone=None, bands=None,
        shadow_tint=-12.4, red_hue=150.0, red_saturation=None,
        green_hue=0.0, green_saturation=-200.0, blue_hue=None, blue_saturation=None,
        n_matched=2, seed_ids=["a", "b"],
    )
    assert t.has_calibration() is True
    dev = ac._calib_develop_dict(t)
    assert dev == {
        "ShadowTint": -10,       # clamped to -12.4, snapped to the nearest 5
        "RedHue": 100,           # clamped to +100 (already on-grid)
        "GreenHue": 0,
        "GreenSaturation": -100,  # clamped to -100 (already on-grid)
        "EnableCalibration": True,
    }
    # Fields absent from the target (RedSaturation/BlueHue/BlueSaturation) omitted.
    assert "RedSaturation" not in dev
    assert "BlueHue" not in dev


# --------------------------------------------------------------------------- #
# H1 (PLAN) — the `raw_oversat` guard is wired from the sharp-zone RAW of the
# target photo (not the seeds) in both BandTarget constructors.
# --------------------------------------------------------------------------- #
def _raw_band(name="Red", sat_clip_frac=0.0, frac=0.5):
    from app.core.render_metrics import BandStats

    return BandStats(
        name=name, frac=frac, median_hue=0.0, median_chroma=40.0,
        median_sat=0.5, sat_clip_frac=sat_clip_frac, median_l=50.0,
    )


def test_embedded_band_targets_wires_raw_oversat_from_target_photo_raw():
    from app.core.pipeline import RenderAnalysis
    from app.core.render_metrics import BandStats

    t = RenderAnalysis(
        tone=None, neutral=None,
        bands=[BandStats("Red", 0.5, 0.0, 40.0, 0.5, 0.0, 50.0)],
    )
    bias = ac.ProfileBias(n=8)

    # RAW confirms (hard sat_clip_frac) → raw_oversat=True.
    raw_bands = [_raw_band(sat_clip_frac=0.10)]
    tgs = ac._embedded_band_targets(t, bias, ignore_bias=True, raw_bands=raw_bands)
    assert tgs["Red"].raw_oversat is True

    # RAW denies (no hard clip) → raw_oversat=False.
    raw_bands = [_raw_band(sat_clip_frac=0.0)]
    tgs = ac._embedded_band_targets(t, bias, ignore_bias=True, raw_bands=raw_bands)
    assert tgs["Red"].raw_oversat is False

    # No RAW info → raw_oversat=None (historical behavior, no blocking).
    tgs = ac._embedded_band_targets(t, bias, ignore_bias=True, raw_bands=None)
    assert tgs["Red"].raw_oversat is None


def test_band_targets_from_seed_match_wires_raw_oversat_from_target_photo_raw():
    from app.core.seed_match import SeedTarget
    from app.core.render_metrics import BandStats

    t = SeedTarget(
        temperature=None, tint=None, tone=None,
        bands=[BandStats("Red", 0.5, 0.0, 40.0, 0.5, 0.0, 50.0)],
        shadow_tint=None, red_hue=None, red_saturation=None,
        green_hue=None, green_saturation=None, blue_hue=None, blue_saturation=None,
        n_matched=1, seed_ids=["s"],
    )

    raw_bands = [_raw_band(sat_clip_frac=0.10)]
    tgs = ac._band_targets_from_seed_match(t, raw_bands=raw_bands)
    assert tgs["Red"].raw_oversat is True

    tgs = ac._band_targets_from_seed_match(t, raw_bands=None)
    assert tgs["Red"].raw_oversat is None


# --------------------------------------------------------------------------- #
# H3 (PLAN) — `embedded_raw` marks raw-transplant targets from the in-camera
# JPEG (`ignore_bias=True` mode), for the strict L*/hue cap on the `hsl.plan_band`
# side.
# --------------------------------------------------------------------------- #
def test_embedded_band_targets_marks_embedded_raw_only_when_ignore_bias():
    from app.core.pipeline import RenderAnalysis
    from app.core.render_metrics import BandStats

    t = RenderAnalysis(
        tone=None, neutral=None,
        bands=[BandStats("Red", 0.5, 0.0, 40.0, 0.5, 0.0, 50.0)],
    )
    bias = ac.ProfileBias(n=8)
    bias.bands["Red"] = (0.0, 0.0, 0.0)

    tgs = ac._embedded_band_targets(t, bias, ignore_bias=True)
    assert tgs["Red"].embedded_raw is True

    # Historical mode (delta vs bias norm): no raw transplant → False.
    tgs = ac._embedded_band_targets(t, bias, ignore_bias=False)
    assert tgs["Red"].embedded_raw is False


def test_band_targets_from_seed_match_does_not_mark_embedded_raw():
    from app.core.seed_match import SeedTarget
    from app.core.render_metrics import BandStats

    t = SeedTarget(
        temperature=None, tint=None, tone=None,
        bands=[BandStats("Red", 0.5, 0.0, 40.0, 0.5, 0.0, 50.0)],
        shadow_tint=None, red_hue=None, red_saturation=None,
        green_hue=None, green_saturation=None, blue_hue=None, blue_saturation=None,
        n_matched=1, seed_ids=["s"],
    )
    tgs = ac._band_targets_from_seed_match(t)
    assert tgs["Red"].embedded_raw is False
