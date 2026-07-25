"""k-NN matching on seeds (`core.seed_match`) — normalized distance, selection of
the nearest, weighted 1/distance aggregation. Pure, no DB or RAW (synthetic
`SeedVector` objects are built).
"""

from __future__ import annotations

import pytest

from app.core import seed_match as sm
from app.core.render_metrics import BAND_NAMES, BandStats, ToneStats


def _tone(median_l: float) -> ToneStats:
    return ToneStats(median_l, median_l, median_l - 5, median_l + 5, 0.0, 0.0, 1.0)


def _seed(pid, rg, bg, l, temp=5500.0, tint=0.0, tone_l=50.0, profile=None, **calib):
    return sm.SeedVector(
        photo_id=pid, asshot_rg=rg, asshot_bg=bg, raw_median_l=l,
        temperature=temp, tint=tint, preview_tone=_tone(tone_l),
        preview_bands=None, profile_capture=profile, **calib,
    )


def test_distance_identical_is_zero():
    a = _seed("a", 0.5, 0.6, 40.0)
    b = _seed("b", 0.5, 0.6, 40.0)
    scale = {"asshot_rg": 1.0, "asshot_bg": 1.0, "raw_median_l": 1.0}
    assert sm._distance(a, b, scale) == pytest.approx(0.0)


def test_distance_ignores_missing_feature():
    a = _seed("a", 0.5, None, 40.0)
    b = _seed("b", 0.5, 0.6, 40.0)  # bg present on only one side → ignored
    scale = {"asshot_rg": 1.0, "asshot_bg": 1.0, "raw_median_l": 1.0}
    assert sm._distance(a, b, scale) == pytest.approx(0.0)


def _band(name, frac):
    return BandStats(name, frac, 0.0, 0.0, 0.0, 0.0, 50.0)


def test_distance_zero_when_composition_also_identical():
    a = _seed("a", 0.5, 0.6, 40.0)
    a.raw_bands = [_band("Red", 0.3), _band("Blue", 0.1)]
    b = _seed("b", 0.5, 0.6, 40.0)
    b.raw_bands = [_band("Red", 0.3), _band("Blue", 0.1)]
    scale = sm._feature_scale([a, b])
    assert sm._distance(a, b, scale) == pytest.approx(0.0)


def test_distance_grows_with_composition_difference():
    a = _seed("a", 0.5, 0.6, 40.0)
    a.raw_bands = [_band("Red", 0.3), _band("Blue", 0.0)]
    b = _seed("b", 0.5, 0.6, 40.0)  # same scalar features, different composition
    b.raw_bands = [_band("Red", 0.0), _band("Blue", 0.3)]
    scale = {"asshot_rg": 1.0, "asshot_bg": 1.0, "raw_median_l": 1.0,
             "band_frac_Red": 1.0, "band_frac_Blue": 1.0}
    assert sm._distance(a, b, scale) > 0.0


def test_distance_ignores_band_composition_when_one_side_missing():
    # `a` has no RAW band measurement at all → soft-skipped, same as a missing
    # scalar feature (not treated as "all bands at 0.0" on that side).
    a = _seed("a", 0.5, 0.6, 40.0)
    b = _seed("b", 0.5, 0.6, 40.0)
    b.raw_bands = [_band("Red", 0.9)]
    scale = {"asshot_rg": 1.0, "asshot_bg": 1.0, "raw_median_l": 1.0, "band_frac_Red": 1.0}
    assert sm._distance(a, b, scale) == pytest.approx(0.0)


def test_feature_scale_includes_band_keys():
    a = _seed("a", 0.5, 0.6, 40.0)
    a.raw_bands = [_band("Red", 0.2)]
    b = _seed("b", 0.5, 0.6, 40.0)
    b.raw_bands = [_band("Red", 0.8)]
    scale = sm._feature_scale([a, b])
    assert scale["band_frac_Red"] == pytest.approx(0.3)  # std of [0.2, 0.8]
    assert scale["band_frac_Blue"] == pytest.approx(1.0)  # no data → neutral scale


