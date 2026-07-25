"""k-NN matching on seeds (`core.seed_match`) — normalized distance, selection of
the nearest, weighted 1/distance aggregation, calibration spread guard. Pure, no
DB or RAW (synthetic `SeedVector` objects are built via `conftest.make_seed`).

N1 (weighted k-NN), N2 (per-band k-NN) and N3 (confidence gate) live in
`test_seed_match_perband.py` — a separate file so this one doesn't grow back
into the 300-line block it used to be, and because those sections track
`match_target_per_band`, not yet wired into production (PLAN.md N2).
"""

from __future__ import annotations

import pytest

from app.core import seed_match as sm
from app.tests.conftest import make_band, make_seed


def test_distance_identical_is_zero():
    a = make_seed("a", 0.5, 0.6, 40.0)
    b = make_seed("b", 0.5, 0.6, 40.0)
    scale = {"asshot_rg": 1.0, "asshot_bg": 1.0, "raw_median_l": 1.0}
    assert sm._distance(a, b, scale) == pytest.approx(0.0)


def test_distance_ignores_missing_feature():
    a = make_seed("a", 0.5, None, 40.0)
    b = make_seed("b", 0.5, 0.6, 40.0)  # bg present on only one side → ignored
    scale = {"asshot_rg": 1.0, "asshot_bg": 1.0, "raw_median_l": 1.0}
    assert sm._distance(a, b, scale) == pytest.approx(0.0)


def test_distance_zero_when_composition_also_identical():
    a = make_seed("a", 0.5, 0.6, 40.0)
    a.raw_bands = [make_band("Red", frac=0.3), make_band("Blue", frac=0.1)]
    b = make_seed("b", 0.5, 0.6, 40.0)
    b.raw_bands = [make_band("Red", frac=0.3), make_band("Blue", frac=0.1)]
    scale = sm._feature_scale([a, b])
    assert sm._distance(a, b, scale) == pytest.approx(0.0)


def test_distance_grows_with_composition_difference():
    a = make_seed("a", 0.5, 0.6, 40.0)
    a.raw_bands = [make_band("Red", frac=0.3), make_band("Blue", frac=0.0)]
    b = make_seed("b", 0.5, 0.6, 40.0)  # same scalar features, different composition
    b.raw_bands = [make_band("Red", frac=0.0), make_band("Blue", frac=0.3)]
    scale = {"asshot_rg": 1.0, "asshot_bg": 1.0, "raw_median_l": 1.0,
             "band_frac_Red": 1.0, "band_frac_Blue": 1.0}
    assert sm._distance(a, b, scale) > 0.0


def test_distance_ignores_band_composition_when_one_side_missing():
    # `a` has no RAW band measurement at all → soft-skipped, same as a missing
    # scalar feature (not treated as "all bands at 0.0" on that side).
    a = make_seed("a", 0.5, 0.6, 40.0)
    b = make_seed("b", 0.5, 0.6, 40.0)
    b.raw_bands = [make_band("Red", frac=0.9)]
    scale = {"asshot_rg": 1.0, "asshot_bg": 1.0, "raw_median_l": 1.0, "band_frac_Red": 1.0}
    assert sm._distance(a, b, scale) == pytest.approx(0.0)


def test_feature_scale_includes_band_keys():
    a = make_seed("a", 0.5, 0.6, 40.0)
    a.raw_bands = [make_band("Red", frac=0.2)]
    b = make_seed("b", 0.5, 0.6, 40.0)
    b.raw_bands = [make_band("Red", frac=0.8)]
    scale = sm._feature_scale([a, b])
    assert scale["band_frac_Red"] == pytest.approx(0.3)  # std of [0.2, 0.8]
    assert scale["band_frac_Blue"] == pytest.approx(1.0)  # no data → neutral scale


def test_k_nearest_prefers_seed_with_similar_composition():
    # Two candidates tie exactly on the scalar features — only their RAW colour
    # composition differs. The k-NN must break the tie toward the one whose
    # composition matches the target (cf. PLAN.md D1 — goal 2).
    target = make_seed("t", 0.5, 0.5, 50.0)
    target.raw_bands = [make_band("Red", frac=0.5), make_band("Blue", frac=0.0)]
    same_scalar_close = make_seed("close", 0.5, 0.5, 50.0)
    same_scalar_close.raw_bands = [make_band("Red", frac=0.5), make_band("Blue", frac=0.0)]
    same_scalar_far = make_seed("far", 0.5, 0.5, 50.0)
    same_scalar_far.raw_bands = [make_band("Red", frac=0.0), make_band("Blue", frac=0.5)]
    matches = sm.k_nearest(target, [same_scalar_far, same_scalar_close], k=1)
    assert matches[0][0].photo_id == "close"


