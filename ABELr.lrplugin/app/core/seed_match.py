"""k-NN matching on seeds — replaces `regime.py` on the live app side (`wb_model.py`
stays live: `refine_temp_tint` refines Temp/Tint after the k-NN, cf. autocorrect).

Instead of a physical regression (camera slope r/g → Temperature) or a purely
render-space recalibration, for each target photo we look for the **seeds**
(explicitly marked by the user, `cache.is_seed`) whose RAW analysis (sharp zone,
`core.sharpness`) is closest, and use **their** rendered preview (`PreviewJPEG`,
already retouched by the user — the desired style reference) as the target for
the Exposure/WB/HSL axes.

`exposure.py`/`hsl.py`/`autocorrect.py` consume `target_from_seeds(...)` to get
a target (ToneStats + bands + Temperature/Tint) to compare against the
**current** state, freshly measured (hash-checked) by the caller — it's this
hash-check on the caller side that guarantees we never recompound a delta on a
stale measurement (cf. CLAUDE.md / refactor plan).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import cache as cachemod
from .render_metrics import BAND_NAMES, BandStats, ToneStats, band_is_reliable

K_MAX = 3

# Scalar features in the k-NN distance (cf. `_distance`/`_feature_scale`).
_SCALAR_FEATURES = ("asshot_rg", "asshot_bg", "raw_median_l")
# Per-band population fraction (RAW sharp zone, already cached in
# `SourceRAW.hsl_sharp` — cf. PLAN.md D1) added as 8 extra distance features,
# so two seeds with the same exposure/WB but a different colour composition
# (e.g. a red-lit vs. blue-lit frame) no longer look identical to the k-NN.
_BAND_FEATURE_PREFIX = "band_frac_"


# Camera Calibration (the "Camera Calibration" panel): 7 flat settings, transplanted
# as-is from the seeds (like Temperature/Tint) — no measurement/inversion possible,
# these are creative settings with no objective target on the render side.
# Note: "RedHue"/"GreenHue"/"BlueHue" are linear -100..100 sliders (not a hue
# angle) → classic weighted average, not circular averaging.
CALIB_FIELDS = (
    "shadow_tint",
    "red_hue", "red_saturation",
    "green_hue", "green_saturation",
    "blue_hue", "blue_saturation",
)


@dataclass
class SeedVector:
    photo_id: str
    asshot_rg: float | None
    asshot_bg: float | None
    raw_median_l: float | None              # ToneStats.median_l of the RAW (sharp zone)
    temperature: float | None               # Temperature retouched by the user
    tint: float | None
    preview_tone: ToneStats | None          # seed's PreviewJPEG (exposure target)
    preview_bands: list[BandStats] | None   # seed's PreviewJPEG (HSL target)
    raw_bands: list[BandStats] | None = None  # seed's RAW sharp-zone bands (composition, cf. D1)
    profile_capture: str | None = None      # camera creative profile (group filter)
    ev100: float | None = None              # scene context (not used in the distance)
    shadow_tint: float | None = None        # Calibration — cf. CALIB_FIELDS
    red_hue: float | None = None
    red_saturation: float | None = None
    green_hue: float | None = None
    green_saturation: float | None = None
    blue_hue: float | None = None
    blue_saturation: float | None = None


@dataclass
class SeedTarget:
    """Target aggregated from the k nearest seeds (or a single seed if the match
    is near-exact)."""

    temperature: float | None
    tint: float | None
    tone: ToneStats | None
    bands: list[BandStats] | None
    shadow_tint: float | None
    red_hue: float | None
    red_saturation: float | None
    green_hue: float | None
    green_saturation: float | None
    blue_hue: float | None
    blue_saturation: float | None
    n_matched: int
    seed_ids: list[str]
    confidence: float | None = None  # nearest matched seed's raw distance (cf. PLAN.md N3)

    def has_calibration(self) -> bool:
        return any(getattr(self, f) is not None for f in CALIB_FIELDS)


def _f(dev: dict, key: str, default: float | None = None) -> float | None:
    v = (dev or {}).get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_seed_vector(conn, uuid: str) -> SeedVector | None:
    """Builds a seed's vector from the cache (no freshness check —
    cf. `cache.get_source_raw_latest`). `None` if the RAW analysis is missing
    (the seed hasn't gone through "Analyze selection" yet)."""
    sr = cachemod.get_source_raw_latest(conn, uuid)
    if sr is None or sr["asshot_rg"] is None:
        return None
    pic = cachemod.get_picture(conn, uuid)
    dev = pic["current_develop"] if pic else {}
    preview = cachemod.get_preview_jpeg_latest(conn, uuid)
    profile = sr.get("profile_capture") or (pic.get("profile_capture") if pic else None)
    return SeedVector(
        photo_id=uuid,
        asshot_rg=sr["asshot_rg"],
        asshot_bg=sr["asshot_bg"],
        raw_median_l=sr["tone"].median_l if sr["tone"] else None,
        temperature=_f(dev, "Temperature"),
        tint=_f(dev, "Tint"),
        preview_tone=preview.tone if preview else None,
        preview_bands=preview.bands if preview else None,
        raw_bands=sr.get("bands"),
        profile_capture=profile,
        ev100=sr.get("ev100"),
        shadow_tint=_f(dev, "ShadowTint"),
        red_hue=_f(dev, "RedHue"),
        red_saturation=_f(dev, "RedSaturation"),
        green_hue=_f(dev, "GreenHue"),
        green_saturation=_f(dev, "GreenSaturation"),
        blue_hue=_f(dev, "BlueHue"),
        blue_saturation=_f(dev, "BlueSaturation"),
    )


def build_seed_pool(conn) -> list[SeedVector]:
    """All usable seeds in the catalog (RAW analysis present)."""
    out = []
    for uuid in cachemod.list_seed_uuids(conn):
        v = build_seed_vector(conn, uuid)
        if v is not None:
            out.append(v)
    return out


def _band_frac_map(bands: list[BandStats] | None) -> dict[str, float] | None:
    """`{band_name: frac}` for a seed's RAW sharp-zone bands, or `None` if that
    seed has no band measurement at all (soft-skipped in `_distance`, same
    missing-feature semantics as the scalar features)."""
    if bands is None:
        return None
    return {b.name: b.frac for b in bands}


def _std(vals: list[float]) -> float:
    """Population standard deviation, `1.0` if too few samples to be meaningful
    (mirrors a no-op scale, same convention as the previous inline computation)."""
    if len(vals) < 2:
        return 1.0
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return math.sqrt(var) or 1.0


def _distance(
    target: SeedVector,
    seed: SeedVector,
    scale: dict[str, float],
    weights: dict[str, float] | None = None,
) -> float:
    """Normalized Euclidean distance (z-score) over the scalar features
    (asshot_rg, asshot_bg, raw_median_l) plus, when both sides have a RAW band
    measurement, the 8 per-band population fractions (colour composition —
    cf. PLAN.md D1). A feature missing on either side is ignored (no penalty).

    `weights` (cf. PLAN.md N1) multiplies a feature's squared z-score term —
    `None`/absent key defaults to 1.0, i.e. the historical uniform distance."""
    w = weights or {}
    acc = 0.0
    for key in _SCALAR_FEATURES:
        tv, sv = getattr(target, key), getattr(seed, key)
        if tv is None or sv is None:
            continue
        s = scale.get(key) or 1.0
        acc += w.get(key, 1.0) * ((tv - sv) / s) ** 2

    t_bands, s_bands = _band_frac_map(target.raw_bands), _band_frac_map(seed.raw_bands)
    if t_bands is not None and s_bands is not None:
        for name in BAND_NAMES:
            key = _BAND_FEATURE_PREFIX + name
            s = scale.get(key) or 1.0
            acc += w.get(key, 1.0) * ((t_bands.get(name, 0.0) - s_bands.get(name, 0.0)) / s) ** 2
    return math.sqrt(acc)


def _feature_scale(seeds: list[SeedVector]) -> dict[str, float]:
    """Standard deviation (per feature) of the seed pool — normalizes the Euclidean
    distance so that features on very different scales (rg/bg ~0.1-3, L* ~0-100,
    band frac 0-1) weigh comparably."""
    scale: dict[str, float] = {}
    for key in _SCALAR_FEATURES:
        vals = [getattr(s, key) for s in seeds if getattr(s, key) is not None]
        scale[key] = _std(vals)
    band_maps = [_band_frac_map(s.raw_bands) for s in seeds]
    for name in BAND_NAMES:
        vals = [m.get(name, 0.0) for m in band_maps if m is not None]
        scale[_BAND_FEATURE_PREFIX + name] = _std(vals)
    return scale


def k_nearest_weighted(
    target: SeedVector,
    seeds: list[SeedVector],
    weights: dict[str, float] | None = None,
    k: int | None = None,
) -> list[tuple[SeedVector, float]]:
    """The k seeds closest to `target` (excluding target itself), each distance
    feature scaled by `weights.get(feature_key, 1.0)` on top of the usual
    z-score normalization (cf. PLAN.md N1 — a uniform `weights=None`/`{}`
    reproduces `k_nearest`'s behavior exactly). Used by the per-band k-NN
    (N2) to make one band's own composition feature dominate its own match.

    `k` defaults to `min(K_MAX, max(1, n_seeds // 2))`. If the closest one is at
    a near-zero distance (exact match), only that one is returned.

    Intent behind the `pool // 2` (Fable 5 review A-07): on a small pool (3-5
    seeds), averaging half the pool would dilute the target with distant seeds
    — so k=3 is only reached from 6 seeds onward, and that's intentional.
    """
    pool = [s for s in seeds if s.photo_id != target.photo_id]
    if not pool:
        return []
    if k is None:
        k = min(K_MAX, max(1, len(pool) // 2))
    scale = _feature_scale(pool)
    ranked = sorted(
        ((s, _distance(target, s, scale, weights)) for s in pool), key=lambda t: t[1]
    )
    if ranked[0][1] < 1e-6:
        return [ranked[0]]
    return ranked[:k]


def k_nearest(
    target: SeedVector, seeds: list[SeedVector], k: int | None = None
) -> list[tuple[SeedVector, float]]:
    """Uniform-weight shortcut over `k_nearest_weighted` (all features at
    weight 1.0) — the historical single global match."""
    return k_nearest_weighted(target, seeds, None, k)


def _circular_mean_deg(values: list[float]) -> float:
    if not values:
        return 0.0
    ang = [math.radians(v) for v in values]
    s = sum(math.sin(a) for a in ang)
    c = sum(math.cos(a) for a in ang)
    return math.degrees(math.atan2(s, c)) % 360.0


def _weighted(values: list[tuple[float, float]]) -> float | None:
    """Weighted average `[(value, weight), ...]`. None if nothing usable."""
    total_w = sum(w for _, w in values)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in values) / total_w


def _weighted_tone(matches: list[tuple[SeedVector, float]]) -> ToneStats | None:
    items = [(m.preview_tone, w) for m, _d in matches if m.preview_tone is not None
             for w in [1.0 / (_d + 1e-6)]]
    if not items:
        return None
    fields = ("median_l", "mean_l", "p05_l", "p95_l", "clipped_hi", "clipped_lo", "tonal_frac")
    kwargs = {f: _weighted([(getattr(t, f), w) for t, w in items]) for f in fields}
    return ToneStats(**kwargs)


def _weighted_single_band(
    matches: list[tuple[SeedVector, float]], name: str
) -> BandStats | None:
    """Aggregates one named band across the matched seeds (1/distance weighting),
    reliable occurrences only. `None` if no matched seed has a reliable reading
    of that band."""
    items = []
    for m, d in matches:
        if not m.preview_bands:
            continue
        b = next((b for b in m.preview_bands if b.name == name), None)
        if b is None or not band_is_reliable(b):
            continue
        items.append((b, 1.0 / (d + 1e-6)))
    if not items:
        return None
    return BandStats(
        name=name,
        frac=_weighted([(b.frac, w) for b, w in items]) or 0.0,
        median_hue=_circular_mean_deg([b.median_hue for b, _ in items]),
        median_chroma=_weighted([(b.median_chroma, w) for b, w in items]) or 0.0,
        median_sat=_weighted([(b.median_sat, w) for b, w in items]) or 0.0,
        sat_clip_frac=_weighted([(b.sat_clip_frac, w) for b, w in items]) or 0.0,
        median_l=_weighted([(b.median_l, w) for b, w in items]) or 0.0,
    )


def _weighted_bands(matches: list[tuple[SeedVector, float]]) -> list[BandStats] | None:
    """Every band's aggregate from a single shared match list (historical
    behavior — whichever bands those seeds happen to carry reliably)."""
    out = [b for name in BAND_NAMES if (b := _weighted_single_band(matches, name)) is not None]
    return out or None


def _weighted_bands_per_band(
    band_matches: dict[str, list[tuple[SeedVector, float]]]
) -> list[BandStats] | None:
    """Like `_weighted_bands`, but each band is aggregated from its **own**
    dedicated match list (cf. PLAN.md N2 — `match_bands_per_band`)."""
    out = [
        b
        for name, matches in band_matches.items()
        if (b := _weighted_single_band(matches, name)) is not None
    ]
    return out or None


# Maximum tolerated divergence (slider points, -100..100 scale) between the k
# seeds matched on the same Calibration field before refusing the weighted
# average and falling back to the nearest seed (cf. PLAN.md C2 — a `RedHue` of
# +30 on one seed and -20 on another shouldn't produce an average that matches
# no real seed). Provisional value chosen in the same order of magnitude as the
# existing correction guards (`hsl._MAX_SAT=25`), for lack of real conflicting
# seed data to settle it (cf. C3, unresolved).
_CALIB_SPREAD_MAX = 25.0


def _weighted_calib_field(matches: list[tuple[SeedVector, float]], field: str) -> float | None:
    """Weighted average of a Calibration field — unless the matched seeds diverge
    too much on that field (`_CALIB_SPREAD_MAX`), in which case we fall back to
    the value of the nearest seed by distance rather than averaging blindly
    (cf. PLAN.md C2)."""
    items = [(m, d) for m, d in matches if getattr(m, field) is not None]
    if not items:
        return None
    values = [getattr(m, field) for m, _ in items]
    if len(values) > 1 and (max(values) - min(values)) > _CALIB_SPREAD_MAX:
        nearest, _ = min(items, key=lambda t: t[1])
        return getattr(nearest, field)
    return _weighted([(getattr(m, field), 1.0 / (d + 1e-6)) for m, d in items])


def target_from_seeds(matches: list[tuple[SeedVector, float]]) -> SeedTarget | None:
    """Aggregates the matched seeds (1/distance weighting) into a single target."""
    if not matches:
        return None
    temps = [(m.temperature, 1.0 / (d + 1e-6)) for m, d in matches if m.temperature is not None]
    tints = [(m.tint, 1.0 / (d + 1e-6)) for m, d in matches if m.tint is not None]
    return SeedTarget(
        temperature=_weighted(temps),
        tint=_weighted(tints),
        tone=_weighted_tone(matches),
        bands=_weighted_bands(matches),
        shadow_tint=_weighted_calib_field(matches, "shadow_tint"),
        red_hue=_weighted_calib_field(matches, "red_hue"),
        red_saturation=_weighted_calib_field(matches, "red_saturation"),
        green_hue=_weighted_calib_field(matches, "green_hue"),
        green_saturation=_weighted_calib_field(matches, "green_saturation"),
        blue_hue=_weighted_calib_field(matches, "blue_hue"),
        blue_saturation=_weighted_calib_field(matches, "blue_saturation"),
        n_matched=len(matches),
        seed_ids=[m.photo_id for m, _ in matches],
        confidence=min(d for _m, d in matches),  # nearest raw distance, order-independent
    )


def _filter_by_profile(target: SeedVector, seeds: list[SeedVector]) -> list[SeedVector]:
    """Restricts the pool to seeds sharing the **same creative profile** as the
    target, if possible.

    The camera creative profile (Standard/IN/SH/VV2…) correlates with editing
    style and exposure bias (cf. intentional under-exposure under IN/SH).
    Matching within the same group avoids transferring a target from a
    different regime. **Soft filter**: if the target has no profile, or no
    seed shares it, the full pool is kept (never an empty pool → no
    regression on small seed sets)."""
    if target.profile_capture is None:
        return seeds
    same = [s for s in seeds if s.profile_capture == target.profile_capture]
    return same if same else seeds


# Extra weight given to a band's own composition feature (cf. D1's
# `band_frac_<Name>`) when running its dedicated per-band k-NN (N2) — seeds
# must first resemble the target in that specific hue's population; the base
# scalar features (exposure/WB) and other bands stay in the mix at weight 1.0
# so the match doesn't ignore overall scene similarity entirely.
_BAND_KNN_SELF_WEIGHT = 4.0


def _band_weights(name: str) -> dict[str, float]:
    return {_BAND_FEATURE_PREFIX + name: _BAND_KNN_SELF_WEIGHT}


def match_bands_per_band(
    target: SeedVector,
    seeds: list[SeedVector],
    k: int | None = None,
    *,
    profile_aware: bool = True,
) -> dict[str, list[tuple[SeedVector, float]]]:
    """Runs one dedicated k-NN per HSL band (cf. PLAN.md N2), instead of
    reusing a single global match and averaging whichever bands those seeds
    happen to carry — "if a photo renders reds better, pick it for reds"."""
    pool = _filter_by_profile(target, seeds) if profile_aware else seeds
    return {
        name: k_nearest_weighted(target, pool, _band_weights(name), k) for name in BAND_NAMES
    }


def match_target_per_band(
    target: SeedVector,
    seeds: list[SeedVector],
    k: int | None = None,
    *,
    profile_aware: bool = True,
) -> SeedTarget | None:
    """Like `match_target`, but `bands` comes from `match_bands_per_band` (N2)
    instead of the single global match. Temperature/Tint/Calibration stay on
    the global match — they follow the overall scene, not a single hue band
    (cf. PLAN.md N2, same decision as the historical `target_from_seeds`).
    `confidence` also stays the global match's nearest distance (the gate is
    about trusting the overall scene match, not any one band)."""
    pool = _filter_by_profile(target, seeds) if profile_aware else seeds
    global_matches = k_nearest(target, pool, k)
    base = target_from_seeds(global_matches)
    if base is None:
        return None
    band_matches = {
        name: k_nearest_weighted(target, pool, _band_weights(name), k) for name in BAND_NAMES
    }
    base.bands = _weighted_bands_per_band(band_matches)
    return base


def pool_confidence_threshold(seeds: list[SeedVector], percentile: float = 75.0) -> float | None:
    """Confidence gate baseline (cf. PLAN.md N3): each seed's nearest-neighbor
    distance *within the pool itself*, at `percentile`. A match whose nearest
    seed is farther than this is landing in a sparser region than seeds
    normally sit from each other — a signal to flag, not a hard cutoff.

    Expensive-ish (O(n^2) over the pool) — call **once per batch** (e.g. once
    per `plan()` run over a whole catalog selection), not per target photo."""
    if len(seeds) < 2:
        return None
    scale = _feature_scale(seeds)
    nearest = []
    for s in seeds:
        others = [o for o in seeds if o.photo_id != s.photo_id]
        if not others:
            continue
        nearest.append(min(_distance(s, o, scale) for o in others))
    if not nearest:
        return None
    ranked = sorted(nearest)
    idx = min(len(ranked) - 1, max(0, round((percentile / 100.0) * (len(ranked) - 1))))
    return ranked[idx]


def is_low_confidence(target: SeedTarget, threshold: float | None) -> bool:
    """`True` if `target`'s nearest matched seed is farther than `threshold`
    (from `pool_confidence_threshold`). `False` if either side is unknown
    (`threshold is None` — too few seeds to establish a baseline — or
    `target.confidence is None`, which `target_from_seeds` never actually
    produces, but a caller could hand-build a `SeedTarget` without one)."""
    if threshold is None or target.confidence is None:
        return False
    return target.confidence > threshold


def match_target_with_distance(
    target: SeedVector,
    seeds: list[SeedVector],
    k: int | None = None,
    *,
    profile_aware: bool = True,
) -> tuple[SeedTarget | None, float | None]:
    """Like `match_target`, also returns the nearest matched seed's raw distance
    (confidence proxy — cf. PLAN.md N3; used by the S0 validation harness to
    stratify LOOCV error by proximity to the seed pool)."""
    pool = _filter_by_profile(target, seeds) if profile_aware else seeds
    matches = k_nearest(target, pool, k)
    nearest = matches[0][1] if matches else None
    return target_from_seeds(matches), nearest


def match_target(
    target: SeedVector,
    seeds: list[SeedVector],
    k: int | None = None,
    *,
    profile_aware: bool = True,
) -> SeedTarget | None:
    """Shortcut: (soft profile filter) + k nearest + aggregation into a single target."""
    tgt, _dist = match_target_with_distance(target, seeds, k, profile_aware=profile_aware)
    return tgt
