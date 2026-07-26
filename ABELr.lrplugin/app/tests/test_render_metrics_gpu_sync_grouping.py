"""PLAN.md G2 — bit-exactness test for the grouped-sync rewrite of
`tone_stats`/`neutral_stats`/`band_stats`.

Profiling (torch.cuda.set_sync_debug_mode('warn') on a real-shaped synthetic
2048-ish render) confirmed the hypothesis: 192 device->host syncs per
`analyze_rendered_gpu_dual` call, ~81ms/call — most of them individual
`.item()`/`float()`/`int()` pulls inside a per-band loop (8 bands x up to 6
each). The fix groups every such pull per function into ONE `torch.stack(...)
.tolist()` (tone_stats, neutral_stats) or one combined stack across the whole
band loop (band_stats) — changes ONLY *when* a value crosses to host, never
*how* it's computed, so the two implementations must be bit-exact.

`_legacy_*` below are frozen copies of the pre-G2 implementations. Do NOT
update them when `render_metrics_gpu.py` changes — that would defeat the
point of the parity check.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
import torch

from app.core import render_metrics as rm
from app.core import render_metrics_gpu as rmg
from app.core.render_metrics import BandStats, NeutralStats, ToneStats


def _assert_close(got, want) -> None:
    """Field-by-field comparison for a dataclass instance: str fields exact,
    numeric fields via pytest.approx (pytest.approx doesn't natively support
    arbitrary dataclasses, only numbers/sequences/mappings)."""
    assert type(got) is type(want)
    for f in dataclasses.fields(got):
        gv, wv = getattr(got, f.name), getattr(want, f.name)
        if isinstance(gv, str):
            assert gv == wv, f"{f.name}: {gv!r} != {wv!r}"
        else:
            assert gv == pytest.approx(wv), f"{f.name}: {gv!r} != {wv!r}"


# --------------------------------------------------------------------------- #
# Frozen legacy implementations (pre-G2, one .item()-style pull per value)
# --------------------------------------------------------------------------- #
def _legacy_q(x: torch.Tensor, q: float) -> float:
    if x.numel() == 0:
        return 0.0
    if x.numel() > 8_000_000:
        x = x[:: (x.numel() // 8_000_000 + 1)]
    return float(torch.quantile(x.float(), q))


def _legacy_tone_stats(hwc_u8, lab, mask=None) -> ToneStats:
    lstar = lab[..., 0]
    clipped_hi_mask = (hwc_u8 >= rmg._HIGHLIGHT_U8).any(dim=-1)
    clipped_lo_mask = lstar <= rmg._SHADOW_L
    tonal = (~clipped_hi_mask) & (~clipped_lo_mask)
    if mask is not None:
        tonal &= mask
    vals = lstar[tonal]
    if vals.numel() == 0:
        vals = lstar.reshape(-1)
    return ToneStats(
        median_l=_legacy_q(vals, 0.5),
        mean_l=float(vals.mean()),
        p05_l=_legacy_q(vals, 0.05),
        p95_l=_legacy_q(vals, 0.95),
        clipped_hi=float(clipped_hi_mask.float().mean()),
        clipped_lo=float(clipped_lo_mask.float().mean()),
        tonal_frac=float(tonal.float().mean()),
    )


def _legacy_neutral_stats(lab, mask=None) -> NeutralStats:
    lstar = lab[..., 0]
    chroma = torch.hypot(lab[..., 1], lab[..., 2])
    neutral_mask = (
        (chroma < rmg._NEUTRAL_CHROMA) & (lstar >= rmg._NEUTRAL_L_MIN) & (lstar <= rmg._NEUTRAL_L_MAX)
    )
    if mask is not None:
        neutral_mask &= mask
    n = int(neutral_mask.sum())
    if n == 0:
        return NeutralStats(0.0, 0.0, 0.0, 0.0, 0)
    a = lab[..., 1][neutral_mask]
    b = lab[..., 2][neutral_mask]
    return NeutralStats(
        a_bias=_legacy_q(a, 0.5),
        b_bias=_legacy_q(b, 0.5),
        chroma=_legacy_q(torch.hypot(a, b), 0.5),
        neutral_frac=float(neutral_mask.float().mean()),
        n_neutral=n,
    )


def _legacy_band_stats(hwc_u8, lab, mask=None) -> list[BandStats]:
    hue, sat = rmg._hsv_hue_sat(hwc_u8)
    chroma = torch.hypot(lab[..., 1], lab[..., 2])
    lstar = lab[..., 0]

    colored = chroma >= rmg._NEUTRAL_CHROMA
    if mask is not None:
        colored &= mask
    diff = (hue.unsqueeze(-1) - rmg._BAND_CENTERS).abs()
    circ = torch.minimum(diff, 360.0 - diff)
    band_idx = circ.argmin(dim=-1)
    total = hue.numel()

    out: list[BandStats] = []
    for i, name in enumerate(rmg._BAND_NAMES):
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
                median_hue=_legacy_q(hue[m], 0.5),
                median_chroma=_legacy_q(chroma[m], 0.5),
                median_sat=_legacy_q(sat_m, 0.5),
                sat_clip_frac=float((sat_m >= 0.97).float().mean()),
                median_l=_legacy_q(lstar[m], 0.5),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Synthetic inputs
# --------------------------------------------------------------------------- #
def _synthetic_render(h: int, w: int, seed: int) -> torch.Tensor:
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
    return torch.from_numpy(arr).to(rmg._DEV)


def _synthetic_all_neutral_gray(h: int, w: int) -> torch.Tensor:
    """Every pixel mid-gray: exercises the "colored"/"neutral" masks landing on
    every band being empty (band_stats' n==0 branch) and neutral_stats' full mask."""
    arr = np.full((h, w, 3), 128, dtype=np.uint8)
    return torch.from_numpy(arr).to(rmg._DEV)


@pytest.mark.parametrize("h, w, seed", [(64, 96, 1), (33, 50, 7), (200, 150, 99)])
def test_tone_stats_matches_legacy(h, w, seed):
    hwc = _synthetic_render(h, w, seed)
    lab = rmg._srgb_u8_to_lab(hwc)
    for mask in (None, torch.rand(h, w, device=rmg._DEV) > 0.5):
        got = rmg.tone_stats(hwc, lab, mask=mask)
        want = _legacy_tone_stats(hwc, lab, mask=mask)
        _assert_close(got, want)


@pytest.mark.parametrize("h, w, seed", [(64, 96, 1), (33, 50, 7), (200, 150, 99)])
def test_neutral_stats_matches_legacy(h, w, seed):
    hwc = _synthetic_render(h, w, seed)
    lab = rmg._srgb_u8_to_lab(hwc)
    for mask in (None, torch.rand(h, w, device=rmg._DEV) > 0.5):
        got = rmg.neutral_stats(lab, mask=mask)
        want = _legacy_neutral_stats(lab, mask=mask)
        _assert_close(got, want)


def test_neutral_stats_matches_legacy_when_mask_all_false():
    hwc = _synthetic_render(40, 40, 3)
    lab = rmg._srgb_u8_to_lab(hwc)
    mask = torch.zeros(40, 40, dtype=torch.bool, device=rmg._DEV)
    got = rmg.neutral_stats(lab, mask=mask)
    want = _legacy_neutral_stats(lab, mask=mask)
    _assert_close(got, want)
    _assert_close(got, NeutralStats(0.0, 0.0, 0.0, 0.0, 0))


@pytest.mark.parametrize("h, w, seed", [(64, 96, 1), (33, 50, 7), (200, 150, 99)])
def test_band_stats_matches_legacy(h, w, seed):
    hwc = _synthetic_render(h, w, seed)
    lab = rmg._srgb_u8_to_lab(hwc)
    for mask in (None, torch.rand(h, w, device=rmg._DEV) > 0.5):
        got = rmg.band_stats(hwc, lab, mask=mask)
        want = _legacy_band_stats(hwc, lab, mask=mask)
        assert len(got) == len(want)
        for g, w_ in zip(got, want):
            _assert_close(g, w_)


def test_band_stats_matches_legacy_all_bands_empty():
    """Every band takes the n==0 branch (no gather, no host-crossing quantile
    at all) — must still match legacy exactly, including the numpy-derived
    band-center default (never a GPU sync in either implementation)."""
    hwc = _synthetic_all_neutral_gray(30, 40)
    lab = rmg._srgb_u8_to_lab(hwc)
    got = rmg.band_stats(hwc, lab)
    want = _legacy_band_stats(hwc, lab)
    assert len(got) == len(want) == len(rmg._BAND_NAMES)
    for g, w_ in zip(got, want):
        _assert_close(g, w_)


def test_analyze_rendered_gpu_dual_matches_field_by_field_with_legacy_pieces():
    """End-to-end sanity: analyze_rendered_gpu_dual composes the same three
    grouped functions, global + sharp scope — no separate parity needed
    beyond the three unit tests above, but this pins the composition."""
    from app.core import sharpness

    hwc_chw = _synthetic_render(64, 96, 11).permute(2, 0, 1)  # CHW, as decode_blobs produces
    result = rmg.analyze_rendered_gpu_dual(hwc_chw)

    hwc = rmg._to_hwc_u8(hwc_chw)
    lab = rmg._srgb_u8_to_lab(hwc)
    mask = sharpness.sharp_mask_gpu(lab[..., 0])

    _assert_close(result.glob.tone, _legacy_tone_stats(hwc, lab, mask=None))
    _assert_close(result.sharp.tone, _legacy_tone_stats(hwc, lab, mask=mask))
