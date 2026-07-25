"""Metrics in **render space** (output-referred) — foundation of the refactor.

Scene-linear RAW (`raw.py` / `analysis.py`) measures the physics of the scene.
But the perceived exposure and color balance that the photographer judges live in
the **render**: after DCP profile + tone curve + sliders, encoded sRGB display.
This module therefore measures on the **rendered JPEG** (Lr preview or `requestJpegThumbnail`),
decoded as sRGB uint8 RGB.

Three families of measurements, all consumed by exposure / wb (refinement) / hsl:

1. `tone_stats`  — robust perceived **CIE L*** brightness (mid-tone median, clipping excluded)
                   → exposure target and deviation.
2. `neutral_stats` — residual a*/b* bias on **near-neutral pixels** only
                   → WB refinement (never a global gray-world, cf. n=1142 dead end).
3. `band_stats`  — chroma / lightness / hue per **HSL hue band** (8 Lr channels)
                   → HSL planning (oversaturation, luminance, hue recentering).

Colorimetry: sRGB (IEC 61966-2-1) → XYZ(D65) → CIELAB(D65). Standard constants,
not invented. The **band centers** for HSL and the slider response are *nominal*
here (measurement); their precise calibration happens in `response.py` + validation scripts.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# --------------------------------------------------------------------------- #
# Common measurement grid (PLAN.md step R) — single source of truth
# --------------------------------------------------------------------------- #
# Long-edge cap (px) every measured source (RAW, embedded in-camera JPEG, Lr
# render/neutral anchor) is normalized to before sharp_mask/tone_stats/
# band_stats/neutral_stats: the 4 sources previously lived at radically
# different resolutions (RAW ~36.7MP, embedded JPEG ~32.7MP, Lr render 512px
# default) and got differenced against each other — the sharp-zone mask
# degenerates toward global at native resolution (PLAN.md R evidence).
# Starting hypothesis (single-photo measurement, near-zero sharp/global drift
# at this scale) confirmed by R5's multi-photo scale->drift curve + live Lr
# timing — see PLAN.md step R5 for the evidence.
MEASURE_LONG_EDGE = 2048

# --------------------------------------------------------------------------- #
# sRGB → CIELAB (D65) colorimetry. Standard matrices/constants.
# --------------------------------------------------------------------------- #
# Linear sRGB (Rec.709 primaries, D65 white) → XYZ. IEC 61966-2-1.
_SRGB_LIN_TO_XYZ_D65 = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    np.float32,
)

# D65 reference white (CIE 1931, 2°).
_D65_WHITE = np.array([0.95047, 1.0, 1.08883], np.float32)

# Threshold/slope of the CIELAB f() function (δ = 6/29).
_LAB_DELTA = 6.0 / 29.0
_LAB_DELTA3 = _LAB_DELTA**3
_LAB_SLOPE = 1.0 / (3.0 * _LAB_DELTA**2)  # = 7.787...
_LAB_OFFSET = 4.0 / 29.0


def srgb_u8_to_linear(u8: np.ndarray) -> np.ndarray:
    """sRGB uint8 → linear float32 [0, 1] (inverse sRGB EOTF)."""
    x = u8.astype(np.float32) / 255.0
    a = 0.055
    return np.where(x <= 0.04045, x / 12.92, ((x + a) / (1.0 + a)) ** 2.4)


def _lab_f(t: np.ndarray) -> np.ndarray:
    return np.where(t > _LAB_DELTA3, np.cbrt(t), _LAB_SLOPE * t + _LAB_OFFSET)


def srgb_u8_to_lab(rgb_u8: np.ndarray) -> np.ndarray:
    """sRGB uint8 RGB (HxWx3, RGB order) → CIELAB (HxWx3: L* 0-100, a*, b*)."""
    lin = srgb_u8_to_linear(rgb_u8)
    xyz = lin @ _SRGB_LIN_TO_XYZ_D65.T
    f = _lab_f(xyz / _D65_WHITE)
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    lab = np.empty_like(xyz)
    lab[..., 0] = 116.0 * fy - 16.0
    lab[..., 1] = 500.0 * (fx - fy)
    lab[..., 2] = 200.0 * (fy - fz)
    return lab


# --------------------------------------------------------------------------- #
# 1. Exposure — robust perceived L* brightness
# --------------------------------------------------------------------------- #
# "Highlight-clipped" pixel: an sRGB channel nearly saturated (sky/specular).
_HIGHLIGHT_U8 = 250
# Shadow floor in L*: below this, the pixel carries no useful tonal info.
_SHADOW_L = 5.0


@dataclass
class ToneStats:
    """Perceived brightness of a render (CIE L*, 0-100).

    median_l    : L* median of **tonal** pixels (excluding HL clipping / dead shadows).
                  Main exposure metric (target = seed median).
    mean_l      : tonal L* mean (photographic key, complement).
    p05_l/p95_l : 5th/95th tonal L* percentiles (tonal spread).
    clipped_hi  : fraction of pixels with an sRGB channel ≥ 250 (blown).
    clipped_lo  : fraction of pixels with L* ≤ 5 (crushed).
    tonal_frac  : fraction of pixels retained as tonal.
    """

    median_l: float
    mean_l: float
    p05_l: float
    p95_l: float
    clipped_hi: float
    clipped_lo: float
    tonal_frac: float


def tone_stats(
    rgb_u8: np.ndarray, lab: np.ndarray | None = None, mask: np.ndarray | None = None
) -> ToneStats:
    """Robust perceived brightness of a rendered sRGB uint8 RGB.

    Excludes clipped highlights (sky, specular) and dead shadows, which don't
    reflect the intended exposure level, then computes statistics on the rest.
    `lab` can be supplied to avoid a reconversion (otherwise computed). `mask`
    (HxW bool, e.g. sharp zone `sharpness.sharp_mask`) further restricts the
    retained pixels if provided.
    """
    if lab is None:
        lab = srgb_u8_to_lab(rgb_u8)
    lstar = lab[..., 0]

    clipped_hi_mask = (rgb_u8 >= _HIGHLIGHT_U8).any(axis=-1)
    clipped_lo_mask = lstar <= _SHADOW_L
    tonal = ~clipped_hi_mask & ~clipped_lo_mask
    if mask is not None:
        tonal &= mask

    vals = lstar[tonal]
    if vals.size == 0:  # entirely clipped render: fall back to everything
        vals = lstar.reshape(-1)
    return ToneStats(
        median_l=float(np.median(vals)),
        mean_l=float(vals.mean()),
        p05_l=float(np.percentile(vals, 5)),
        p95_l=float(np.percentile(vals, 95)),
        clipped_hi=float(clipped_hi_mask.mean()),
        clipped_lo=float(clipped_lo_mask.mean()),
        tonal_frac=float(tonal.mean()),
    )


# --------------------------------------------------------------------------- #
# 2. WB — residual bias on near-neutral pixels
# --------------------------------------------------------------------------- #
# Max chroma (C* = hypot(a*, b*)) for a pixel to count as "neutral".
_NEUTRAL_CHROMA = 10.0
# Lightness window for usable neutrals (avoids noisy black / clipped white).
_NEUTRAL_L_MIN, _NEUTRAL_L_MAX = 20.0, 92.0


@dataclass
class NeutralStats:
    """Residual color cast measured on the near-neutral pixels of a render.

    a_bias / b_bias : a*/b* median of the neutrals (target = 0 → no cast).
                      a*>0 = magenta, a*<0 = green; b*>0 = yellow, b*<0 = blue.
    chroma          : C* median of the neutrals (cast residual magnitude).
    neutral_frac    : fraction of pixels judged neutral (WB refinement reliability).
    n_neutral       : number of neutral pixels.
    """

    a_bias: float
    b_bias: float
    chroma: float
    neutral_frac: float
    n_neutral: int


def neutral_stats(
    lab: np.ndarray,
    chroma_max: float = _NEUTRAL_CHROMA,
    l_min: float = _NEUTRAL_L_MIN,
    l_max: float = _NEUTRAL_L_MAX,
    mask: np.ndarray | None = None,
) -> NeutralStats:
    """Measures the residual cast **on neutrals only**.

    **Never** does a global gray-world (contaminated by content — proven n=1142
    dead end). The caller decides via `neutral_frac` whether the WB refinement is
    reliable; otherwise it keeps the seed prediction. `mask` (HxW bool, e.g. sharp
    zone) further restricts the retained pixels if provided.
    """
    lstar = lab[..., 0]
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    neutral_mask = (chroma < chroma_max) & (lstar >= l_min) & (lstar <= l_max)
    if mask is not None:
        neutral_mask &= mask
    n = int(neutral_mask.sum())
    if n == 0:
        return NeutralStats(0.0, 0.0, 0.0, 0.0, 0)
    a = lab[..., 1][neutral_mask]
    b = lab[..., 2][neutral_mask]
    return NeutralStats(
        a_bias=float(np.median(a)),
        b_bias=float(np.median(b)),
        chroma=float(np.median(np.hypot(a, b))),
        neutral_frac=float(neutral_mask.mean()),
        n_neutral=n,
    )


# --------------------------------------------------------------------------- #
# 3. HSL — statistics per hue band (8 Lr channels)
# --------------------------------------------------------------------------- #
# Lr order of the 8 HSL bands.
BAND_NAMES = ("Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta")
# NOMINAL hue centers (HSV degrees) — approximate. The exact boundaries and
# slider response are calibrated in response.py / validation scripts.
_BAND_CENTERS = np.array([0.0, 35.0, 60.0, 135.0, 180.0, 225.0, 275.0, 315.0], np.float32)
# Minimum population for a band's stats to be usable.
_BAND_MIN_FRAC = 0.01


def rgb_u8_to_hsv_hue_sat(rgb_u8: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Hue (0-360 degrees) and HSV saturation (0-1) of a uint8 RGB. Pure numpy."""
    rgb = rgb_u8.astype(np.float32) / 255.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    cmax = rgb.max(axis=-1)
    cmin = rgb.min(axis=-1)
    delta = cmax - cmin
    sat = np.where(cmax > 1e-6, delta / (cmax + 1e-9), 0.0)

    hue = np.zeros_like(cmax)
    safe = delta > 1e-6
    # Sector dominated by R / G / B.
    idx_r = safe & (cmax == r)
    idx_g = safe & (cmax == g) & ~idx_r
    idx_b = safe & (cmax == b) & ~idx_r & ~idx_g
    hue[idx_r] = (((g - b) / (delta + 1e-9)) % 6.0)[idx_r]
    hue[idx_g] = (((b - r) / (delta + 1e-9)) + 2.0)[idx_g]
    hue[idx_b] = (((r - g) / (delta + 1e-9)) + 4.0)[idx_b]
    return (hue * 60.0) % 360.0, sat