def test_k_nearest_prefers_seed_with_similar_composition():
    # Two candidates tie exactly on the scalar features — only their RAW colour
    # composition differs. The k-NN must break the tie toward the one whose
    # composition matches the target (cf. PLAN.md D1 — goal 2).
    target = _seed("t", 0.5, 0.5, 50.0)
    target.raw_bands = [_band("Red", 0.5), _band("Blue", 0.0)]
    same_scalar_close = _seed("close", 0.5, 0.5, 50.0)
    same_scalar_close.raw_bands = [_band("Red", 0.5), _band("Blue", 0.0)]
    same_scalar_far = _seed("far", 0.5, 0.5, 50.0)
    same_scalar_far.raw_bands = [_band("Red", 0.0), _band("Blue", 0.5)]
    matches = sm.k_nearest(target, [same_scalar_far, same_scalar_close], k=1)
    assert matches[0][0].photo_id == "close"


def test_k_nearest_excludes_self():
    target = _seed("t", 0.5, 0.5, 50.0)
    pool = [target, _seed("a", 0.6, 0.5, 50.0), _seed("b", 0.9, 0.9, 90.0)]
    matches = sm.k_nearest(target, pool)
    assert all(m.photo_id != "t" for m, _ in matches)


def test_k_nearest_exact_match_returns_single():
    target = _seed("t", 0.5, 0.5, 50.0)
    twin = _seed("twin", 0.5, 0.5, 50.0)   # identical → distance ~0
    far = _seed("far", 5.0, 5.0, 5.0)
    matches = sm.k_nearest(target, [twin, far])
    assert len(matches) == 1
    assert matches[0][0].photo_id == "twin"


def test_k_nearest_orders_by_distance():
    target = _seed("t", 0.5, 0.5, 50.0)
    near = _seed("near", 0.55, 0.5, 50.0)
    mid = _seed("mid", 0.7, 0.5, 50.0)
    far = _seed("far", 0.9, 0.5, 90.0)
    matches = sm.k_nearest(target, [far, mid, near], k=3)
    ids = [m.photo_id for m, _ in matches]
    assert ids[0] == "near"  # the nearest comes first


def test_weighted_mean_and_empty():
    assert sm._weighted([]) is None
    assert sm._weighted([(10.0, 1.0), (20.0, 1.0)]) == pytest.approx(15.0)
    assert sm._weighted([(10.0, 3.0), (20.0, 1.0)]) == pytest.approx(12.5)


def _circ_close(deg: float, target: float, tol: float = 1e-4) -> bool:
    d = abs((deg - target + 180.0) % 360.0 - 180.0)
    return d < tol


def test_circular_mean_deg():
    # Result in [0,360): 10 and 350 → circular mean ≡ 0 (may output 360.0).
    assert _circ_close(sm._circular_mean_deg([10.0, 350.0]), 0.0)
    assert sm._circular_mean_deg([0.0, 90.0]) == pytest.approx(45.0, abs=1e-6)
    assert sm._circular_mean_deg([]) == pytest.approx(0.0)


def test_target_from_seeds_none_on_empty():
    assert sm.target_from_seeds([]) is None


def test_target_from_seeds_weights_nearer_seed():
    near = (_seed("near", 0.5, 0.5, 50.0, temp=6000.0), 0.001)  # weight ~1000
    far = (_seed("far", 0.9, 0.9, 90.0, temp=4000.0), 1.0)      # weight ~1
    tgt = sm.target_from_seeds([near, far])
    assert tgt is not None
    assert tgt.n_matched == 2
    assert tgt.temperature > 5900.0  # dominated by the near seed (6000)


def test_filter_by_profile_soft():
    target = _seed("t", 0.5, 0.5, 50.0, profile="VV2")
    same = _seed("a", 0.5, 0.5, 50.0, profile="VV2")
    other = _seed("b", 0.5, 0.5, 50.0, profile="STD")
    # Same profile available → restricted pool.
    assert sm._filter_by_profile(target, [same, other]) == [same]
    # No same-profile match → fall back to the full pool (never empty).
    only_other = [other]
    assert sm._filter_by_profile(target, only_other) == only_other
    # Target without a profile → full pool.
    no_prof = _seed("t2", 0.5, 0.5, 50.0, profile=None)
    assert sm._filter_by_profile(no_prof, [same, other]) == [same, other]


