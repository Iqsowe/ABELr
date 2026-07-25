"""Pytest config — test net for the **pure** core functions (no GPU, no RAW).

These tests lock down the numerical invariants that the whole pipeline's
correctness depends on (colorimetry, cache key stability, k-NN aggregation,
calibrated response). They run in a few seconds, with no CUDA or `.ARW` file needed:
- run from the project root: `pytest app/tests -q`
- tests marked `@pytest.mark.gpu` are **skipped** if torch/CUDA is absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The project root (parent of the `app` package) must be on sys.path for
# `from app.core import ...` regardless of the launch cwd.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# `gpu` marker registered via ../pytest.ini (testpaths/markers/filterwarnings).

from app.core.autocorrect import PhotoMeasure  # noqa: E402
from app.core.pipeline import RenderAnalysis  # noqa: E402
from app.core.render_metrics import BandStats, NeutralStats, ToneStats  # noqa: E402
from app.core.seed_match import SeedVector  # noqa: E402


@pytest.fixture(scope="session")
def cuda_or_skip():
    """Skip the test if torch/CUDA is not available (GPU/CPU parity)."""
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("CUDA unavailable")
    return torch


# --------------------------------------------------------------------------- #
# Shared synthetic-data factories — module-level functions (not pytest
# fixtures: callers invoke them mid-test with varying arguments, a fixture
# would only add an indirection). All fields but the first 1-2 are
# keyword-only, so accidental positional shadowing (the historical bug this
# consolidation fixes: two conflicting `_band()` defs coexisted in
# test_seed_match.py) can't recur.
# --------------------------------------------------------------------------- #
def make_tone(median_l: float = 50.0) -> ToneStats:
    return ToneStats(median_l, median_l, median_l - 5, median_l + 5, 0.0, 0.0, 1.0)


def make_neutral(
    *, a: float = 0.0, b: float = 0.0, chroma: float = 0.0, frac: float = 0.0, n: int = 0
) -> NeutralStats:
    return NeutralStats(a_bias=a, b_bias=b, chroma=chroma, neutral_frac=frac, n_neutral=n)


def make_band(
    name: str = "Red",
    *,
    frac: float = 0.5,
    median_hue: float = 0.0,
    median_chroma: float = 40.0,
    median_sat: float = 0.5,
    sat_clip_frac: float = 0.0,
    median_l: float = 50.0,
) -> BandStats:
    return BandStats(
        name=name, frac=frac, median_hue=median_hue, median_chroma=median_chroma,
        median_sat=median_sat, sat_clip_frac=sat_clip_frac, median_l=median_l,
    )


def make_analysis(
    *, tone: ToneStats | None = None, neutral: NeutralStats | None = None, bands=()
) -> RenderAnalysis:
    return RenderAnalysis(tone=tone or make_tone(), neutral=neutral or make_neutral(), bands=list(bands))


def make_seed(
    pid: str,
    rg: float | None = 0.5,
    bg: float | None = 0.5,
    l: float | None = 50.0,
    *,
    temp: float | None = 5500.0,
    tint: float | None = 0.0,
    tone_l: float = 50.0,
    profile: str | None = None,
    **calib,
) -> SeedVector:
    return SeedVector(
        photo_id=pid, asshot_rg=rg, asshot_bg=bg, raw_median_l=l,
        temperature=temp, tint=tint, preview_tone=make_tone(tone_l),
        preview_bands=None, profile_capture=profile, **calib,
    )


def make_measure(pid: str = "p1", **kw) -> PhotoMeasure:
    kw.setdefault("path", f"{pid}.ARW")
    kw.setdefault("current_develop", {})
    kw.setdefault("exif_camera", "ILCE-7M3")
    return PhotoMeasure(photo_id=pid, **kw)
