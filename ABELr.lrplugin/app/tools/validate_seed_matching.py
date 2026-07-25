"""PLAN step S0 — validation harness for k-NN seed matching (seeds mode) and the
neutral-anchored embedded-JPEG prediction (embedded mode).

**Read-only on `ABELr_cache.db`, no Lightroom connection needed** (unlike
`calibrate_hsl_response.py`): opens the cache in SQLite read-only mode (never
calls `cache.open_cache`, which DROPs+recreates the schema on a version
mismatch — not acceptable on the user's real, live cache) and replays the
production matching/planning logic (`core.seed_match`, `core.autocorrect`)
against historical ground truth already sitting in the cache.

- **Seeds-mode LOOCV**: for each seed, hold it out, re-match against the
  remaining pool (`seed_match.match_target_with_distance`), compare the
  predicted target to the held-out seed's own ground truth (its
  `PreviewJPEG` tone/bands + its real `develop_json` Temp/Tint/Calibration).
- **Embedded-mode validation**: for every photo with a cached
  `NeutralPreviewJPEG` anchor, run `autocorrect.plan(..., forced_embedded=True)`
  and compare the predicted T-N delta (expo/wb/hsl) to the real `develop_json`
  gap vs. the neutral point (Exposure2012=0 / WB As Shot / HSL 0).

Usage:
    python -m app.tools.validate_seed_matching "<catalog folder or ABELr_cache.db path>"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core import autocorrect, cache, render_metrics, response, seed_match  # noqa: E402
from app.core.autocorrect import PhotoMeasure  # noqa: E402
from app.core.render_metrics import band_is_reliable  # noqa: E402
from app.core.seed_match import CALIB_FIELDS, SeedVector  # noqa: E402

_HSL_AXES = ("Saturation", "Luminance", "Hue")
_EMBEDDED_AXES = frozenset({"expo", "wb", "hsl"})


# --------------------------------------------------------------------------- #
# Pure helpers — error aggregation (unit-tested, no DB involved)
# --------------------------------------------------------------------------- #
def mae(pairs: list[tuple[float, float]]) -> float | None:
    """Mean absolute error over `(predicted, actual)` pairs. `None` if empty."""
    if not pairs:
        return None
    return sum(abs(p - a) for p, a in pairs) / len(pairs)


def circular_mae(pairs: list[tuple[float, float]]) -> float | None:
    """MAE for circular (hue, degrees) quantities — shortest-arc difference."""
    if not pairs:
        return None

    def diff(a: float, b: float) -> float:
        return abs((a - b + 180.0) % 360.0 - 180.0)

    return sum(diff(p, a) for p, a in pairs) / len(pairs)


def split_by_median(
    triples: list[tuple[float, float, float]],
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Splits `(distance, predicted, actual)` triples into (near-half, far-half)
    by the median of `distance` — stratification proxy for "same scene block"
    (cf. PLAN.md S0: known LOOCV over-generalization bias within a block)."""
    if not triples:
        return [], []
    med = sorted(d for d, _, _ in triples)[len(triples) // 2]
    near = [(p, a) for d, p, a in triples if d <= med]
    far = [(p, a) for d, p, a in triples if d > med]
    return near, far


# --------------------------------------------------------------------------- #
# Seeds-mode LOOCV
# --------------------------------------------------------------------------- #
@dataclass
class SeedsLoocvResult:
    n_seeds: int
    expo_triples: list[tuple[float, float, float]] = field(default_factory=list)
    temp_triples: list[tuple[float, float, float]] = field(default_factory=list)
    tint_triples: list[tuple[float, float, float]] = field(default_factory=list)
    calib_pairs: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    hsl_chroma: list[tuple[float, float]] = field(default_factory=list)
    hsl_lstar: list[tuple[float, float]] = field(default_factory=list)
    hsl_hue: list[tuple[float, float]] = field(default_factory=list)


def run_seeds_loocv(pool: list[SeedVector]) -> SeedsLoocvResult:
    r = SeedsLoocvResult(n_seeds=len(pool), calib_pairs={f: [] for f in CALIB_FIELDS})
    for s in pool:
        target, dist = seed_match.match_target_with_distance(s, pool)
        if target is None or dist is None:
            continue
        if target.tone is not None and s.preview_tone is not None:
            r.expo_triples.append((dist, target.tone.median_l, s.preview_tone.median_l))
        if target.temperature is not None and s.temperature is not None:
            r.temp_triples.append((dist, target.temperature, s.temperature))
        if target.tint is not None and s.tint is not None:
            r.tint_triples.append((dist, target.tint, s.tint))
        for f in CALIB_FIELDS:
            pv, av = getattr(target, f), getattr(s, f)
            if pv is not None and av is not None:
                r.calib_pairs[f].append((pv, av))
        if target.bands and s.preview_bands:
            actual_by_name = {b.name: b for b in s.preview_bands if band_is_reliable(b)}
            for b in target.bands:
                ab = actual_by_name.get(b.name)
                if ab is None:
                    continue
                r.hsl_chroma.append((b.median_chroma, ab.median_chroma))
                r.hsl_lstar.append((b.median_l, ab.median_l))
                r.hsl_hue.append((b.median_hue, ab.median_hue))
    return r


def _print_seeds_result(r: SeedsLoocvResult) -> None:
    def fmt(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.2f}"

    expo_pairs = [(p, a) for _, p, a in r.expo_triples]
    print(f"exposure (target L*)  MAE={fmt(mae(expo_pairs))}  n={len(expo_pairs)}")
    near, far = split_by_median(r.expo_triples)
    print(f"  by nearest-seed distance: near-half MAE={fmt(mae(near))} (n={len(near)}), "
          f"far-half MAE={fmt(mae(far))} (n={len(far)})")

    temp_pairs = [(p, a) for _, p, a in r.temp_triples]
    print(f"WB Temperature (K)    MAE={fmt(mae(temp_pairs))}  n={len(temp_pairs)}")
    near, far = split_by_median(r.temp_triples)
    print(f"  by nearest-seed distance: near-half MAE={fmt(mae(near))} (n={len(near)}), "
          f"far-half MAE={fmt(mae(far))} (n={len(far)})")

    tint_pairs = [(p, a) for _, p, a in r.tint_triples]
    print(f"WB Tint                MAE={fmt(mae(tint_pairs))}  n={len(tint_pairs)}")

    print("Calibration fields (7):")
    for f in CALIB_FIELDS:
        pairs = r.calib_pairs.get(f, [])
        print(f"  {f:<16} MAE={fmt(mae(pairs))}  n={len(pairs)}")

    print("HSL (aggregated across 8 bands, seeds mode):")
    print(f"  chroma (C*)    MAE={fmt(mae(r.hsl_chroma))}  n={len(r.hsl_chroma)}")
    print(f"  lightness (L*) MAE={fmt(mae(r.hsl_lstar))}  n={len(r.hsl_lstar)}")
    print(f"  hue (deg)      MAE={fmt(circular_mae(r.hsl_hue))}  n={len(r.hsl_hue)}")


# --------------------------------------------------------------------------- #
# Embedded-mode validation
# --------------------------------------------------------------------------- #
@dataclass
class EmbeddedValidationResult:
    n_candidates: int
    n_resolved: int
    expo_pairs: list[tuple[float, float]] = field(default_factory=list)
    wb_temp_pairs: list[tuple[float, float]] = field(default_factory=list)
    wb_tint_pairs: list[tuple[float, float]] = field(default_factory=list)
    wb_n_uncalibrated: int = 0
    hsl_pairs: dict[str, list[tuple[float, float]]] = field(
        default_factory=lambda: {axis: [] for axis in _HSL_AXES}
    )
    notes: list[str] = field(default_factory=list)


def _build_embedded_measure(conn: sqlite3.Connection, uuid: str) -> PhotoMeasure | None:
    pic = cache.get_picture(conn, uuid)
    if pic is None:
        return None
    t = cache.get_in_camera_jpeg_latest(conn, uuid)
    n = cache.get_neutral_preview_latest(conn, uuid)
    if t is None or n is None:
        return None
    t_ok = (t["sharp"] is not None and t["sharp"].tone is not None) or (
        t["global"] is not None and t["global"].tone is not None
    )
    n_ok = (n["sharp"] is not None and n["sharp"].tone is not None) or (
        n["glob"] is not None and n["glob"].tone is not None
    )
    if not t_ok or not n_ok:
        return None
    sr = cache.get_source_raw_latest(conn, uuid)
    cam_row = conn.execute("SELECT exif_camera FROM LightroomPicture WHERE uuid=?", (uuid,)).fetchone()
    return PhotoMeasure(
        photo_id=uuid,
        path=pic["path"] or "",
        current_develop=pic["current_develop"],
        exif_camera=cam_row["exif_camera"] if cam_row else None,
        is_seed=False,
        raw_tone=sr["tone"] if sr else None,
        raw_bands=sr["bands"] if sr else None,
        embedded_sharp=t["sharp"],
        embedded_global=t["global"],
        neutral_sharp=n["sharp"],
        neutral_global=n["glob"],
        neutral_asshot_temp=n["asshot_temp"],
        neutral_asshot_tint=n["asshot_tint"],
        profile_capture=pic["profile_capture"],
    )


def run_embedded_validation(conn: sqlite3.Connection) -> EmbeddedValidationResult:
    """Runs `autocorrect.plan(forced_embedded=True)` on every photo with a cached
    neutral anchor and compares the predicted absolute values (anchor=0) to the
    real `develop_json` (already an absolute gap vs. the neutral point by
    construction — Exposure2012/HSL anchor at 0, WB anchor at As Shot).

    Measures are grouped by `(exif_camera, profile_capture)` and each group gets
    its own `response.load(...)` model — matching the real production lookup
    (`gui.autocorrect_worker`, keyed by the **in-camera creative profile**, not
    the Lr DCP `CameraProfile` develop key — those are two different axes; a
    catalog can have one constant DCP profile across every in-camera style).
    A group with no camera gets `model=None` (uncalibrated, honestly reported).
    """
    uuids = [row["uuid"] for row in conn.execute("SELECT uuid FROM NeutralPreviewJPEG")]
    measures = [m for uuid in uuids if (m := _build_embedded_measure(conn, uuid)) is not None]

    groups: dict[tuple[str | None, str | None], list[PhotoMeasure]] = {}
    for m in measures:
        groups.setdefault((m.exif_camera, m.profile_capture), []).append(m)

    adjustments: list = []
    all_notes: list[str] = []
    wb_calibrated_by_id: dict[str, bool] = {}
    for (camera, profile), group in groups.items():
        model = response.load(camera, profile) if camera else None
        calibrated = bool(model and model.wb.is_calibrated())
        for m in group:
            wb_calibrated_by_id[m.photo_id] = calibrated
        group_adj, group_diag = autocorrect.plan(
            group, axes=_EMBEDDED_AXES, forced_embedded=True, model=model, seed_pool=[]
        )
        adjustments.extend(group_adj)
        all_notes.append(f"[{camera}|{profile}] n={len(group)} wb_calibrated={calibrated}")
        all_notes.extend(group_diag.notes)
    dev_by_id = {a.photo_id: a.develop for a in adjustments}

    r = EmbeddedValidationResult(n_candidates=len(uuids), n_resolved=len(measures), notes=all_notes)
    for m in measures:
        dev = m.current_develop or {}
        predicted = dev_by_id.get(m.photo_id, {})

        actual_ev = float(dev.get("Exposure2012") or 0.0)
        pred_ev = float(predicted.get("Exposure2012", 0.0))
        r.expo_pairs.append((pred_ev, actual_ev))

        if dev.get("WhiteBalance") == "Custom" and m.neutral_asshot_temp is not None:
            actual_temp = dev.get("Temperature")
            if actual_temp is not None:
                pred_temp = predicted.get("Temperature")
                if pred_temp is None and wb_calibrated_by_id.get(m.photo_id):
                    # Calibrated, but within the dead zone -> nothing WRITTEN, yet the
                    # prediction is still real (predicted delta 0, i.e. the anchor
                    # itself) — comparable to ground truth like Exposure/HSL, not a
                    # "can't evaluate" case.
                    pred_temp = m.neutral_asshot_temp
                    predicted = {**predicted, "Tint": predicted.get("Tint", m.neutral_asshot_tint or 0.0)}
                if pred_temp is not None:
                    r.wb_temp_pairs.append((float(pred_temp), float(actual_temp)))
                    actual_tint = float(dev.get("Tint") or 0.0)
                    pred_tint = float(predicted.get("Tint", 0.0))
                    r.wb_tint_pairs.append((pred_tint, actual_tint))
                else:
                    r.wb_n_uncalibrated += 1  # genuinely no calibrated response for this
                    # photo's (camera, profile_capture) group — cannot evaluate at all

        for band in render_metrics.BAND_NAMES:
            for axis in _HSL_AXES:
                key = f"{axis}Adjustment{band}"
                actual_v = float(dev.get(key) or 0.0)
                pred_v = float(predicted.get(key, 0.0))
                r.hsl_pairs[axis].append((pred_v, actual_v))
    return r


def _print_embedded_result(r: EmbeddedValidationResult) -> None:
    def fmt(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.2f}"

    print(f"candidates (NeutralPreviewJPEG cached): {r.n_candidates}, resolved (usable pair): {r.n_resolved}")
    for note in r.notes:
        print(f"  note: {note}")
    print(f"exposure (Exposure2012, EV)  MAE={fmt(mae(r.expo_pairs))}  n={len(r.expo_pairs)}")
    print(f"WB Temperature (K)           MAE={fmt(mae(r.wb_temp_pairs))}  n={len(r.wb_temp_pairs)}"
          f"  ({r.wb_n_uncalibrated} photo(s) with a real Custom WB edit but no calibrated"
          f" response to compare against — see diag notes for the deviant-uncorrected subset)")
    print(f"WB Tint                      MAE={fmt(mae(r.wb_tint_pairs))}  n={len(r.wb_tint_pairs)}")
    print("HSL (24 keys = 8 bands x 3 axes, embedded mode):")
    for axis in _HSL_AXES:
        pairs = r.hsl_pairs[axis]
        print(f"  {axis:<10} MAE={fmt(mae(pairs))}  n={len(pairs)}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _resolve_db_path(arg: str) -> Path:
    p = Path(arg)
    return p / cache.CACHE_FILENAME if p.is_dir() else p


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Read-only connection on an existing `ABELr_cache.db` — never touches the
    schema (unlike `cache.open_cache`, which DROPs+recreates the tables on a
    version mismatch: not acceptable on the user's real, live cache)."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("catalog", help="catalog folder or ABELr_cache.db path")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    db_path = _resolve_db_path(a.catalog)
    if not db_path.is_file():
        raise SystemExit(f"cache not found: {db_path}")
    conn = open_readonly(db_path)
    try:
        pool = seed_match.build_seed_pool(conn)
        print(f"=== Seeds-mode LOOCV ({len(pool)} usable seeds) ===")
        _print_seeds_result(run_seeds_loocv(pool))

        print("\n=== Embedded-mode validation ===")
        _print_embedded_result(run_embedded_validation(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