def test_target_from_seeds_no_calibration_when_seeds_lack_it():
    a = (_seed("a", 0.5, 0.5, 50.0), 1.0)
    b = (_seed("b", 0.5, 0.5, 50.0), 1.0)
    tgt = sm.target_from_seeds([a, b])
    assert tgt is not None
    assert tgt.has_calibration() is False


def test_target_from_seeds_aggregates_calibration_weighted():
    near = (_seed("near", 0.5, 0.5, 50.0, shadow_tint=-10.0, red_hue=20.0), 0.001)  # weight ~1000
    far = (_seed("far", 0.9, 0.9, 90.0, shadow_tint=10.0, red_hue=-20.0), 1.0)      # weight ~1
    tgt = sm.target_from_seeds([near, far])
    assert tgt is not None
    assert tgt.has_calibration() is True
    assert tgt.shadow_tint < -9.0  # dominated by the near seed
    assert tgt.red_hue > 19.0
    # Fields not seeded by anyone stay None (no 0 imposed).
    assert tgt.blue_hue is None


def test_target_from_seeds_calibration_partial_across_seeds():
    # Only one of the two seeds carries GreenSaturation → only it contributes.
    a = (_seed("a", 0.5, 0.5, 50.0, green_saturation=30.0), 1.0)
    b = (_seed("b", 0.5, 0.5, 50.0), 1.0)
    tgt = sm.target_from_seeds([a, b])
    assert tgt is not None
    assert tgt.green_saturation == pytest.approx(30.0)


def test_target_from_seeds_calibration_spread_guard_falls_back_to_nearest():
    # RedHue diverges strongly between the 2 matched seeds (+30 vs -20, spread 50 >
    # _CALIB_SPREAD_MAX=25): weighted average forbidden (wouldn't correspond to
    # any real seed) → fall back to the exact value of the nearest seed.
    near = (_seed("near", 0.5, 0.5, 50.0, red_hue=30.0), 0.1)
    far = (_seed("far", 0.9, 0.9, 90.0, red_hue=-20.0), 1.0)
    tgt = sm.target_from_seeds([near, far])
    assert tgt is not None
    assert tgt.red_hue == pytest.approx(30.0)  # near seed's value, not an average (5.4)


def test_target_from_seeds_calibration_spread_guard_allows_close_values():
    # Small divergence (spread 2 < _CALIB_SPREAD_MAX): consistent seeds → normal
    # weighted average unchanged, no 1-seed fallback.
    near = (_seed("near", 0.5, 0.5, 50.0, red_hue=10.0), 0.001)
    far = (_seed("far", 0.9, 0.9, 90.0, red_hue=12.0), 1.0)
    tgt = sm.target_from_seeds([near, far])
    assert tgt is not None
    assert 10.0 < tgt.red_hue < 12.0


def test_match_target_with_distance_returns_nearest_raw_distance():
    target = _seed("t", 0.5, 0.5, 50.0)
    near = _seed("near", 0.55, 0.5, 50.0, temp=6000.0)
    far = _seed("far", 5.0, 5.0, 5.0, temp=4000.0)
    tgt, dist = sm.match_target_with_distance(target, [near, far], k=1)
    assert tgt is not None
    expected_dist = sm.k_nearest(target, [near, far], k=1)[0][1]
    assert dist == pytest.approx(expected_dist)
    assert dist > 0.0  # near isn't an exact match
    # Consistent with match_target (same aggregation, distance just exposed alongside).
    assert sm.match_target(target, [near, far], k=1).temperature == tgt.temperature


def test_match_target_with_distance_none_on_empty_pool():
    target = _seed("t", 0.5, 0.5, 50.0)
    tgt, dist = sm.match_target_with_distance(target, [target])  # only self in pool
    assert tgt is None
    assert dist is None


