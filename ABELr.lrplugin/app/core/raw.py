"""Sony ARW RAW decoding via rawpy (LibRaw).

Analysis fallback path: used by `image_source` when no Smart Preview exists for
the photo.

Two outputs:
- `load_linear`: float32 0-1 **scene-linear**, **ProPhoto** primaries (wide
  gamut) → the analysis working space (exposure, white balance). No gamma, no
  8-bit or gamut clipping, sensor clipping point preserved.
- `load_rgb`: uint8 sRGB display-referred → GUI display / thumbnails.

The `use_camera_wb=True` and `output_color=ProPhoto` choices are validated by
calibration on a real catalog (see `core/color`). The analysis itself lives in
`analysis.py`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rawpy

from . import color


# Settings common to both decodes:
# - `no_auto_bright`: no auto-exposure, otherwise the measured luma would be
#   meaningless.
# - `use_camera_wb`: applies the "as shot" white balance. Essential — without
#   it, the per-channel ratios measure the sensor's spectral imbalance, not the
#   scene (gray-world becomes uninterpretable). With it, gray-world measures the
#   residual cast vs. as-shot = the useful signal.
_COMMON = dict(no_auto_bright=True, use_camera_wb=True)


def load_linear(
    path: str | Path,
    half_size: bool = True,
    color_space: str = color.ANALYSIS_COLOR_SPACE,
) -> np.ndarray:
    """Decodes a RAW (.ARW) into **float32 0-1 scene-linear** RGB.

    `gamma=(1, 1)` disables the transfer curve; `output_color` sets the
    primaries (default ProPhoto, wide gamut — avoids sRGB gamut clipping
    biasing the white balance). `half_size` speeds up decoding (1/4 the pixels)
    without changing the global statistics.

    Returns an HxWx3 float32 array in [0, 1].
    """
    with rawpy.imread(str(path)) as raw:
        rgb16 = raw.postprocess(
            half_size=half_size,
            output_bps=16,
            gamma=(1, 1),
            output_color=getattr(rawpy.ColorSpace, color_space),
            **_COMMON,
        )
    return rgb16.astype(np.float32) / 65535.0


def read_asshot_wb(path: str | Path) -> tuple[float, float]:
    """Reads the camera's "as shot" white balance (RGGB multipliers).

    Returns (r/g, b/g) — the ratios normalized to the camera WB's green channel,
    the physical input signal of the WB model (`core.wb_model`). Anchored to the
    camera body: for a given body these ratios predict the chosen Temperature
    (near-universal slope, see ILCE-7M4 calibration). Independent of scene content.
    """
    with rawpy.imread(str(path)) as raw:
        wb = list(raw.camera_whitebalance)  # [R, G1, B, G2]
    g = wb[1] or 1.0
    return wb[0] / g, wb[2] / g


def load_thumbnail(path: str | Path) -> np.ndarray | None:
    """Extracts the embedded JPEG thumbnail if available (fast, for GUI preview)."""
    try:
        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
    except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
        return None
    if thumb.format == rawpy.ThumbFormat.JPEG:
        import cv2

        data = np.frombuffer(thumb.data, dtype=np.uint8)
        bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if bgr is None:  # corrupted embedded JPEG (Fable 5 review C-03)
            return None
        return bgr[:, :, ::-1]  # BGR->RGB
    if thumb.format == rawpy.ThumbFormat.BITMAP:
        return thumb.data
    return None