def test_k_nearest_excludes_self():
    target = make_seed("t", 0.5, 0.5, 50.0)
    pool = [target, make_seed("a", 0.6, 0.5, 50.0), make_seed("b", 0.9, 0.9, 90.0)]
    matches = sm.k_nearest(target, pool)
    assert all(m.photo_id != "t" for m, _ in matches)


def test_k_nearest_exact_match_returns_single():
    target = make_seed("t", 0.5, 0.5, 50.0)
    twin = make_seed("twin", 0.5, 0.5, 50.0)   # identical → distance ~0
    far = make_seed("far", 5.0, 5.0, 5.0)
    matches = sm.k_nearest(target, [twin, far])
    assert len(matches) == 1
    assert matches[0][0].photo_id == "twin"


def test_k_nearest_orders_by_distance():
    target = make_seed("t", 0.5, 0.5, 50.0)
    near = make_seed("near", 0.55, 0.5, 50.0)
    mid = make_seed("mid", 0.7, 0.5, 50.0)
    far = make_seed("far", 0.9, 0.5, 90.0)
    matches = sm.k_nearest(target, [far, mid, near], k=3)
    ids = [m.photo_id for m, _ in matches]
    assert ids[0] == "near"  # the nearest comes first


# --------------------------------------------------------------------------- #
# Degenerate cases: empty pool / empty weighted-mean input.
# --------------------------------------------------------------------------- #
def test_weighted_mean_and_empty():
    assert sm._weighted([]) is None
    assert sm._weighted([(10.0, 1.0), (20.0, 1.0)]) == pytest.approx(15.0)
    assert sm._weighted([(10.0, 3.0), (20.0, 1.0)]) == pytest.approx(12.5)


def test_target_from_seeds_none_on_empty():
    assert sm.target_from_seeds([]) is None


def _circ_close(deg: float, target: float, tol: float = 1e-4) -> bool:
    d = abs((deg - target + 180.0) % 360.0 - 180.0)
    return d < tol


def test_circular_mean_deg():
    # Result in [0,360): 10 and 350 → circular mean ≡ 0 (may output 360.0).
    assert _circ_close(sm._circular_mean_deg([10.0, 350.0]), 0.0)
    assert sm._circular_mean_deg([0.0, 90.0]) == pytest.approx(45.0, abs=1e-6)
    assert sm._circular_mean_deg([]) == pytest.approx(0.0)


def test_target_from_seeds_weights_nearer_seed():
    near = (make_seed("near", 0.5, 0.5, 50.0, temp=6000.0), 0.001)  # weight ~1000
    far = (make_seed("far", 0.9, 0.9, 90.0, temp=4000.0), 1.0)      # weight ~1
    tgt = sm.target_from_seeds([near, far])
    assert tgt is not None
    assert tgt.n_matched == 2
    assert tgt.temperature > 5900.0  # dominated by the near seed (6000)


def test_filter_by_profile_soft():
    target = make_seed("t", 0.5, 0.5, 50.0, profile="VV2")
    same = make_seed("a", 0.5, 0.5, 50.0, profile="VV2")
    other = make_seed("b", 0.5, 0.5, 50.0, profile="STD")
    # Same profile available → restricted pool.
    assert sm._filter_by_profile(target, [same, other]) == [same]
    # No same-profile match → fall back to the full pool (never empty).
    only_other = [other]
    assert sm._filter_by_profile(target, only_other) == only_other
    # Target without a profile → full pool.
    no_prof = make_seed("t2", 0.5, 0.5, 50.0, profile=None)
    assert sm._filter_by_profile(no_prof, [same, other]) == [same, other]


def test_target_from_seeds_no_calibration_when_seeds_lack_it():
    a = (make_seed("a", 0.5, 0.5, 50.0), 1.0)
    b = (make_seed("b", 0.5, 0.5, 50.0), 1.0)
    tgt = sm.target_from_seeds([a, b])
    assert tgt is not None
    assert tgt.has_calibration() is False


