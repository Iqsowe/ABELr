"""'Sharp zone' mask — restricts histogram measurements to the in-focus subject.

A Laplacian (high-pass filter) measures local sharpness; blurred areas (bokeh,
motion, out of depth-of-field) have a magnitude close to zero. We keep the
**top `SHARP_TOP_FRACTION`** sharpest pixels — `render_metrics`/`render_metrics_gpu`
compute tone/neutral/bands over this zone, so the histogram reflects the subject
rather than a blurred background.

**Resolution-proportional pre-Laplacian blur** (PLAN.md step R3): `luma` is
Gaussian-blurred (sigma proportional to the image diagonal) before the
Laplacian — a scale-space (Laplacian-of-Gaussian) edge detector, secondary
robustness on top of R2's common measurement grid against the exact resolution
chosen (a fixed sigma, like `tools/cluster_sharp_zone.py`'s `SIGMA_BLUR`, isn't
scale-invariant: tuned at one resolution, it over- or under-smooths at another).

Two identical implementations (same formula, same threshold):
- `sharp_mask`: numpy, used by the `tools/` scripts (CPU).
- `sharp_mask_gpu`: torch CUDA, used by the live GPU-strict path.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import torch

SHARP_TOP_FRACTION = 0.25  # top 25% sharpest pixels retained

# Gaussian sigma / image diagonal — resolution-proportional pre-Laplacian blur
# (R3). Ballpark derived from `tools/cluster_sharp_zone.py`'s SIGMA_BLUR=8,
# tuned at that script's "half-size" working resolution (~4200px diagonal on a
# 24MP a7IV) -> 8/4200 ~= 0.0019, rounded. At the R2 common grid (2048px long
# edge, ~2460px diagonal for 3:2) this gives sigma ~= 4.9px, a moderate blur.
_BLUR_SIGMA_RATIO = 0.002


def _blur_sigma(h: int, w: int) -> float:
    """Gaussian sigma for a given image size — proportional to the diagonal."""
    return _BLUR_SIGMA_RATIO * math.hypot(h, w)


def _gaussian_blur(luma: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return luma
    from scipy.ndimage import gaussian_filter

    return gaussian_filter(luma, sigma=sigma, mode="nearest")


def _laplacian_magnitude(luma: np.ndarray) -> np.ndarray:
    """|Laplacian| (4*center - N/S/E/W neighbors) over an HxW luminance map."""
    p = np.pad(luma, 1, mode="edge")
    lap = (
        4.0 * p[1:-1, 1:-1]
        - p[:-2, 1:-1]
        - p[2:, 1:-1]
        - p[1:-1, :-2]
        - p[1:-1, 2:]
    )
    return np.abs(lap)


def sharp_mask(luma: np.ndarray, top_fraction: float = SHARP_TOP_FRACTION) -> np.ndarray:
    """Bool mask HxW: True = pixel among the `top_fraction` sharpest.

    `luma`: 2D map (CIELAB L* for an sRGB render, or linear Y for a RAW).
    If the image is uniform (magnitude zero everywhere), everything is kept (no
    identifiable sharp zone → don't restrict).
    """
    h, w = luma.shape[:2]
    blurred = _gaussian_blur(luma.astype(np.float32), _blur_sigma(h, w))
    mag = _laplacian_magnitude(blurred)
    if not np.any(mag > 0):
        return np.ones(luma.shape, dtype=bool)
    threshold = np.quantile(mag, 1.0 - top_fraction)
    return mag >= threshold


def _gaussian_kernel1d_gpu(sigma: float, device) -> "torch.Tensor":
    import torch

    radius = max(1, int(round(3.0 * sigma)))
    x = torch.arange(-radius, radius + 1, dtype=torch.float32, device=device)
    k = torch.exp(-(x * x) / (2.0 * sigma * sigma))
    return k / k.sum()


def _gaussian_blur_gpu(luma: "torch.Tensor", sigma: float) -> "torch.Tensor":
    if sigma <= 0:
        return luma
    import torch
    import torch.nn.functional as F

    k1d = _gaussian_kernel1d_gpu(sigma, luma.device)
    pad = k1d.numel() // 2
    x = luma.float()[None, None]
    x = F.pad(x, (pad, pad, 0, 0), mode="reflect")
    x = F.conv2d(x, k1d.view(1, 1, 1, -1))
    x = F.pad(x, (0, 0, pad, pad), mode="reflect")
    x = F.conv2d(x, k1d.view(1, 1, -1, 1))
    return x[0, 0]


def sharp_mask_gpu(luma: torch.Tensor, top_fraction: float = SHARP_TOP_FRACTION) -> torch.Tensor:
    """CUDA equivalent of `sharp_mask`. `luma`: 2D tensor (H, W) float on GPU."""
    import torch

    h, w = luma.shape[-2], luma.shape[-1]
    blurred = _gaussian_blur_gpu(luma, _blur_sigma(h, w))
    p = torch.nn.functional.pad(blurred[None, None], (1, 1, 1, 1), mode="replicate")[0, 0]
    lap = 4.0 * p[1:-1, 1:-1] - p[:-2, 1:-1] - p[2:, 1:-1] - p[1:-1, :-2] - p[1:-1, 2:]
    mag = lap.abs()
    if not torch.any(mag > 0):
        return torch.ones_like(luma, dtype=torch.bool)
    flat = mag.reshape(-1)
    # torch.quantile caps the number of elements (~16M) — subsample beyond that
    # (same pattern as render_metrics_gpu._q; a large render/RAW quickly exceeds this).
    if flat.numel() > 8_000_000:
        flat = flat[:: (flat.numel() // 8_000_000 + 1)]
    threshold = torch.quantile(flat, 1.0 - top_fraction)
    return mag >= threshold
