"""Integration of the "calib" (Calibration) axis in `core.autocorrect.plan()` —
k-NN transplant from the seeds, active in both reference modes
(unlike expo/wb/hsl, which only have a measurable target in seeds mode or
via the embedded neutral anchor).
"""

from __future__ import annotations

from app.core import autocorrect as ac
from app.core.pipeline import RenderAnalysis
from app.core.render_metrics import NeutralStats, ToneStats
from app.core.seed_match import SeedVector


def _tone(median_l: float = 50.0) -> ToneStats:
    return ToneStats(median_l, median_l, median_l - 5, median_l + 5, 0.0, 0.0, 1.0)


def _neutral() -> NeutralStats:
    return NeutralStats(a_bias=0.0, b_bias=0.0, chroma=0.0, neutral_frac=0.0, n_neutral=0)


def _analysis() -> RenderAnalysis:
    return RenderAnalysis(tone=_tone(), neutral=_neutral(), bands=[])


def _seed_with_calib(pid="seed1", **calib) -> SeedVector:
    return SeedVector(
        photo_id=pid, asshot_rg=0.5, asshot_bg=0.5, raw_median_l=50.0,
        temperature=5500.0, tint=0.0, preview_tone=_tone(), preview_bands=None,
        **calib,
    )


def test_plan_seeds_mode_transplants_calibration():
    seed_pool = [_seed_with_calib(shadow_tint=8.0, red_hue=-15.0, blue_saturation=20.0)]
    target = ac.PhotoMeasure(
        photo_id="p1", path="p1.ARW", current_develop={}, exif_camera="ILCE-7M3",
        analysis=_analysis(), raw_tone=_tone(), asshot_rg=0.5, asshot_bg=0.5,
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
    target = ac.PhotoMeasure(
        photo_id="p1", path="p1.ARW", current_develop={}, exif_camera="ILCE-7M3",
        raw_tone=_tone(), asshot_rg=0.5, asshot_bg=0.5,
    )
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
    target = ac.PhotoMeasure(
        photo_id="p1", path="p1.ARW", current_develop={}, exif_camera="ILCE-7M3",
        raw_tone=_tone(), asshot_rg=0.5, asshot_bg=0.5,
    )
    adjustments, diag = ac.plan(
        [target], axes=frozenset({"calib"}), forced_embedded=True, model=None, seed_pool=None,
    )
    assert adjustments == []
    assert any("calib" in note and "skipped" in note for note in diag.notes)


def test_plan_seeds_mode_ignores_calib_axis_when_no_seed_has_calibration():
    seed_pool = [_seed_with_calib()]  # no calib field filled in
    target = ac.PhotoMeasure(
        photo_id="p1", path="p1.ARW", current_develop={}, exif_camera="ILCE-7M3",
        analysis=_analysis(), raw_tone=_tone(), asshot_rg=0.5, asshot_bg=0.5,
    )
    adjustments, _diag = ac.plan(
        [target], axes=frozenset({"calib"}), model=None, seed_pool=seed_pool,
    )
    assert adjustments == []
