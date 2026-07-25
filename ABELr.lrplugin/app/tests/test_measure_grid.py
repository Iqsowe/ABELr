"""PLAN.md step R5 — regression guard for the common measurement grid (R2) +
resolution-proportional blur (R3).

Deterministic, CPU-only (numpy + cv2), no GPU/RAW/Lr dependency: reproduces
the exact failure mode from PLAN.md's R evidence ("sharp drifts... and
degenerates toward global at native resolution") with a synthetic
native-resolution image (well above `MEASURE_LONG_EDGE`) carrying a known
bright, textured subject on a smooth, dark background. Downsampled to the
common grid exactly like the live pipeline (`render_metrics_gpu.
downsample_to_measure_grid` on GPU; `cv2.INTER_AREA` here is the CPU-side
reference — numerically equivalent area/box averaging), the sharp zone must
still track the subject's own lightness rather than collapse toward the
whole-frame global.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.core import render_metrics, sharpness

# Native-resolution-like canvas, well above MEASURE_LONG_EDGE (2048) — the
# real-world scale of a RAW/embedded in-camera JPEG (PLAN.md R evidence: RAW
# ~36.7MP, embedded JPEG ~32.7MP).
_NATIVE_H, _NATIVE_W = 4000, 6000
_BG_VALUE, _BG_NOISE = 60.0, 0.4     # smooth, dark background (mild sensor-noise floor)
_SUBJ_VALUE, _SUBJ_NOISE = 200.0, 45.0  # bright, genuinely textured subject


def _subject_on_background(h: int, w: int, block: int):
    """sRGB uint8 image: a smooth dark background with a bright, textured
    square subject in the center, occupying `block`x`block` pixels."""
    rng = np.random.default_rng(0)
    bg = _BG_VALUE + rng.normal(0.0, _BG_NOISE, size=(h, w, 1)).astype(np.float32)
    rgb = np.clip(np.repeat(bg, 3, axis=2), 0, 255).astype(np.uint8)
    half = block // 2
    cy, cx = h // 2, w // 2
    subj = _SUBJ_VALUE + rng.normal(0.0, _SUBJ_NOISE, size=(2 * half, 2 * half, 1)).astype(np.float32)
    subj_rgb = np.clip(np.repeat(subj, 3, axis=2), 0, 255).astype(np.uint8)
    rgb[cy - half:cy + half, cx - half:cx + half] = subj_rgb
    region = (slice(cy - half, cy + half), slice(cx - half, cx + half))
    return rgb, region


def _measure(rgb: np.ndarray):
    lab = render_metrics.srgb_u8_to_lab(rgb)
    mask = sharpness.sharp_mask(lab[..., 0])
    sharp = render_metrics.tone_stats(rgb, lab, mask=mask)
    glob = render_metrics.tone_stats(rgb, lab)
    return sharp, glob


def test_sharp_zone_tracks_subject_after_measurement_grid_downsample():
    # Subject occupies ~29% of the frame area (comfortably above
    # sharpness.SHARP_TOP_FRACTION=0.25) — large enough that the sharp-zone
    # budget CAN be filled by genuine subject texture rather than diluted by
    # the much larger background (cf. area-imbalance pitfall documented here
    # during test development: a too-small subject gets outvoted by raw pixel
    # count even when its own Laplacian magnitude is far higher).
    block = int(min(_NATIVE_H, _NATIVE_W) * 0.66)
    rgb, region = _subject_on_background(_NATIVE_H, _NATIVE_W, block)
    subject_only = render_metrics.tone_stats(rgb[region])

    long_edge = render_metrics.MEASURE_LONG_EDGE
    scale = long_edge / max(_NATIVE_H, _NATIVE_W)
    resized = cv2.resize(
        rgb, (round(_NATIVE_W * scale), round(_NATIVE_H * scale)), interpolation=cv2.INTER_AREA
    )

    sharp, glob = _measure(resized)
    gap = sharp.median_l - glob.median_l

    assert gap > 15.0, (
        f"sharp/global gap collapsed ({gap:.1f} L*) — sharp zone degenerated "
        "toward global (the exact PLAN.md R bug this guards against)"
    )
    assert abs(sharp.median_l - subject_only.median_l) < 10.0, (
        f"sharp zone ({sharp.median_l:.1f} L*) drifted away from the true "
        f"subject lightness ({subject_only.median_l:.1f} L*)"
    )


def test_measure_long_edge_is_the_documented_hypothesis():
    # Locks the constant to the value PLAN.md's R5 evidence validated (single-
    # photo + multi-photo scale->drift curve, near-zero drift at this scale).
    # A future change to this constant is fine — but must be a deliberate
    # re-validation (re-run PLAN.md's S0 harness + this test), not a silent drift.
    assert render_metrics.MEASURE_LONG_EDGE == 2048
