"""Exposure / white balance analysis (numpy) on linear ProPhoto RAW.

`exposure_stats` / `gray_world_wb` — physical source, float32 produced by
`image_source.load_for_analysis`. Independent of the applied style.

Consumed by `gui.autocorrect_worker` (`ev100`, `ExposureStats`) and by GPU parity
(`core.gpu_raw` reuses the clipping thresholds). Correction computation lives in
`core.seed_match` / `core.wb_model` / `core.autocorrect`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from . import color

# Clipping thresholds in **linear** (0-1). Highlights: a channel nearly saturated.
# Shadows: near-zero luminance. Adjustable depending on the target render.
_HIGHLIGHT_CLIP = 0.99
_SHADOW_CLIP = 0.0008


@dataclass
class ExposureStats:
    """Exposure metrics of an image (**linear 0-1** scale)."""

    mean_luma: float           # mean Y luminance (linear)
    median_luma: float         # median Y luminance (linear)
    clipped_highlights: float  # fraction of pixels with a channel ≥ 0.99
    clipped_shadows: float     # fraction of pixels with luminance ≤ 0.0008


def exposure_stats(rgb: np.ndarray) -> ExposureStats:
    """Exposure metrics of a linear ProPhoto RGB.

    Luminance via XYZ's Y (exact, gamut-independent). Highlight clipping is
    detected per channel (a single saturated channel is enough), shadows on Y.
    """
    luma = color.luminance(rgb)
    total = luma.size
    return ExposureStats(
        mean_luma=float(luma.mean()),
        median_luma=float(np.median(luma)),
        clipped_highlights=float((rgb >= _HIGHLIGHT_CLIP).any(axis=-1).sum() / total),
        clipped_shadows=float((luma <= _SHADOW_CLIP).sum() / total),
    )


def parse_shutter_seconds(shutter: str | float | None) -> float | None:
    """Converts an EXIF shutter speed into seconds.

    Accepts `"1/200"`, `"0.5"`, `"1\""` (sometimes formatted by the Lr SDK) or a float.
    Returns None if not interpretable.
    """
    if shutter is None:
        return None
    if isinstance(shutter, (int, float)):
        return float(shutter) if shutter > 0 else None
    # French-localized Lr formats slow shutter speeds with a comma ("0,4 s") —
    # normalize before float() (Fable 5 review A-03).
    s = str(shutter).strip().rstrip('"s ').strip().replace(",", ".")
    try:
        if "/" in s:
            num, den = s.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else None
        v = float(s)
        return v if v > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def ev100(
    iso: float | int | None,
    aperture: float | None,
    shutter: str | float | None,
) -> float | None:
    """Exposure Value normalized to ISO 100 from the EXIF exposure triangle.

    `EV100 = log2(aperture² / t) - log2(ISO/100)` where `t` = exposure time (s).
    Measures the scene's ambient light **independently** of the pixels — serves as
    scene context (bright sun ≈ 15-16, indoor ≈ 5-8, night < 3) for the k-NN
    matching and to interpret a deliberate underexposure bias. None if some
    data is missing or invalid.
    """
    t = parse_shutter_seconds(shutter)
    if not iso or not aperture or aperture <= 0 or t is None or t <= 0 or iso <= 0:
        return None
    import math

    return math.log2(aperture * aperture / t) - math.log2(iso / 100.0)


def gray_world_wb(rgb: np.ndarray) -> tuple[float, float]:
    """Gray-world white balance estimate, on **linear** RGB.

    Gray-world hypothesis: on average the scene is neutral. Returns
    (g_over_r_gain, g_over_b_gain) — the residual cast relative to gray, basis for
    suggesting Temperature/Tint. Input must be linear (otherwise gamma bias) and
    in a wide gamut (otherwise clipping bias on saturated colors).
    """
    rgb_f = rgb.astype(np.float32) + 1e-9
    mean_r = rgb_f[..., 0].mean()
    mean_g = rgb_f[..., 1].mean()
    mean_b = rgb_f[..., 2].mean()
    return float(mean_g / mean_r), float(mean_g / mean_b)


