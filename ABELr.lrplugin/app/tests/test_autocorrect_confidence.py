"""Confidence gate wired into `core.autocorrect.plan()`'s seeds mode —
`PlanDiagnostics.n_low_confidence` + a matching note, surfaced by the GUI as a
distinct line (`main_window.py`'s `plan_summary_label`). Origin: PLAN.md N3/N4."""

from __future__ import annotations

from app.core import autocorrect as ac
from app.tests.conftest import make_analysis, make_measure, make_seed, make_tone


def _target(raw_median_l: float) -> ac.PhotoMeasure:
    return make_measure(
        "p1", analysis=make_analysis(), raw_tone=make_tone(raw_median_l),
        asshot_rg=0.5, asshot_bg=0.5,
    )


# Tight cluster (50/51/52) + one distant outlier (90) — the pool's own
# nearest-neighbor spacing is small, so the 75th-percentile threshold stays
# small too.
_POOL = [
    make_seed("s50", l=50.0), make_seed("s51", l=51.0),
    make_seed("s52", l=52.0), make_seed("s90", l=90.0),
]


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
    tiny_pool = [make_seed("s50", l=50.0)]
    _adjustments, diag = ac.plan(
        [_target(500.0)], axes=frozenset({"expo"}), model=None, seed_pool=tiny_pool,
    )
    assert diag.n_low_confidence == 0