def test_weighted_bands_averages_reliable_only():
    def band(name, frac, hue, chroma):
        return BandStats(name, frac, hue, chroma, 0.3, 0.0, 50.0)

    # frac 0.5 reliable, frac 0.0 ignored (band_is_reliable min 0.01).
    s1 = _seed("s1", 0.5, 0.5, 50.0)
    s1.preview_bands = [band("Red", 0.5, 10.0, 20.0)]
    s2 = _seed("s2", 0.5, 0.5, 50.0)
    s2.preview_bands = [band("Red", 0.0, 999.0, 999.0)]  # unreliable → excluded
    tgt = sm.target_from_seeds([(s1, 0.001), (s2, 0.002)])
    assert tgt is not None and tgt.bands is not None
    red = next(b for b in tgt.bands if b.name == "Red")
    assert red.median_chroma == pytest.approx(20.0)  # only s1 counts


# --------------------------------------------------------------------------- #
# N1 — weighted k-NN
# --------------------------------------------------------------------------- #
def test_k_nearest_weighted_matches_k_nearest_with_no_weights():
    target = _seed("t", 0.5, 0.5, 50.0)
    near = _seed("near", 0.55, 0.5, 50.0)
    far = _seed("far", 5.0, 5.0, 5.0)
    plain = sm.k_nearest(target, [near, far], k=1)
    weighted = sm.k_nearest_weighted(target, [near, far], None, k=1)
    assert [m.photo_id for m, _ in weighted] == [m.photo_id for m, _ in plain]
    assert weighted[0][1] == pytest.approx(plain[0][1])


def test_distance_weight_scales_the_squared_term():
    a = _seed("a", 0.5, 0.5, 40.0)
    b = _seed("b", 1.5, 0.5, 40.0)  # differ by 1.0 on asshot_rg only
    scale = {"asshot_rg": 1.0, "asshot_bg": 1.0, "raw_median_l": 1.0}
    base = sm._distance(a, b, scale)
    weighted = sm._distance(a, b, scale, {"asshot_rg": 4.0})
    assert weighted == pytest.approx(base * 2.0)  # sqrt(4 * d^2) = 2 * d


def test_k_nearest_weighted_can_flip_the_ranking():
    # 3-seed pool so the z-score scale isn't trivially symmetric between the
    # two candidates. `a` is nearer to the target on raw_median_l, `b` is
    # nearer on asshot_rg — weighting raw_median_l heavily must flip the winner.
    target = _seed("t", 0.5, 0.5, 50.0)
    a = _seed("a", 5.0, 0.5, 50.0)
    b = _seed("b", 0.5, 0.5, 5.0)
    c = _seed("c", 2.0, 0.5, 30.0)  # breaks the 2-point symmetry
    unweighted = sm.k_nearest_weighted(target, [a, b, c], None, k=1)
    assert unweighted[0][0].photo_id == "c"  # closest overall without a boosted axis
    weighted = sm.k_nearest_weighted(target, [a, b, c], {"raw_median_l": 1000.0}, k=1)
    assert weighted[0][0].photo_id == "a"  # L* now dominates → exact-L* seed wins


# --------------------------------------------------------------------------- #
# N2 — per-band k-NN
# --------------------------------------------------------------------------- #
def _band(name, frac, chroma=20.0):
    return BandStats(name, frac, 0.0, chroma, 0.3, 0.0, 50.0)


def test_match_bands_per_band_runs_one_knn_per_band_name():
    target = _seed("t", 0.5, 0.5, 50.0)
    a = _seed("a", 0.5, 0.5, 50.0)
    b = _seed("b", 0.5, 0.5, 50.0)
    result = sm.match_bands_per_band(target, [a, b])
    assert set(result.keys()) == set(BAND_NAMES)


