"""Confidence gate (PLAN.md N3) wired into `core.autocorrect.plan()`'s seeds
mode (N4) — `PlanDiagnostics.n_low_confidence` + a matching note, surfaced by
the GUI as a distinct line (`main_window.py`'s `plan_summary_label`)."""

from __future__ import annotations

from app.core import autocorrect as ac
from app.core.pipeline import RenderAnalysis
from app.core.render_metrics import NeutralStats, ToneStats
from app.core.seed_match import SeedVector


def _tone(median_l: float = 50.0) -> ToneStats:
    return ToneStats(median_l, median_l, median_l - 5, median_l + 5, 0.0, 0.0, 1.0)


def _neutral() -> NeutralStats:
    return NeutralStats(a_bias=0.0, b_bias=0.0, chroma=0.0, neutral_frac=0.0, n_neutral=0)


def _analysis() -> RenderAnalysis:
    return RenderAnalysis(tone=_tone(), neutral=_neutral(), bands=[])


def _seed(pid: str, raw_median_l: float) -> SeedVector:
    return SeedVector(
        photo_id=pid, asshot_rg=0.5, asshot_bg=0.5, raw_median_l=raw_median_l,
        temperature=5500.0, tint=0.0, preview_tone=_tone(), preview_bands=None,
    )


def _target(raw_median_l: float) -> ac.PhotoMeasure:
    return ac.PhotoMeasure(
        photo_id="p1", path="p1.ARW", current_develop={}, exif_camera="ILCE-7M3",
        analysis=_analysis(), raw_tone=_tone(raw_median_l), asshot_rg=0.5, asshot_bg=0.5,
    )


# Tight cluster (50/51/52) + one distant outlier (90) — the pool's own
# nearest-neighbor spacing is small, so the 75th-percentile threshold stays
# small too.
_POOL = [_seed("s50", 50.0), _seed("s51", 51.0), _seed("s52", 52.0), _seed("s90", 90.0)]


def test_plan_seeds_mode_flags_low_confidence_match():
    adjustments, diag = ac.plan(
        [_target(200.0)], axes=frozenset({"expo"}), model=None, seed_pool=_POOL,
    )
    assert diag.n_low_confidence == 1
    assert any("low-confidence match" in note for note in diag.notes)
    assert adjustments  # the axis still ran — this is a flag, not a hard cutoff


def test_plan_seeds_mode_does_not_flag_well_matched_target():
    adjustments, diag = ac.plan(
        [_target(51.5)], axes=frozenset({"expo"}), model=None, seed_pool=_POOL,
    )
    assert diag.n_low_confidence == 0
    assert not any("low-confidence match" in note for note in diag.notes)
    assert adjustments


def test_plan_seeds_mode_confidence_gate_no_op_on_tiny_pool():
    # < 2 seeds: pool_confidence_threshold returns None, no flag possible.
    tiny_pool = [_seed("s50", 50.0)]
    _adjustments, diag = ac.plan(
        [_target(500.0)], axes=frozenset({"expo"}), model=None, seed_pool=tiny_pool,
    )
    assert diag.n_low_confidence == 0
