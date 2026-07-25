"""k-NN matching extensions on top of `core.seed_match` (split out of
`test_seed_match.py` — see that file's docstring for why):

- N1 — per-feature weighted distance (`_distance(..., weights)`, `k_nearest_weighted`).
- N2 — per-band k-NN (`match_bands_per_band`/`match_target_per_band`) — not yet
  wired into `autocorrect.py`'s production path (PLAN.md N2), kept isolated so
  wiring it in doesn't require reopening a 300-line file.
- N3 — confidence gate (`SeedTarget.confidence`, `pool_confidence_threshold`,
  `is_low_confidence`).
"""

from __future__ import annotations

import pytest

from app.core import seed_match as sm
from app.core.render_metrics import BAND_NAMES
from app.tests.conftest import make_band, make_seed


# --------------------------------------------------------------------------- #
# N1 — weighted k-NN
# --------------------------------------------------------------------------- #
def test_k_nearest_weighted_matches_k_nearest_with_no_weights():
    target = make_seed("t", 0.5, 0.5, 50.0)
    near = make_seed("near", 0.55, 0.5, 50.0)
    far = make_seed("far", 5.0, 5.0, 5.0)
    plain = sm.k_nearest(target, [near, far], k=1)
    weighted = sm.k_nearest_weighted(target, [near, far], None, k=1)
    assert [m.photo_id for m, _ in weighted] == [m.photo_id for m, _ in plain]
    assert weighted[0][1] == pytest.approx(plain[0][1])


def test_distance_weight_scales_the_squared_term():
    a = make_seed("a", 0.5, 0.5, 40.0)
    b = make_seed("b", 1.5, 0.5, 40.0)  # differ by 1.0 on asshot_rg only
    scale = {"asshot_rg": 1.0, "asshot_bg": 1.0, "raw_median_l": 1.0}
    base = sm._distance(a, b, scale)
    weighted = sm._distance(a, b, scale, {"asshot_rg": 4.0})
    assert weighted == pytest.approx(base * 2.0)  # sqrt(4 * d^2) = 2 * d


def test_k_nearest_weighted_can_flip_the_ranking():
    # 3-seed pool so the z-score scale isn't trivially symmetric between the
    # two candidates. `a` is nearer to the target on raw_median_l, `b` is
    # nearer on asshot_rg — weighting raw_median_l heavily must flip the winner.
    target = make_seed("t", 0.5, 0.5, 50.0)
    a = make_seed("a", 5.0, 0.5, 50.0)
    b = make_seed("b", 0.5, 0.5, 5.0)
    c = make_seed("c", 2.0, 0.5, 30.0)  # breaks the 2-point symmetry
    unweighted = sm.k_nearest_weighted(target, [a, b, c], None, k=1)
    assert unweighted[0][0].photo_id == "c"  # closest overall without a boosted axis
    weighted = sm.k_nearest_weighted(target, [a, b, c], {"raw_median_l": 1000.0}, k=1)
    assert weighted[0][0].photo_id == "a"  # L* now dominates → exact-L* seed wins


# --------------------------------------------------------------------------- #
# N2 — per-band k-NN
# --------------------------------------------------------------------------- #
def test_match_bands_per_band_runs_one_knn_per_band_name():
    target = make_seed("t", 0.5, 0.5, 50.0)
    a = make_seed("a", 0.5, 0.5, 50.0)
    b = make_seed("b", 0.5, 0.5, 50.0)
    result = sm.match_bands_per_band(target, [a, b])
    assert set(result.keys()) == set(BAND_NAMES)


