"""RAW decoding **on GPU** — bayer → linear ProPhoto → stats (torch CUDA).

Replaces `raw.load_linear` + `analysis` (the CPU LibRaw path) with a GPU pipeline:

1. **CPU (thin, irreducible)**: `rawpy` decompresses/unpacks the ARW container
   into a 16-bit bayer plane and exposes the metadata (CFA pattern, as-shot WB,
   black/white levels, color matrix). No GPU codec exists for Sony ARW → this
   step stays on CPU, but does **not** demosaic.
2. **GPU (all pixel compute)**: black-level subtraction + normalization,
   per-CFA-site WB, **demosaic** (normalized convolution = bilinear), camera→
   ProPhoto matrix (replicates the dcraw `cam_xyz_coeff` composition), → linear
   ProPhoto float32 RGB. Exposure stats (Y) and gray-world computed on the GPU
   tensor.

Parity with LibRaw (same ProPhoto primaries, same `use_camera_wb`): to be
confirmed via `tools/validate_gpu_vs_libraw`. The color matrix and chromatic
adaptation are the sensitive points — isolated here to be adjustable.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
import torch.nn.functional as F

from . import color, gpu, render_metrics_gpu, sharpness
from .analysis import (
    _HIGHLIGHT_CLIP,
    _SHADOW_CLIP,
    ExposureStats,
)
from .render_metrics import BandStats, ToneStats
from .render_metrics_gpu import _q

# Linear ProPhoto(D50) → linear sRGB(D65) — same matrix as color.PROPHOTO_TO_SRGB,
# used here to give the RAW a u8 sRGB representation comparable (Lab/bands) to
# InCameraJPEG/PreviewJPEG, never used for exposure/WB analysis (ProPhoto only).
_PP_TO_SRGB = torch.from_numpy(color.PROPHOTO_TO_SRGB)

# ProPhoto(D50) → XYZ(D65): output primaries, D65-adapted like the dcraw table.
_PP_TO_XYZ_D65 = (color._BRADFORD_D50_D65 @ color._PP_TO_XYZ_D50).astype(np.float32)

# Bilinear demosaic kernel (convolution normalized by neighbor count).
_BILINEAR_K = torch.tensor(
    [[[[1.0, 2.0, 1.0], [2.0, 4.0, 2.0], [1.0, 2.0, 1.0]]]], dtype=torch.float32
)


@dataclass
class RawBayer:
    """Output of the CPU unpack — **picklable**, lives in host RAM (for the scheduler)."""

    bayer: np.ndarray       # uint16 HxW (visible area)
    pattern: np.ndarray     # 2x2 int: color index per CFA site
    color_desc: str         # e.g. "RGBG"
    wb: tuple               # camera_whitebalance [R, G1, B, G2]
    black: tuple            # black_level_per_channel (4)
    white: float            # white_level (scalar)
    cam_xyz: np.ndarray     # 3x3: XYZ(D65) → camera (rgb_xyz_matrix[:3,:3])


@dataclass
class RawGpuResult:
    exposure: ExposureStats               # GLOBAL exposure (whole frame, ProPhoto Y)
    grayworld_rg: float                   # GLOBAL gray-world
    grayworld_bg: float
    asshot_rg: float
    asshot_bg: float
    tone: ToneStats | None = None         # sharp zone, sRGB derived from the RAW
    bands: list[BandStats] | None = None  # sharp zone, sRGB derived from the RAW
    exposure_sharp: ExposureStats | None = None  # SHARP ZONE exposure (Laplacian mask)
    grayworld_rg_sharp: float | None = None       # SHARP ZONE gray-world
    grayworld_bg_sharp: float | None = None
    mask_sharp_frac: float | None = None          # fraction of pixels retained (diagnostic)


def _prophoto_linear_to_srgb_u8_gpu(pp_hw3: torch.Tensor) -> torch.Tensor:
    """Linear ProPhoto (H,W,3) CUDA → sRGB uint8 (H,W,3) CUDA. Display/histogram
    comparison only (never for exposure/WB, which stay in ProPhoto)."""
    M = _PP_TO_SRGB.to(pp_hw3.device)
    srgb_lin = (pp_hw3 @ M.T).clamp(0.0, 1.0)
    a = 0.055
    srgb = torch.where(
        srgb_lin <= 0.0031308, 12.92 * srgb_lin, (1 + a) * srgb_lin.clamp_min(0).pow(1 / 2.4) - a
    )
    return (srgb.clamp(0.0, 1.0) * 255.0).round().to(torch.uint8)


# --------------------------------------------------------------------------- #
# CPU: thin unpack (no demosaic)
# --------------------------------------------------------------------------- #
def bayer_from_open(r) -> RawBayer:
    """RawBayer from an ALREADY-open rawpy handle.

    Extracted for the scheduler's unified unpack (Fable 5 review P-02): the same
    rawpy open serves both the bayer AND the camera JPEG (`embedded_jpeg.extract_from_open`).
    """
    bayer = r.raw_image_visible.copy()           # uint16 HxW
    pattern = np.asarray(r.raw_pattern).copy()   # 2x2
    color_desc = r.color_desc.decode("ascii")    # "RGBG"
    wb = tuple(float(x) for x in r.camera_whitebalance)
    black = tuple(float(x) for x in r.black_level_per_channel)
    white = float(r.white_level)
    cam_xyz = np.asarray(r.rgb_xyz_matrix, np.float32)[:3, :3].copy()
    return RawBayer(bayer, pattern, color_desc, wb, black, white, cam_xyz)


def unpack_raw(path: str) -> RawBayer | None:
    """Unpacks the RAW via rawpy (CPU): bayer + metadata. None if unreadable.

    Module-level function → picklable for a process pool (see `gpu_schedule`).
    """
    import rawpy

    try:
        with rawpy.imread(str(path)) as r:
            return bayer_from_open(r)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Camera → ProPhoto matrix (replicates dcraw cam_xyz_coeff)
# --------------------------------------------------------------------------- #
def _cam_to_prophoto(cam_xyz: np.ndarray) -> np.ndarray:
    """3x3: camera-RGB → linear ProPhoto.

    dcraw: cam_rgb = cam_xyz · (ProPhoto→XYZ_D65), row-normalized (sum=1, =
    camera white point), then inverted → camera→ProPhoto. Reproduces LibRaw's
    `output_color=ProPhoto` color conversion.
    """
    cam_rgb = cam_xyz @ _PP_TO_XYZ_D65          # 3x3: ProPhoto → camera
    row_sums = cam_rgb.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    cam_rgb_n = cam_rgb / row_sums              # white-point normalization
    return np.linalg.inv(cam_rgb_n).astype(np.float32)  # camera → ProPhoto


# --------------------------------------------------------------------------- #
# GPU: black-level, WB, demosaic, matrix, stats
# --------------------------------------------------------------------------- #
def _demosaic_bilinear(val: torch.Tensor, chan_map: torch.Tensor) -> torch.Tensor:
    """Demosaic via normalized convolution. `val` HxW (0-1, WB applied), `chan_map`
    HxW in {0,1,2}. Returns (3,H,W) linear camera-RGB."""
    K = _BILINEAR_K.to(val.device)
    planes = []
    v4 = val.unsqueeze(0).unsqueeze(0)  # 1,1,H,W
    for c in range(3):
        mask = (chan_map == c).to(val.dtype)
        num = F.conv2d((v4 * mask), K, padding=1)
        den = F.conv2d(mask.unsqueeze(0).unsqueeze(0), K, padding=1)
        planes.append((num / (den + 1e-8)).squeeze(0).squeeze(0))
    return torch.stack(planes, dim=0)  # 3,H,W


def process_bayer_gpu(rb: RawBayer) -> RawGpuResult:
    """Full GPU pipeline for an unpacked bayer → exposure + gray-world stats."""
    dev = gpu.device()
    H, W = rb.bayer.shape

    # H2D as uint16 (48 MB) then cast to float32 ON GPU — instead of an astype
    # float32 on the CPU side, which doubled PCIe traffic and allocated 96 MB
    # host-side (Fable 5 review P-06).
    bayer = torch.from_numpy(rb.bayer).to(dev).to(torch.float32)
    pat = torch.from_numpy(rb.pattern.astype(np.int64)).to(dev)          # 2x2
    idx = pat.repeat((H + 1) // 2, (W + 1) // 2)[:H, :W]                 # HxW index 0..3

    black_v = torch.tensor(rb.black, dtype=torch.float32, device=dev)    # (4,)
    # WB normalized to green (index 1): neutral → (g,g,g).
    # dcraw/LibRaw convention: cam_mul[G2]==0 means "G2 = G1" — without this
    # guard the G2 sites would be multiplied by 0 (green channel corrupted at
    # demosaic). No-op on Sony ARW (G2=G1 already), breaks other camera bodies
    # otherwise (C-01).
    wb = list(rb.wb)
    if len(wb) > 3 and wb[3] == 0:
        wb[3] = wb[1]
    wb_arr = torch.tensor(wb, dtype=torch.float32, device=dev)
    green = wb_arr[1] if wb_arr[1] != 0 else torch.tensor(1.0, device=dev)
    wb_norm = wb_arr / green

    black_map = black_v[idx]
    denom = (rb.white - black_map).clamp_min(1.0)
    val = ((bayer - black_map).clamp_min(0.0) / denom) * wb_norm[idx]    # HxW, WB applied

    # CFA color index → RGB channel 0/1/2 via color_desc.
    letter_to_c = {"R": 0, "G": 1, "B": 2}
    chan_of_index = torch.tensor(
        [letter_to_c[rb.color_desc[i]] for i in range(len(rb.color_desc))],
        dtype=torch.int64, device=dev,
    )
    chan_map = chan_of_index[idx]                                        # HxW in {0,1,2}

    cam_rgb = _demosaic_bilinear(val, chan_map)                         # 3,H,W camera
    M = torch.from_numpy(_cam_to_prophoto(rb.cam_xyz)).to(dev)          # 3x3 camera→ProPhoto
    # (3,H,W) → (H*W,3) @ M.T → ProPhoto, clamped [0,1] (parity with LibRaw output range).
    flat = cam_rgb.reshape(3, -1).T                                     # N,3
    pp = (flat @ M.T).clamp(0.0, 1.0)                                   # N,3 linear ProPhoto

    # Exposure (Y from ProPhoto XYZ) — same weights/thresholds as analysis.exposure_stats.
    y_w = torch.tensor(color.PROPHOTO_TO_Y, dtype=torch.float32, device=dev)
    luma = pp @ y_w                                                     # N

    def _exposure(pp_sub: torch.Tensor, luma_sub: torch.Tensor) -> ExposureStats:
        """ExposureStats over a subset of pixels (global or sharp zone)."""
        n = luma_sub.numel()
        if n == 0:
            return ExposureStats(0.0, 0.0, 0.0, 0.0)
        return ExposureStats(
            mean_luma=float(luma_sub.mean()),
            median_luma=_q(luma_sub, 0.5),
            clipped_highlights=float((pp_sub >= _HIGHLIGHT_CLIP).any(dim=-1).sum()) / n,
            clipped_shadows=float((luma_sub <= _SHADOW_CLIP).sum()) / n,
        )

    def _grayworld(pp_sub: torch.Tensor) -> tuple[float, float]:
        """ProPhoto gray-world (g/r, g/b) — same as analysis.gray_world_wb."""
        if pp_sub.numel() == 0:
            return 0.0, 0.0
        mean_rgb = pp_sub.mean(dim=0) + 1e-9
        return float(mean_rgb[1] / mean_rgb[0]), float(mean_rgb[1] / mean_rgb[2])

    # Global (whole frame).
    exposure = _exposure(pp, luma)
    grayworld_rg, grayworld_bg = _grayworld(pp)

    # Sharp-zone tone/bands — sRGB derived from ProPhoto, comparable to JPEGs
    # (camera/preview). Measured directly on the common measurement grid
    # (PLAN.md G1b) — no full-resolution Lab/sharp-mask pass: at 33 Mpx those
    # two ops alone cost ~146ms (44% of process_bayer_gpu), and fed only
    # exposure_sharp/grayworld_*_sharp, which were write-only in the cache
    # (no non-test code read them) — dropped here rather than kept unused.
    # Provably value-preserving for tone/bands vs. the old shortcut: when the
    # old "hwc_measure is hwc_u8" shortcut fired (no downsampling needed),
    # hwc_measure already equaled hwc_u8, so computing lab/sharp directly on
    # hwc_measure unconditionally gives the same result (see G1a's parity test).
    pp_hw3 = pp.reshape(H, W, 3)
    hwc_u8 = _prophoto_linear_to_srgb_u8_gpu(pp_hw3)
    hwc_measure = render_metrics_gpu.downsample_to_measure_grid(hwc_u8)
    lab_measure = render_metrics_gpu._srgb_u8_to_lab(hwc_measure)
    sharp_measure = sharpness.sharp_mask_gpu(lab_measure[..., 0])
    tone = render_metrics_gpu.tone_stats(hwc_measure, lab_measure, mask=sharp_measure)
    bands = render_metrics_gpu.band_stats(hwc_measure, lab_measure, mask=sharp_measure)

    exposure_sharp = None
    grayworld_rg_sharp = None
    grayworld_bg_sharp = None
    mask_sharp_frac = float(sharp_measure.float().mean())

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


def analyze_raw_gpu(path: str) -> RawGpuResult | None:
    """CPU unpack + GPU processing of a RAW. None if unreadable."""
    rb = unpack_raw(path)
    if rb is None:
        return None
    return process_bayer_gpu(rb)
