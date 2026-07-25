"""PLAN.md COV3 — `app/core/render_metrics.py` core measurement functions.

`tone_stats`/`neutral_stats`/`band_stats` were previously only exercised
indirectly (through cache/autocorrect tests using pre-built dataclasses).
This adds direct tests on small synthetic sRGB uint8 arrays with known
colorimetry, including the two clipped/degenerate-population fallback
branches.
"""

from __future__ import annotations

import numpy as np

from app.core.render_metrics import (
    BAND_NAMES,
    band_is_reliable,
    band_stats,
    neutral_stats,
    rgb_u8_to_hsv_hue_sat,
    srgb_u8_to_lab,
    tone_stats,
)


def _solid(rgb: tuple[int, int, int], size: int = 16) -> np.ndarray:
    arr = np.zeros((size, size, 3), dtype=np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = rgb
    return arr


# --------------------------------------------------------------------------- #
# tone_stats
# --------------------------------------------------------------------------- #
def test_tone_stats_mid_gray_lands_near_l50():
    img = _solid((128, 128, 128))
    stats = tone_stats(img)
    assert 45.0 < stats.median_l < 55.0
    assert stats.clipped_hi == 0.0
    assert stats.clipped_lo == 0.0
    assert stats.tonal_frac == 1.0


def test_tone_stats_all_highlight_clipped_falls_back_to_full_frame():
    """Every pixel >= 250 on some channel -> `tonal` mask empty -> the
    `vals.size == 0` branch re-uses the unfiltered L* array instead of NaN."""
    img = _solid((255, 255, 255))
    stats = tone_stats(img)
    assert stats.clipped_hi == 1.0
    assert stats.tonal_frac == 0.0
    assert stats.median_l > 90.0  # fallback still reflects the (bright) image


def test_tone_stats_accepts_precomputed_lab_and_mask():
    img = _solid((128, 128, 128), size=4)
    lab = srgb_u8_to_lab(img)
    mask = np.zeros((4, 4), dtype=bool)
    mask[0, 0] = True  # restrict to a single pixel
    stats = tone_stats(img, lab=lab, mask=mask)
    assert stats.tonal_frac == 1.0 / 16.0


# --------------------------------------------------------------------------- #
# neutral_stats
# --------------------------------------------------------------------------- #
def test_neutral_stats_gray_image_high_neutral_frac_near_zero_bias():
    img = _solid((128, 128, 128))
    lab = srgb_u8_to_lab(img)
    stats = neutral_stats(lab)
    assert stats.neutral_frac == 1.0
    assert abs(stats.a_bias) < 0.5
    assert abs(stats.b_bias) < 0.5
    assert stats.n_neutral == img.shape[0] * img.shape[1]


def test_neutral_stats_saturated_image_has_zero_neutral_pixels():
    """Pure red is far outside the neutral chroma window -> the `n == 0`
    branch returns an all-zero NeutralStats rather than NaN medians."""
    img = _solid((220, 20, 20))
    lab = srgb_u8_to_lab(img)
    stats = neutral_stats(lab)
    assert stats.n_neutral == 0
    assert stats == type(stats)(0.0, 0.0, 0.0, 0.0, 0)


def test_neutral_stats_respects_mask():
    img = _solid((128, 128, 128), size=4)
    lab = srgb_u8_to_lab(img)
    mask = np.zeros((4, 4), dtype=bool)
    mask[:2, :] = True  # half the pixels
    stats = neutral_stats(lab, mask=mask)
    assert stats.n_neutral == 8


# --------------------------------------------------------------------------- #
# rgb_u8_to_hsv_hue_sat
# --------------------------------------------------------------------------- #
def test_hue_sat_pure_red():
    img = _solid((255, 0, 0), size=2)
    hue, sat = rgb_u8_to_hsv_hue_sat(img)
    assert np.allclose(hue, 0.0, atol=1.0)
    assert np.allclose(sat, 1.0, atol=0.01)


def test_hue_sat_pure_green_and_blue():
    hue_g, _ = rgb_u8_to_hsv_hue_sat(_solid((0, 255, 0), size=2))
    hue_b, _ = rgb_u8_to_hsv_hue_sat(_solid((0, 0, 255), size=2))
    assert np.allclose(hue_g, 120.0, atol=1.0)
    assert np.allclose(hue_b, 240.0, atol=1.0)


def test_hue_sat_gray_has_zero_saturation():
    img = _solid((128, 128, 128), size=2)
    _, sat = rgb_u8_to_hsv_hue_sat(img)
    assert np.allclose(sat, 0.0)


# --------------------------------------------------------------------------- #
# band_stats
# --------------------------------------------------------------------------- #
def test_band_stats_pure_red_populates_only_red_band():
    img = _solid((220, 20, 20), size=8)
    bands = band_stats(img)
    assert len(bands) == 8
    by_name = {b.name: b for b in bands}
    assert by_name["Red"].frac == 1.0
    assert band_is_reliable(by_name["Red"])
    for name in BAND_NAMES:
        if name != "Red":
            assert by_name[name].frac == 0.0
            assert not band_is_reliable(by_name[name])


def test_band_stats_gray_image_all_bands_empty_not_error():
    img = _solid((128, 128, 128), size=8)
    bands = band_stats(img)
    assert all(b.frac == 0.0 for b in bands)
    # empty-band fallback still reports the nominal hue center, not NaN
    assert all(np.isfinite(b.median_hue) for b in bands)


def test_band_stats_respects_mask():
    img = np.zeros((8, 8, 3), dtype=np.uint8)
    img[:, :, :] = (220, 20, 20)  # solid red
    mask = np.zeros((8, 8), dtype=bool)
    mask[:4, :] = True  # only top half counted
    bands = band_stats(img, mask=mask)
    by_name = {b.name: b for b in bands}
    assert by_name["Red"].frac == 0.5


def test_band_stats_mixed_image_two_bands_populated():
    img = np.zeros((8, 8, 3), dtype=np.uint8)
    img[:4, :, :] = (220, 20, 20)   # top half red
    img[4:, :, :] = (20, 20, 220)   # bottom half blue
    bands = band_stats(img)
    by_name = {b.name: b for b in bands}
    assert by_name["Red"].frac == 0.5
    assert by_name["Blue"].frac == 0.5
    assert by_name["Green"].frac == 0.0
