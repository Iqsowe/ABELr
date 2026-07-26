"""PLAN.md G1a — parity test for the `process_bayer_gpu` measure-grid change.

G1b drops the full-resolution Lab + sharp-mask computation (and the
`hwc_measure is hwc_u8` shortcut) from `process_bayer_gpu`, computing
`hwc_measure`/`lab_measure`/`sharp_measure` unconditionally instead. The
claim is that this is value-preserving for `tone`/`bands`/`exposure`/
`grayworld_*`/`asshot_*`: when the shortcut fired, `hwc_measure` *was*
`hwc_u8`, so the same deterministic functions on the same tensor give the
same result. `exposure_sharp`/`grayworld_*_sharp`/`mask_sharp_frac` are
deliberately NOT compared — G1b changes their semantics on purpose (dropped
/ recomputed on the measure grid instead of full res).

`_legacy_process_bayer_gpu` is a frozen, self-contained re-implementation of
today's `process_bayer_gpu` (reusing only gpu_raw's private demosaic/matrix
helpers, which G1b does not touch) — comparing two code paths on the same
device is device-agnostic (gpu.device() returns CPU without CUDA, per
CLAUDE.md), unlike hard-coded golden floats which would be CUDA-only or
brittle. Must stay green both before and after G1b lands; this file itself
must not change when G1b does.
"""

from __future__ import annotations

import numpy as np
import pytest
import torch

from app.core import gpu, gpu_raw, render_metrics_gpu, sharpness
from app.core.gpu_raw import RawBayer, RawGpuResult


def _synthetic_bayer(h: int, w: int, seed: int) -> RawBayer:
    rng = np.random.default_rng(seed)
    bayer = rng.integers(64, 16000, size=(h, w)).astype(np.uint16)
    pattern = np.array([[0, 1], [3, 2]], dtype=np.int64)  # RGGB-style, 4-letter color_desc
    color_desc = "RGBG"
    wb = (2048.0, 1024.0, 1536.0, 1024.0)
    black = (64.0, 64.0, 64.0, 64.0)
    white = 16383.0
    # Diagonal-dominant, invertible — plausible XYZ(D65)->camera matrix shape.
    cam_xyz = np.array(
        [[0.90, 0.02, 0.05], [0.05, 0.85, 0.03], [0.04, 0.01, 0.92]], dtype=np.float32
    )
    return RawBayer(bayer, pattern, color_desc, wb, black, white, cam_xyz)


