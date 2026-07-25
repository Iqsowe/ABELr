"""Render-space metrics — **torch port** of `render_metrics` (GPU or CPU).

Same measurements as `render_metrics` (tone L*, neutral a*/b*, HSL bands) and **same
dataclasses** (`ToneStats`, `NeutralStats`, `BandStats`), but all computation runs
on torch tensors instead of numpy. The device follows `gpu.device()` (GPU priority,
CPU fallback if no CUDA is usable — see `core/gpu.py`). On GPU, the input is a
**uint8 CHW RGB tensor already on device**, as produced by nvJPEG
(`torchvision.io.decode_jpeg(device='cuda')`) → no CPU round-trip between
decoding and measurement; on CPU, same path, CPU tensors.

The colorimetric constants (sRGB→XYZ→CIELAB, thresholds) are **imported** from
`render_metrics` so it stays the single source of truth; the port must reproduce
the numpy version (verified by `tools/validate_gpu_vs_libraw` / dedicated tests).
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from . import gpu
from . import render_metrics as rm
from .pipeline import RenderAnalysis, RenderAnalysisDual
from .render_metrics import BandStats, NeutralStats, ToneStats

MEASURE_LONG_EDGE = rm.MEASURE_LONG_EDGE

# Resolved once: GPU if available, otherwise CPU (gpu.device() never raises).
# Upstream modules (gpu_jpeg, gpu_schedule) already decode on this same device via
# gpu.device() — consistent, never a CUDA tensor mixed with a CPU computation.
_DEV = gpu.device()


def _const(arr) -> torch.Tensor:
    """numpy constant → float32 tensor on the current device (GPU or CPU)."""
    return torch.as_tensor(arr, dtype=torch.float32, device=_DEV)


# Matrices/constants (same values as render_metrics).
_SRGB_LIN_TO_XYZ = _const(rm._SRGB_LIN_TO_XYZ_D65)        # 3x3
_D65 = _const(rm._D65_WHITE)                              # (3,)
_BAND_CENTERS = _const(rm._BAND_CENTERS)                  # (8,)
_LAB_DELTA3 = float(rm._LAB_DELTA3)
_LAB_SLOPE = float(rm._LAB_SLOPE)
_LAB_OFFSET = float(rm._LAB_OFFSET)
_HIGHLIGHT_U8 = float(rm._HIGHLIGHT_U8)
_SHADOW_L = float(rm._SHADOW_L)
_NEUTRAL_CHROMA = float(rm._NEUTRAL_CHROMA)
_NEUTRAL_L_MIN = float(rm._NEUTRAL_L_MIN)
_NEUTRAL_L_MAX = float(rm._NEUTRAL_L_MAX)
_BAND_MIN_FRAC = float(rm._BAND_MIN_FRAC)
_BAND_NAMES = rm.BAND_NAMES


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def downsample_to_measure_grid(
    hwc_u8: torch.Tensor, long_edge: int = MEASURE_LONG_EDGE
) -> torch.Tensor:
    """Downsamples an HWC uint8 tensor so its long edge is at most `long_edge`.

    **Never upsamples** — a source already at or below the target grid (e.g.
    an Lr render already requested at `long_edge`, cf. R1) is returned as-is.
    Area/box averaging (`F.interpolate(mode="area")`): the GPU-resident
    equivalent of `cv2.INTER_AREA` for downsampling, staying on-device
    (GPU-strict, CLAUDE.md — no CPU round-trip for a cv2 call).
    """
    h, w = hwc_u8.shape[0], hwc_u8.shape[1]
    long_side = max(h, w)
    if long_side <= long_edge:
        return hwc_u8
    scale = long_edge / long_side
    new_h, new_w = max(1, round(h * scale)), max(1, round(w * scale))
    chw = hwc_u8.permute(2, 0, 1).unsqueeze(0).float()  # 1,C,H,W
    resized = F.interpolate(chw, size=(new_h, new_w), mode="area")
    return resized.squeeze(0).round().clamp(0, 255).to(torch.uint8).permute(1, 2, 0).contiguous()


def _to_hwc_u8(chw_u8: torch.Tensor) -> torch.Tensor:
    """nvJPEG/CPU outputs CHW uint8; we work in HWC. Force RGB 3 channels on the
    current device, then normalize to the common measurement grid (PLAN.md R2)."""
    if chw_u8.device != _DEV:
        chw_u8 = chw_u8.to(_DEV)
    if chw_u8.dim() == 3 and chw_u8.shape[0] in (1, 3):
        hwc = chw_u8.permute(1, 2, 0)
    else:
        hwc = chw_u8
    if hwc.shape[-1] == 1:
        hwc = hwc.expand(-1, -1, 3)
    return downsample_to_measure_grid(hwc.contiguous())


def _q(x: torch.Tensor, q: float) -> float:
    """Quantile (linear interpolation, like numpy) of a 1D tensor, as float.

    `torch.quantile` caps the number of elements (~16M); we subsample at a
    constant stride beyond that (a large render's global percentiles are insensitive to stride).
    """
    if x.numel() == 0:
        return 0.0
    if x.numel() > 8_000_000:
        x = x[:: (x.numel() // 8_000_000 + 1)]
    return float(torch.quantile(x.float(), q))


def _srgb_u8_to_lab(hwc_u8: torch.Tensor) -> torch.Tensor:
    """RGB uint8 sRGB (HWC) → CIELAB (HWC: L* 0-100, a*, b*) on CUDA."""
    x = hwc_u8.float() / 255.0
    a = 0.055
    lin = torch.where(x <= 0.04045, x / 12.92, ((x + a) / (1.0 + a)) ** 2.4)
    xyz = lin @ _SRGB_LIN_TO_XYZ.T
    t = xyz / _D65
    f = torch.where(t > _LAB_DELTA3, t.clamp_min(0).pow(1.0 / 3.0), _LAB_SLOPE * t + _LAB_OFFSET)
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    lab = torch.empty_like(xyz)
    lab[..., 0] = 116.0 * fy - 16.0
    lab[..., 1] = 500.0 * (fx - fy)
    lab[..., 2] = 200.0 * (fy - fz)
    return lab


def _hsv_hue_sat(hwc_u8: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Hue (deg 0-360) and HSV saturation (0-1) of a uint8 RGB HWC. Pure torch."""
    rgb = hwc_u8.float() / 255.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    cmax = rgb.amax(dim=-1)
    cmin = rgb.amin(dim=-1)
    delta = cmax - cmin
    sat = torch.where(cmax > 1e-6, delta / (cmax + 1e-9), torch.zeros_like(cmax))

    hue = torch.zeros_like(cmax)
    safe = delta > 1e-6
    idx_r = safe & (cmax == r)
    idx_g = safe & (cmax == g) & ~idx_r
    idx_b = safe & (cmax == b) & ~idx_r & ~idx_g
    d = delta + 1e-9
    hue = torch.where(idx_r, ((g - b) / d) % 6.0, hue)
    hue = torch.where(idx_g, ((b - r) / d) + 2.0, hue)
    hue = torch.where(idx_b, ((r - g) / d) + 4.0, hue)
    return (hue * 60.0) % 360.0, sat


# --------------------------------------------------------------------------- #
# 1. Tone (exposure L*)
# --------------------------------------------------------------------------- #
def tone_stats(
    hwc_u8: torch.Tensor, lab: torch.Tensor, mask: torch.Tensor | None = None
) -> ToneStats:
    lstar = lab[..., 0]
    clipped_hi_mask = (hwc_u8 >= _HIGHLIGHT_U8).any(dim=-1)
    clipped_lo_mask = lstar <= _SHADOW_L
    tonal = (~clipped_hi_mask) & (~clipped_lo_mask)
    if mask is not None:
        tonal &= mask

    vals = lstar[tonal]
    if vals.numel() == 0:
        vals = lstar.reshape(-1)
    return ToneStats(
        median_l=_q(vals, 0.5),
        mean_l=float(vals.mean()),
        p05_l=_q(vals, 0.05),
        p95_l=_q(vals, 0.95),
        clipped_hi=float(clipped_hi_mask.float().mean()),
        clipped_lo=float(clipped_lo_mask.float().mean()),
        tonal_frac=float(tonal.float().mean()),
    )


# --------------------------------------------------------------------------- #
# 2. Neutral (WB cast on near-neutrals)
# --------------------------------------------------------------------------- #
def neutral_stats(lab: torch.Tensor, mask: torch.Tensor | None = None) -> NeutralStats:
    lstar = lab[..., 0]
    chroma = torch.hypot(lab[..., 1], lab[..., 2])
    neutral_mask = (chroma < _NEUTRAL_CHROMA) & (lstar >= _NEUTRAL_L_MIN) & (lstar <= _NEUTRAL_L_MAX)
    if mask is not None:
        neutral_mask &= mask
    n = int(neutral_mask.sum())
    if n == 0:
        return NeutralStats(0.0, 0.0, 0.0, 0.0, 0)
    a = lab[..., 1][neutral_mask]
    b = lab[..., 2][neutral_mask]
    return NeutralStats(
        a_bias=_q(a, 0.5),
        b_bias=_q(b, 0.5),
        chroma=_q(torch.hypot(a, b), 0.5),
        neutral_frac=float(neutral_mask.float().mean()),
        n_neutral=n,
    )


# --------------------------------------------------------------------------- #
# 3. HSL bands
# --------------------------------------------------------------------------- #
def band_stats(
    hwc_u8: torch.Tensor, lab: torch.Tensor, mask: torch.Tensor | None = None
) -> list[BandStats]:
    hue, sat = _hsv_hue_sat(hwc_u8)
    chroma = torch.hypot(lab[..., 1], lab[..., 2])
    lstar = lab[..., 0]

    colored = chroma >= _NEUTRAL_CHROMA
    if mask is not None:
        colored &= mask
    diff = (hue.unsqueeze(-1) - _BAND_CENTERS).abs()
    circ = torch.minimum(diff, 360.0 - diff)
    band_idx = circ.argmin(dim=-1)
    total = hue.numel()

    out: list[BandStats] = []
    for i, name in enumerate(_BAND_NAMES):
        m = colored & (band_idx == i)
        n = int(m.sum())
        if n == 0:
            out.append(BandStats(name, 0.0, float(rm._BAND_CENTERS[i]), 0.0, 0.0, 0.0, 0.0))
            continue
        sat_m = sat[m]
        out.append(
            BandStats(
                name=name,
                frac=float(n / total),
                median_hue=_q(hue[m], 0.5),
                median_chroma=_q(chroma[m], 0.5),
                median_sat=_q(sat_m, 0.5),
                sat_clip_frac=float((sat_m >= 0.97).float().mean()),
                median_l=_q(lstar[m], 0.5),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Composition (GPU equivalent of pipeline.analyze_rendered)
# --------------------------------------------------------------------------- #
def analyze_rendered_gpu(chw_u8: torch.Tensor) -> RenderAnalysis:
    """Analyze a render decoded on GPU (uint8 CHW) in a single CUDA Lab pass.

    Restricts tone/neutral/bands to the **sharp zone** (sharpest top 25%,
    `sharpness.sharp_mask_gpu` on L*) — excludes bokeh/background blur
    from the measured histogram.
    """
    from . import sharpness

    hwc = _to_hwc_u8(chw_u8)
    lab = _srgb_u8_to_lab(hwc)
    mask = sharpness.sharp_mask_gpu(lab[..., 0])
    return RenderAnalysis(
        tone=tone_stats(hwc, lab, mask=mask),
        neutral=neutral_stats(lab, mask=mask),
        bands=band_stats(hwc, lab, mask=mask),
    )


def analyze_rendered_gpu_dual(chw_u8: torch.Tensor) -> RenderAnalysisDual:
    """GPU equivalent of `pipeline.analyze_rendered_dual`: global + sharp zone.

    A single CUDA Lab conversion + a single sharpness map, shared between the
    two scales (global = `mask=None`, sharp = sharp mask).
    """
    from . import sharpness

    hwc = _to_hwc_u8(chw_u8)
    lab = _srgb_u8_to_lab(hwc)
    mask = sharpness.sharp_mask_gpu(lab[..., 0])
    glob = RenderAnalysis(
        tone=tone_stats(hwc, lab, mask=None),
        neutral=neutral_stats(lab, mask=None),
        bands=band_stats(hwc, lab, mask=None),
    )
    sharp = RenderAnalysis(
        tone=tone_stats(hwc, lab, mask=mask),
        neutral=neutral_stats(lab, mask=mask),
        bands=band_stats(hwc, lab, mask=mask),
    )
    return RenderAnalysisDual(sharp=sharp, glob=glob, mask_sharp_frac=float(mask.float().mean()))