def test_target_from_seeds_aggregates_calibration_weighted():
    near = (make_seed("near", 0.5, 0.5, 50.0, shadow_tint=-10.0, red_hue=20.0), 0.001)  # weight ~1000
    far = (make_seed("far", 0.9, 0.9, 90.0, shadow_tint=10.0, red_hue=-20.0), 1.0)      # weight ~1
    tgt = sm.target_from_seeds([near, far])
    assert tgt is not None
    assert tgt.has_calibration() is True
    assert tgt.shadow_tint < -9.0  # dominated by the near seed
    assert tgt.red_hue > 19.0
    # Fields not seeded by anyone stay None (no 0 imposed).
    assert tgt.blue_hue is None


def test_target_from_seeds_calibration_partial_across_seeds():
    # Only one of the two seeds carries GreenSaturation → only it contributes.
    a = (make_seed("a", 0.5, 0.5, 50.0, green_saturation=30.0), 1.0)
    b = (make_seed("b", 0.5, 0.5, 50.0), 1.0)
    tgt = sm.target_from_seeds([a, b])
    assert tgt is not None
    assert tgt.green_saturation == pytest.approx(30.0)


def test_target_from_seeds_calibration_spread_guard_falls_back_to_nearest():
    # RedHue diverges strongly between the 2 matched seeds (+30 vs -20, spread 50 >
    # _CALIB_SPREAD_MAX=25): weighted average forbidden (wouldn't correspond to
    # any real seed) → fall back to the exact value of the nearest seed.
    near = (make_seed("near", 0.5, 0.5, 50.0, red_hue=30.0), 0.1)
    far = (make_seed("far", 0.9, 0.9, 90.0, red_hue=-20.0), 1.0)
    tgt = sm.target_from_seeds([near, far])
    assert tgt is not None
    assert tgt.red_hue == pytest.approx(30.0)  # near seed's value, not an average (5.4)


def test_target_from_seeds_calibration_spread_guard_allows_close_values():
    # Small divergence (spread 2 < _CALIB_SPREAD_MAX): consistent seeds → normal
    # weighted average unchanged, no 1-seed fallback.
    near = (make_seed("near", 0.5, 0.5, 50.0, red_hue=10.0), 0.001)
    far = (make_seed("far", 0.9, 0.9, 90.0, red_hue=12.0), 1.0)
    tgt = sm.target_from_seeds([near, far])
    assert tgt is not None
    assert 10.0 < tgt.red_hue < 12.0


def test_match_target_with_distance_returns_nearest_raw_distance():
    target = make_seed("t", 0.5, 0.5, 50.0)
    near = make_seed("near", 0.55, 0.5, 50.0, temp=6000.0)
    far = make_seed("far", 5.0, 5.0, 5.0, temp=4000.0)
    tgt, dist = sm.match_target_with_distance(target, [near, far], k=1)
    assert tgt is not None
    expected_dist = sm.k_nearest(target, [near, far], k=1)[0][1]
    assert dist == pytest.approx(expected_dist)
    assert dist > 0.0  # near isn't an exact match
    # Consistent with match_target (same aggregation, distance just exposed alongside).
    assert sm.match_target(target, [near, far], k=1).temperature == tgt.temperature


def test_match_target_with_distance_none_on_empty_pool():
    target = make_seed("t", 0.5, 0.5, 50.0)
    tgt, dist = sm.match_target_with_distance(target, [target])  # only self in pool
    assert tgt is None
    assert dist is None


def test_weighted_bands_averages_reliable_only():
    # frac 0.5 reliable, frac 0.0 ignored (band_is_reliable min 0.01).
    s1 = make_seed("s1", 0.5, 0.5, 50.0)
    s1.preview_bands = [make_band("Red", frac=0.5, median_hue=10.0, median_chroma=20.0)]
    s2 = make_seed("s2", 0.5, 0.5, 50.0)
    s2.preview_bands = [make_band("Red", frac=0.0, median_hue=999.0, median_chroma=999.0)]  # unreliable → excluded
    tgt = sm.target_from_seeds([(s1, 0.001), (s2, 0.002)])
    assert tgt is not None and tgt.bands is not None
    red = next(b for b in tgt.bands if b.name == "Red")
    assert red.median_chroma == pytest.approx(20.0)  # only s1 counts
