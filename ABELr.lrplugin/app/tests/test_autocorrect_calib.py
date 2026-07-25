"""Integration of the "calib" (Calibration) axis in `core.autocorrect.plan()` —
k-NN transplant from the seeds, active in both reference modes
(unlike expo/wb/hsl, which only have a measurable target in seeds mode or
via the embedded neutral anchor).
"""

from __future__ import annotations

from app.core import autocorrect as ac
from app.tests.conftest import make_analysis, make_measure, make_seed, make_tone


def _seed_with_calib(pid="seed1", **calib):
    return make_seed(pid, **calib)


def test_plan_seeds_mode_transplants_calibration():
    seed_pool = [_seed_with_calib(shadow_tint=8.0, red_hue=-15.0, blue_saturation=20.0)]
    target = make_measure(
        "p1", analysis=make_analysis(), raw_tone=make_tone(), asshot_rg=0.5, asshot_bg=0.5,
    )
    adjustments, diag = ac.plan(
        [target], axes=frozenset({"calib"}), model=None, seed_pool=seed_pool,
    )
    assert diag.mode == "seeds"
    assert len(adjustments) == 1
    dev = adjustments[0].develop
    assert dev["EnableCalibration"] is True
    assert dev["ShadowTint"] == 10  # 8.0 snapped to the nearest 5-unit step
    assert dev["RedHue"] == -15     # already on-grid
    assert dev["BlueSaturation"] == 20
    assert "RedSaturation" not in dev  # not seeded → not written


def test_plan_embedded_mode_still_transplants_calibration_via_seeds():
    # Forced embedded mode, NO T/N anchor at all (no embedded_*/neutral_*): expo/wb/hsl
    # would have nothing to correct, but calib must still k-NN-match on RAW.
    seed_pool = [_seed_with_calib(green_hue=12.0)]
    target = make_measure("p1", raw_tone=make_tone(), asshot_rg=0.5, asshot_bg=0.5)
    adjustments, diag = ac.plan(
        [target], axes=frozenset({"calib"}), forced_embedded=True,
        model=None, seed_pool=seed_pool,
    )
    assert diag.mode == "embedded"
    assert len(adjustments) == 1
    dev = adjustments[0].develop
    assert dev["GreenHue"] == 10  # 12.0 snapped to the nearest 5-unit step
    assert dev["EnableCalibration"] is True


def test_plan_embedded_mode_no_seed_pool_skips_calib_axis():
    target = make_measure("p1", raw_tone=make_tone(), asshot_rg=0.5, asshot_bg=0.5)
    adjustments, diag = ac.plan(
        [target], axes=frozenset({"calib"}), forced_embedded=True, model=None, seed_pool=None,
    )
    assert adjustments == []
    assert any("calib" in note and "skipped" in note for note in diag.notes)


def test_plan_seeds_mode_ignores_calib_axis_when_no_seed_has_calibration():
    seed_pool = [_seed_with_calib()]  # no calib field filled in
    target = make_measure(
        "p1", analysis=make_analysis(), raw_tone=make_tone(), asshot_rg=0.5, asshot_bg=0.5,
    )
    adjustments, _diag = ac.plan(
        [target], axes=frozenset({"calib"}), model=None, seed_pool=seed_pool,
    )
    assert adjustments == []