def _nearest_band(hue_deg: np.ndarray) -> np.ndarray:
    """Index 0-7 of the nearest band by circular hue distance."""
    diff = np.abs(hue_deg[..., None] - _BAND_CENTERS[None, :])
    circ = np.minimum(diff, 360.0 - diff)
    return circ.argmin(axis=-1)


@dataclass
class BandStats:
    """Statistics of an HSL hue band on a render.

    name        : Lr name of the band (slider key).
    frac        : fraction of pixels (population — reliability).
    median_hue  : HSV median hue (degrees) — drift vs. nominal center.
    median_chroma : CIELAB C* median chroma (perceptual saturation measure).
    median_sat  : HSV median saturation (0-1) — quick proxy.
    sat_clip_frac : fraction of near-saturated pixels (S ≥ 0.97) — oversaturation.
    median_l    : band's median L* lightness.
    """

    name: str
    frac: float
    median_hue: float
    median_chroma: float
    median_sat: float
    sat_clip_frac: float
    median_l: float


def band_stats(
    rgb_u8: np.ndarray,
    lab: np.ndarray | None = None,
    min_chroma: float = _NEUTRAL_CHROMA,
    mask: np.ndarray | None = None,
) -> list[BandStats]:
    """Stats per HSL band. Near-neutral pixels (C* < `min_chroma`) are excluded
    (a gray's hue is meaningless). `mask` (HxW bool, e.g. sharp zone) further
    restricts the retained pixels if provided. Returns 8 `BandStats` (empty
    bands → frac=0).
    """
    if lab is None:
        lab = srgb_u8_to_lab(rgb_u8)
    hue, sat = rgb_u8_to_hsv_hue_sat(rgb_u8)
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    lstar = lab[..., 0]

    colored = chroma >= min_chroma
    if mask is not None:
        colored &= mask
    band_idx = _nearest_band(hue)
    total = hue.size

    out: list[BandStats] = []
    for i, name in enumerate(BAND_NAMES):
        m = colored & (band_idx == i)
        n = int(m.sum())
        if n == 0:
            out.append(BandStats(name, 0.0, float(_BAND_CENTERS[i]), 0.0, 0.0, 0.0, 0.0))
            continue
        out.append(
            BandStats(
                name=name,
                frac=float(n / total),
                median_hue=float(np.median(hue[m])),
                median_chroma=float(np.median(chroma[m])),
                median_sat=float(np.median(sat[m])),
                sat_clip_frac=float((sat[m] >= 0.97).mean()),
                median_l=float(np.median(lstar[m])),
            )
        )
    return out


def band_is_reliable(band: BandStats, min_frac: float = _BAND_MIN_FRAC) -> bool:
    """Band usable for an HSL correction (sufficient population)."""
    return band.frac >= min_frac
