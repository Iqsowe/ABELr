"""Sharp-zone mask (`core.sharpness`) — top-fraction Laplacian selection +
PLAN.md step R3's resolution-proportional pre-Laplacian blur.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core import sharpness as sh


def _checkerboard_on_gradient(h: int, w: int, block: int = 300, cell: int = 15) -> np.ndarray:
    """Smooth linear-gradient background (near-zero Laplacian) with a hard-edge
    checkerboard block in the center (the "sharp subject"). A touch of noise
    avoids the degenerate exact-tie quantile behavior a perfectly flat/linear
    synthetic background would produce (real photos always have sensor noise)."""
    half = block // 2
    rng = np.random.default_rng(0)
    luma = np.zeros((h, w), np.float32)
    xx = np.arange(w)[None, :]
    luma[:, :] = (xx / w) * 100.0 + rng.normal(0.0, 0.05, size=(h, w)).astype(np.float32)
    cy, cx = h // 2, w // 2
    yy_ck = np.arange(2 * half)[:, None] // cell
    xx_ck = np.arange(2 * half)[None, :] // cell
    checker = ((yy_ck + xx_ck) % 2) * 80.0
    luma[cy - half:cy + half, cx - half:cx + half] = checker
    return luma, (slice(cy - half, cy + half), slice(cx - half, cx + half))


# --------------------------------------------------------------------------- #
# _blur_sigma — resolution-proportional (R3)
# --------------------------------------------------------------------------- #
def test_blur_sigma_scales_with_diagonal():
    small = sh._blur_sigma(600, 900)
    large = sh._blur_sigma(1200, 1800)  # exactly 2x the diagonal
    assert large == pytest.approx(2.0 * small, rel=1e-6)


def test_blur_sigma_positive_for_nonzero_image():
    assert sh._blur_sigma(1200, 1800) > 0.0


# --------------------------------------------------------------------------- #
# sharp_mask (CPU) — uniform image, sharp-vs-smooth concentration
# --------------------------------------------------------------------------- #
def test_sharp_mask_uniform_image_keeps_everything():
    luma = np.full((100, 150), 42.0, np.float32)
    mask = sh.sharp_mask(luma)
    assert mask.all()


def test_sharp_mask_top_fraction_respected():
    luma, _block = _checkerboard_on_gradient(600, 900)
    mask = sh.sharp_mask(luma)
    assert mask.mean() == pytest.approx(sh.SHARP_TOP_FRACTION, abs=0.02)


def test_sharp_mask_concentrates_on_genuine_texture():
    # The checkerboard occupies a small fraction of the frame but must be
    # (almost) entirely selected — the flat gradient region must not be
    # preferentially selected (its Laplacian is ~0 everywhere).
    h, w = 600, 900
    luma, block = _checkerboard_on_gradient(h, w)
    mask = sh.sharp_mask(luma)
    block_frac = mask[block].mean()
    corner_frac = mask[:60, :60].mean()
    assert block_frac > 0.95
    assert block_frac > corner_frac


def test_sharp_mask_survives_at_multiple_resolutions():
    # Regression guard for R3: the checkerboard block must stay overwhelmingly
    # selected whether the source is small or large — the proportional blur
    # (vs. a fixed sigma) is what keeps this true across resolutions.
    for h, w in [(400, 600), (1200, 1800), (2000, 3000)]:
        luma, block = _checkerboard_on_gradient(h, w, block=min(300, h // 3, w // 3))
        mask = sh.sharp_mask(luma)
        assert mask[block].mean() > 0.9, f"failed at {h}x{w}"


# --------------------------------------------------------------------------- #
# sharp_mask_gpu — CUDA parity with the CPU path
# --------------------------------------------------------------------------- #
@pytest.mark.gpu
def test_sharp_mask_gpu_matches_cpu_top_fraction(cuda_or_skip):
    torch = cuda_or_skip
    luma, block = _checkerboard_on_gradient(600, 900)

    cpu_mask = sh.sharp_mask(luma)
    gpu_mask = sh.sharp_mask_gpu(torch.from_numpy(luma).cuda()).cpu().numpy()

    assert gpu_mask.mean() == pytest.approx(cpu_mask.mean(), abs=0.02)
    assert gpu_mask[block].mean() > 0.9


@pytest.mark.gpu
def test_sharp_mask_gpu_uniform_image_keeps_everything(cuda_or_skip):
    torch = cuda_or_skip
    luma = torch.full((100, 150), 42.0, device="cuda")
    mask = sh.sharp_mask_gpu(luma)
    assert bool(mask.all())