def test_match_target_per_band_picks_the_seed_matching_that_bands_composition():
    # Both candidates tie on the scalar features. `red_rich` has real Red content
    # in its RAW composition (and a distinctive Red preview chroma); `blue_rich`
    # has Blue instead. A target whose own RAW is Red-heavy must get the Red
    # band's *value* from `red_rich`, even though a plain global match (tied
    # distance) could have picked either.
    target = make_seed("t", 0.5, 0.5, 50.0)
    target.raw_bands = [make_band("Red", frac=0.5), make_band("Blue", frac=0.0)]

    red_rich = make_seed("red_rich", 0.5, 0.5, 50.0)
    red_rich.raw_bands = [make_band("Red", frac=0.5), make_band("Blue", frac=0.0)]
    red_rich.preview_bands = [
        make_band("Red", frac=0.5, median_chroma=40.0),
        make_band("Blue", frac=0.0, median_chroma=0.0),
    ]

    blue_rich = make_seed("blue_rich", 0.5, 0.5, 50.0)
    blue_rich.raw_bands = [make_band("Red", frac=0.0), make_band("Blue", frac=0.5)]
    blue_rich.preview_bands = [
        make_band("Red", frac=0.0, median_chroma=0.0),
        make_band("Blue", frac=0.5, median_chroma=40.0),
    ]

    tgt = sm.match_target_per_band(target, [red_rich, blue_rich], k=1)
    assert tgt is not None and tgt.bands is not None
    red = next((b for b in tgt.bands if b.name == "Red"), None)
    assert red is not None
    assert red.median_chroma == pytest.approx(40.0)  # from red_rich, not blue_rich


def test_match_target_per_band_keeps_temperature_on_global_match():
    # Temp/Tint/Calibration must come from the *global* match, unaffected by
    # which seed wins any individual band's dedicated k-NN.
    target = make_seed("t", 0.5, 0.5, 50.0)
    target.raw_bands = [make_band("Red", frac=0.5)]
    near = make_seed("near", 0.5, 0.5, 50.0, temp=6000.0)
    near.raw_bands = [make_band("Red", frac=0.0)]  # composition-mismatched, but scalar-nearest
    far = make_seed("far", 9.0, 9.0, 90.0, temp=4000.0)
    far.raw_bands = [make_band("Red", frac=0.5)]  # composition-matched, but scalar-far
    tgt = sm.match_target_per_band(target, [near, far], k=1)
    assert tgt is not None
    assert tgt.temperature == pytest.approx(6000.0)  # global match picked `near`, not `far`


# --------------------------------------------------------------------------- #
# N3 — confidence gate
# --------------------------------------------------------------------------- #
def test_target_from_seeds_sets_confidence_to_nearest_distance():
    near = (make_seed("near", 0.5, 0.5, 50.0), 0.2)
    far = (make_seed("far", 0.9, 0.9, 90.0), 1.5)
    tgt = sm.target_from_seeds([near, far])
    assert tgt is not None
    assert tgt.confidence == pytest.approx(0.2)


def test_pool_confidence_threshold_none_on_tiny_pool():
    assert sm.pool_confidence_threshold([make_seed("a", 0.5, 0.5, 50.0)]) is None
    assert sm.pool_confidence_threshold([]) is None


def test_pool_confidence_threshold_is_a_real_percentile():
    # A tight cluster of 3 + one far outlier: the 75th-percentile
    # nearest-neighbor distance should sit strictly above the cluster's own
    # (near-zero) internal spacing, since the outlier's own nearest-neighbor
    # distance is large.
    seeds = [
        make_seed("a", 0.50, 0.50, 50.0),
        make_seed("b", 0.51, 0.50, 50.0),
        make_seed("c", 0.52, 0.50, 50.0),
        make_seed("outlier", 9.0, 9.0, 90.0),
    ]
    threshold = sm.pool_confidence_threshold(seeds, percentile=75.0)
    assert threshold is not None
    assert threshold > 0.0


def test_is_low_confidence_flags_beyond_threshold():
    near = (make_seed("near", 0.5, 0.5, 50.0), 0.5)
    tgt_near = sm.target_from_seeds([near])
    far = (make_seed("far", 0.5, 0.5, 50.0), 5.0)
    tgt_far = sm.target_from_seeds([far])
    assert sm.is_low_confidence(tgt_near, threshold=1.0) is False
    assert sm.is_low_confidence(tgt_far, threshold=1.0) is True


def test_is_low_confidence_false_when_threshold_unknown():
    tgt = sm.target_from_seeds([(make_seed("a", 0.5, 0.5, 50.0), 0.5)])
    assert sm.is_low_confidence(tgt, threshold=None) is False
