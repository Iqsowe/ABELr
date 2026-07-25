"""HSL band planning (`core.hsl`) — anti-saturation over-correction guard.

`plan_band` must block a saturation reduction when the RAW explicitly denies
oversaturation (`raw_oversat=False`), and behave as before (no blocking) when the
RAW info is absent (`raw_oversat=None`, historical behavior) or confirmed
(`raw_oversat=True`). Origin: PLAN.md step H1.
"""

from __future__ import annotations

from app.core.hsl import BandTarget, plan_band, raw_confirms_oversat
from app.core.response import BandResponse
from app.tests.conftest import make_band as _band


def test_plan_band_reduces_saturation_when_excess_and_no_raw_info():
    # Historical behavior: no RAW info (raw_oversat=None) → no blocking.
    stats = _band(median_chroma=40.0)
    target = BandTarget(name="Red", chroma=20.0, raw_oversat=None)
    corr = plan_band(stats, target, BandResponse())
    assert corr is not None
    assert corr.d_saturation < 0


def test_plan_band_reduces_saturation_when_raw_confirms_oversat():
    stats = _band(median_chroma=40.0)
    target = BandTarget(name="Red", chroma=20.0, raw_oversat=True)
    corr = plan_band(stats, target, BandResponse())
    assert corr is not None
    assert corr.d_saturation < 0


def test_plan_band_blocks_reduction_when_raw_denies_oversat():
    # Same measured chroma excess as the cases above, but the RAW denies it →
    # the guard must block the saturation reduction (no over-correction).
    stats = _band(median_chroma=40.0)
    target = BandTarget(name="Red", chroma=20.0, raw_oversat=False)
    corr = plan_band(stats, target, BandResponse())
    assert corr is None or corr.d_saturation == 0


def test_plan_band_hard_clip_trigger_also_blocked_by_raw_denial():
    # "Hard" oversaturation detected on the render (high sat_clip_frac, no chroma
    # target): the raw_oversat=False guard must block even this trigger.
    stats = _band(median_chroma=10.0, sat_clip_frac=0.5)
    target = BandTarget(name="Red", chroma=None, raw_oversat=False)
    corr = plan_band(stats, target, BandResponse())
    assert corr is None or corr.d_saturation == 0


def test_raw_confirms_oversat_none_without_raw_band():
    assert raw_confirms_oversat(None) is None


def test_raw_confirms_oversat_none_when_band_underpopulated():
    # frac below the minimum threshold → not enough RAW pixels to decide.
    band = _band(frac=0.001, sat_clip_frac=0.9)
    assert raw_confirms_oversat(band) is None


def test_raw_confirms_oversat_true_on_hard_clip():
    band = _band(frac=0.5, sat_clip_frac=0.10)
    assert raw_confirms_oversat(band) is True


def test_raw_confirms_oversat_false_without_hard_clip():
    band = _band(frac=0.5, sat_clip_frac=0.0)
    assert raw_confirms_oversat(band) is False


# --------------------------------------------------------------------------- #
# Embedded transplant (`BandTarget.embedded_raw=True`) caps luminance/hue
# deltas more strictly (no "reduction only" guard possible on these axes like
# there is for saturation). Origin: PLAN.md step H3.
# --------------------------------------------------------------------------- #
def test_plan_band_embedded_raw_caps_luminance_delta_tighter():
    # In-camera JPEG target heavily shifted in L* (+80): without the dedicated cap,
    # the luminance delta would climb to _MAX_LUM (20). With embedded_raw=True, it
    # must stay at the strict cap (_MAX_LUM_EMBEDDED_RAW = 10).
    stats = _band(median_l=20.0, median_chroma=20.0)
    target_loose = BandTarget(name="Red", lstar=100.0, embedded_raw=False)
    target_strict = BandTarget(name="Red", lstar=100.0, embedded_raw=True)
    corr_loose = plan_band(stats, target_loose, BandResponse())
    corr_strict = plan_band(stats, target_strict, BandResponse())
    assert corr_loose.d_luminance == 20
    assert corr_strict.d_luminance == 10


def test_plan_band_embedded_raw_caps_hue_delta_tighter():
    stats = _band(median_hue=0.0, median_chroma=20.0)
    target_loose = BandTarget(name="Red", hue=170.0, embedded_raw=False)
    target_strict = BandTarget(name="Red", hue=170.0, embedded_raw=True)
    corr_loose = plan_band(stats, target_loose, BandResponse())
    corr_strict = plan_band(stats, target_strict, BandResponse())
    assert corr_loose.d_hue == 15
    assert corr_strict.d_hue == 8


def test_plan_band_embedded_raw_default_false_unchanged_behavior():
    # Historical behavior preserved: `embedded_raw` defaults to False, nominal cap
    # unchanged (no regression on non-embedded/seed-match targets).
    stats = _band(median_l=20.0, median_chroma=20.0)
    target = BandTarget(name="Red", lstar=100.0)
    corr = plan_band(stats, target, BandResponse())
    assert corr.d_luminance == 20
