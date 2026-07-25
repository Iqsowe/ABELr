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
from .render_metrics import BandStats, ToneStats, band_is_reliable

K_MAX = 3


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


def _distance(target: SeedVector, seed: SeedVector, scale: dict[str, float]) -> float:
    """Normalized Euclidean distance (z-score) over (asshot_rg, asshot_bg, raw_median_l).
    A feature missing on either side is ignored (no penalty)."""
    acc = 0.0
    for key in ("asshot_rg", "asshot_bg", "raw_median_l"):
        tv, sv = getattr(target, key), getattr(seed, key)
        if tv is None or sv is None:
            continue
        s = scale.get(key) or 1.0
        acc += ((tv - sv) / s) ** 2
    return math.sqrt(acc)


def _feature_scale(seeds: list[SeedVector]) -> dict[str, float]:
    """Standard deviation (per feature) of the seed pool — normalizes the Euclidean
    distance so that features on very different scales (rg/bg ~0.1-3, L* ~0-100)
    weigh comparably."""
    scale: dict[str, float] = {}
    for key in ("asshot_rg", "asshot_bg", "raw_median_l"):
        vals = [getattr(s, key) for s in seeds if getattr(s, key) is not None]
        if len(vals) < 2:
            scale[key] = 1.0
            continue
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        scale[key] = math.sqrt(var) or 1.0
    return scale


def k_nearest(
    target: SeedVector, seeds: list[SeedVector], k: int | None = None
) -> list[tuple[SeedVector, float]]:
    """The k seeds closest to `target` (excluding target itself).

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
    ranked = sorted(((s, _distance(target, s, scale)) for s in pool), key=lambda t: t[1])
    if ranked[0][1] < 1e-6:
        return [ranked[0]]
    return ranked[:k]


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


def _weighted_bands(matches: list[tuple[SeedVector, float]]) -> list[BandStats] | None:
    by_name: dict[str, list[tuple[BandStats, float]]] = {}
    for m, d in matches:
        if not m.preview_bands:
            continue
        w = 1.0 / (d + 1e-6)
        for b in m.preview_bands:
            if not band_is_reliable(b):
                continue
            by_name.setdefault(b.name, []).append((b, w))
    if not by_name:
        return None
    out: list[BandStats] = []
    for name, items in by_name.items():
        out.append(
            BandStats(
                name=name,
                frac=_weighted([(b.frac, w) for b, w in items]) or 0.0,
                median_hue=_circular_mean_deg([b.median_hue for b, _ in items]),
                median_chroma=_weighted([(b.median_chroma, w) for b, w in items]) or 0.0,
                median_sat=_weighted([(b.median_sat, w) for b, w in items]) or 0.0,
                sat_clip_frac=_weighted([(b.sat_clip_frac, w) for b, w in items]) or 0.0,
                median_l=_weighted([(b.median_l, w) for b, w in items]) or 0.0,
            )
        )
    return out


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