def test_match_target_per_band_picks_the_seed_matching_that_bands_composition():
    # Both candidates tie on the scalar features. `red_rich` has real Red content
    # in its RAW composition (and a distinctive Red preview chroma); `blue_rich`
    # has Blue instead. A target whose own RAW is Red-heavy must get the Red
    # band's *value* from `red_rich`, even though a plain global match (tied
    # distance) could have picked either.
    target = _seed("t", 0.5, 0.5, 50.0)
    target.raw_bands = [_band("Red", 0.5), _band("Blue", 0.0)]

    red_rich = _seed("red_rich", 0.5, 0.5, 50.0)
    red_rich.raw_bands = [_band("Red", 0.5), _band("Blue", 0.0)]
    red_rich.preview_bands = [_band("Red", 0.5, chroma=40.0), _band("Blue", 0.0, chroma=0.0)]

    blue_rich = _seed("blue_rich", 0.5, 0.5, 50.0)
    blue_rich.raw_bands = [_band("Red", 0.0), _band("Blue", 0.5)]
    blue_rich.preview_bands = [_band("Red", 0.0, chroma=0.0), _band("Blue", 0.5, chroma=40.0)]

    tgt = sm.match_target_per_band(target, [red_rich, blue_rich], k=1)
    assert tgt is not None and tgt.bands is not None
    red = next((b for b in tgt.bands if b.name == "Red"), None)
    assert red is not None
    assert red.median_chroma == pytest.approx(40.0)  # from red_rich, not blue_rich


def test_match_target_per_band_keeps_temperature_on_global_match():
    # Temp/Tint/Calibration must come from the *global* match, unaffected by
    # which seed wins any individual band's dedicated k-NN.
    target = _seed("t", 0.5, 0.5, 50.0)
    target.raw_bands = [_band("Red", 0.5)]
    near = _seed("near", 0.5, 0.5, 50.0, temp=6000.0)
    near.raw_bands = [_band("Red", 0.0)]  # composition-mismatched, but scalar-nearest
    far = _seed("far", 9.0, 9.0, 90.0, temp=4000.0)
    far.raw_bands = [_band("Red", 0.5)]  # composition-matched, but scalar-far
    tgt = sm.match_target_per_band(target, [near, far], k=1)
    assert tgt is not None
    assert tgt.temperature == pytest.approx(6000.0)  # global match picked `near`, not `far`


# --------------------------------------------------------------------------- #
# N3 — confidence gate
# --------------------------------------------------------------------------- #
def test_target_from_seeds_sets_confidence_to_nearest_distance():
    near = (_seed("near", 0.5, 0.5, 50.0), 0.2)
    far = (_seed("far", 0.9, 0.9, 90.0), 1.5)
    tgt = sm.target_from_seeds([near, far])
    assert tgt is not None
    assert tgt.confidence == pytest.approx(0.2)


def test_pool_confidence_threshold_none_on_tiny_pool():
    assert sm.pool_confidence_threshold([_seed("a", 0.5, 0.5, 50.0)]) is None
    assert sm.pool_confidence_threshold([]) is None


def test_pool_confidence_threshold_is_a_real_percentile():
    # A tight cluster of 3 + one far outlier: the 75th-percentile
    # nearest-neighbor distance should sit strictly above the cluster's own
    # (near-zero) internal spacing, since the outlier's own nearest-neighbor
    # distance is large.
    seeds = [
        _seed("a", 0.50, 0.50, 50.0),
        _seed("b", 0.51, 0.50, 50.0),
        _seed("c", 0.52, 0.50, 50.0),
        _seed("outlier", 9.0, 9.0, 90.0),
    ]
    threshold = sm.pool_confidence_threshold(seeds, percentile=75.0)
    assert threshold is not None
    assert threshold > 0.0


def test_is_low_confidence_flags_beyond_threshold():
    near = (_seed("near", 0.5, 0.5, 50.0), 0.5)
    tgt_near = sm.target_from_seeds([near])
    far = (_seed("far", 0.5, 0.5, 50.0), 5.0)
    tgt_far = sm.target_from_seeds([far])
    assert sm.is_low_confidence(tgt_near, threshold=1.0) is False
    assert sm.is_low_confidence(tgt_far, threshold=1.0) is True


def test_is_low_confidence_false_when_threshold_unknown():
    tgt = sm.target_from_seeds([(_seed("a", 0.5, 0.5, 50.0), 0.5)])
    assert sm.is_low_confidence(tgt, threshold=None) is False