def _legacy_process_bayer_gpu(rb: RawBayer) -> RawGpuResult:
    """Frozen copy of `gpu_raw.process_bayer_gpu` as of PLAN.md G1 (pre-change):
    always computes the full-res Lab + sharp mask, and reuses them for the
    measure grid via the `hwc_measure is hwc_u8` shortcut when no downsampling
    is needed. Do NOT edit this to follow future changes to `process_bayer_gpu`
    — that would defeat the point of the parity check."""
    dev = gpu.device()
    H, W = rb.bayer.shape

    bayer = torch.from_numpy(rb.bayer).to(dev).to(torch.float32)
    pat = torch.from_numpy(rb.pattern.astype(np.int64)).to(dev)
    idx = pat.repeat((H + 1) // 2, (W + 1) // 2)[:H, :W]

    black_v = torch.tensor(rb.black, dtype=torch.float32, device=dev)
    wb = list(rb.wb)
    if len(wb) > 3 and wb[3] == 0:
        wb[3] = wb[1]
    wb_arr = torch.tensor(wb, dtype=torch.float32, device=dev)
    green = wb_arr[1] if wb_arr[1] != 0 else torch.tensor(1.0, device=dev)
    wb_norm = wb_arr / green

    black_map = black_v[idx]
    denom = (rb.white - black_map).clamp_min(1.0)
    val = ((bayer - black_map).clamp_min(0.0) / denom) * wb_norm[idx]

    letter_to_c = {"R": 0, "G": 1, "B": 2}
    chan_of_index = torch.tensor(
        [letter_to_c[rb.color_desc[i]] for i in range(len(rb.color_desc))],
        dtype=torch.int64, device=dev,
    )
    chan_map = chan_of_index[idx]

    cam_rgb = gpu_raw._demosaic_bilinear(val, chan_map)
    M = torch.from_numpy(gpu_raw._cam_to_prophoto(rb.cam_xyz)).to(dev)
    flat = cam_rgb.reshape(3, -1).T
    pp = (flat @ M.T).clamp(0.0, 1.0)

    from app.core import color
    y_w = torch.tensor(color.PROPHOTO_TO_Y, dtype=torch.float32, device=dev)
    luma = pp @ y_w

    from app.core.analysis import _HIGHLIGHT_CLIP, _SHADOW_CLIP, ExposureStats

    def _exposure(pp_sub, luma_sub):
        n = luma_sub.numel()
        if n == 0:
            return ExposureStats(0.0, 0.0, 0.0, 0.0)
        return ExposureStats(
            mean_luma=float(luma_sub.mean()),
            median_luma=render_metrics_gpu._q(luma_sub, 0.5),
            clipped_highlights=float((pp_sub >= _HIGHLIGHT_CLIP).any(dim=-1).sum()) / n,
            clipped_shadows=float((luma_sub <= _SHADOW_CLIP).sum()) / n,
        )

    def _grayworld(pp_sub):
        if pp_sub.numel() == 0:
            return 0.0, 0.0
        mean_rgb = pp_sub.mean(dim=0) + 1e-9
        return float(mean_rgb[1] / mean_rgb[0]), float(mean_rgb[1] / mean_rgb[2])

    exposure = _exposure(pp, luma)
    grayworld_rg, grayworld_bg = _grayworld(pp)

    pp_hw3 = pp.reshape(H, W, 3)
    hwc_u8 = gpu_raw._prophoto_linear_to_srgb_u8_gpu(pp_hw3)
    lab = render_metrics_gpu._srgb_u8_to_lab(hwc_u8)
    sharp = sharpness.sharp_mask_gpu(lab[..., 0])

    # The shortcut under test: reuse full-res lab/sharp when no downsampling occurs.
    hwc_measure = render_metrics_gpu.downsample_to_measure_grid(hwc_u8)
    if hwc_measure is hwc_u8:
        lab_measure, sharp_measure = lab, sharp
    else:
        lab_measure = render_metrics_gpu._srgb_u8_to_lab(hwc_measure)
        sharp_measure = sharpness.sharp_mask_gpu(lab_measure[..., 0])
    tone = render_metrics_gpu.tone_stats(hwc_measure, lab_measure, mask=sharp_measure)
    bands = render_metrics_gpu.band_stats(hwc_measure, lab_measure, mask=sharp_measure)

    mask_flat = sharp.reshape(-1)
    exposure_sharp = _exposure(pp[mask_flat], luma[mask_flat])
    grayworld_rg_sharp, grayworld_bg_sharp = _grayworld(pp[mask_flat])
    mask_sharp_frac = float(sharp.float().mean())

    g = rb.wb[1] or 1.0
    return RawGpuResult(
        exposure=exposure,
        grayworld_rg=grayworld_rg,
        grayworld_bg=grayworld_bg,
        asshot_rg=rb.wb[0] / g,
        asshot_bg=rb.wb[2] / g,
        tone=tone,
        bands=bands,
        exposure_sharp=exposure_sharp,
        grayworld_rg_sharp=grayworld_rg_sharp,
        grayworld_bg_sharp=grayworld_bg_sharp,
        mask_sharp_frac=mask_sharp_frac,
    )


def _assert_tone_equal(a, b):
    assert a.median_l == pytest.approx(b.median_l)
    assert a.mean_l == pytest.approx(b.mean_l)
    assert a.p05_l == pytest.approx(b.p05_l)
    assert a.p95_l == pytest.approx(b.p95_l)
    assert a.clipped_hi == pytest.approx(b.clipped_hi)
    assert a.clipped_lo == pytest.approx(b.clipped_lo)
    assert a.tonal_frac == pytest.approx(b.tonal_frac)


def _assert_bands_equal(a, b):
    assert len(a) == len(b)
    for ba, bb in zip(a, b):
        assert ba.name == bb.name
        assert ba.median_l == pytest.approx(bb.median_l)
        assert ba.median_hue == pytest.approx(bb.median_hue)
        assert ba.median_chroma == pytest.approx(bb.median_chroma)
        assert ba.median_sat == pytest.approx(bb.median_sat)
        assert ba.sat_clip_frac == pytest.approx(bb.sat_clip_frac)
        assert ba.frac == pytest.approx(bb.frac)


@pytest.mark.parametrize("h, w", [(1600, 2400), (800, 1200)])  # above and below the 2048 grid
def test_process_bayer_gpu_matches_legacy_on_measure_fields(h, w):
    rb = _synthetic_bayer(h, w, seed=42)
    got = gpu_raw.process_bayer_gpu(rb)
    want = _legacy_process_bayer_gpu(rb)

    _assert_tone_equal(got.tone, want.tone)
    _assert_bands_equal(got.bands, want.bands)
    assert got.exposure.mean_luma == pytest.approx(want.exposure.mean_luma)
    assert got.exposure.median_luma == pytest.approx(want.exposure.median_luma)
    assert got.exposure.clipped_highlights == pytest.approx(want.exposure.clipped_highlights)
    assert got.exposure.clipped_shadows == pytest.approx(want.exposure.clipped_shadows)
    assert got.grayworld_rg == pytest.approx(want.grayworld_rg)
    assert got.grayworld_bg == pytest.approx(want.grayworld_bg)
    assert got.asshot_rg == pytest.approx(want.asshot_rg)
    assert got.asshot_bg == pytest.approx(want.asshot_bg)
