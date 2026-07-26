"""Orchestrates per-photo automatic correction (exposure + WB + HSL).

Pure and testable (like `exposure`/`hsl`/`seed_match`): receives the measurements
already collected by the GUI worker (+ the seed pool already built from the cache)
and returns one `PhotoAdjustment` per photo + a diagnostic. The Qt worker
(`gui.autocorrect_worker`) handles the I/O (jobs, decoding, cache, parallelism).

Reference modes (user decision):
- **seeds**: a pool of usable seeds exists (explicitly marked, cf.
  `cache.is_seed`) → for each target photo, we look for the seeds whose RAW
  analysis (sharp zone) is closest (`core.seed_match`), and use their already-
  edited rendered preview as the style reference. Current-state measurement =
  fresh render (`m.analysis`). The seeds themselves are NEVER rewritten.
- **embedded** (forced OR no usable seed): **anchored on the neutral render**.
  Target T = in-camera JPEG (immutable, **raw** measurement); anchor N =
  NeutralPreview (Lr render of the same RAW: current style, WB As Shot, Expo 0,
  HSL 0 — cf. `gui.neutral_preview_worker`). The correction targets T directly
  (L* tone, a*/b* cast, HSL bands): the T−N delta, converted into Lr settings,
  brings the RAW's render closer to the in-camera JPEG's look — **without
  subtracting a profile bias** (user decision: the in-camera look is
  transplanted as-is, bias revisited later). Anchor N remains indispensable
  (Lr applies deltas relative to the RAW's render; we cannot write the JPEG's
  absolute L*). The emitted values are **absolute** (anchor at zero) →
  idempotent, no dependency on the current render. Under dead zones, **no key
  is written** (preserves manual settings/presets).

Embedded measurements: **global** by default (T and N are two renders of the
same scene, no mask misalignment); switches to **sharp zone** on strong crop
(the Lr preview is cropped, the in-camera JPEG is not — the sharp mask anchors
on the common subject).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import hsl as _hsl
from . import exposure as _exp
from . import quantize as _quantize
from . import seed_match
from . import wb_model as _wb
from .hsl import BandTarget
from .pipeline import RenderAnalysis
from .render_metrics import BandStats, ToneStats, band_is_reliable
from .response import ResponseModel
from .seed_match import SeedTarget, SeedVector
from ..server.models import PhotoAdjustment

DEFAULT_AXES = frozenset({"expo", "wb", "hsl", "calib"})

# --------------------------------------------------------------------------- #
# Embedded-mode thresholds (neutral-anchored, per-photo deviation)
# --------------------------------------------------------------------------- #
# Exposure dead zone: below this |ΔEV| we do NOT write Exposure2012 (photo
# already matches the profile — don't overwrite a manual setting with ~0).
_EXPO_DEADBAND_EV = 0.10
# WB dead zone: a*b* cast distance (approx ΔE) below which we leave it alone.
_WB_CAST_DEADBAND = 3.0
# Minimum fraction of near-neutral pixels for a cast to be trusted.
_MIN_NEUTRAL_FRAC = 0.02
# Full confidence of the shared ProfileBias (zero bias, cf. _plan_embedded: the
# "bias ignored" decision removed the calibration pool — Fable 5 review DB-06).
_BIAS_FULL_N = 8
# Crop area (fraction of the frame) below which we measure in the sharp zone
# rather than global (frames too different between in-camera JPEG and cropped render).
_CROP_AREA_MIN = 0.8
# Global ↔ sharp-zone ΔL* divergence beyond which we flag "subject/background
# diverge" (backlight, subject lit differently from the background).
_DIVERGENCE_L = 4.0

# Slider-grid steps (PLAN.md step Q — user-imposed workflow constraint, applied
# to every *written* absolute value via `quantize.snap`). Exposure2012 stays
# continuous (out of scope).
_HSL_STEP = 5
_CALIB_STEP = 5
_TEMP_STEP = 250
_TINT_STEP = 5


@dataclass
class PhotoMeasure:
    """Measurements for one photo, collected by the worker.

    Seeds mode: `analysis` (fresh current render, sharp zone) required.
    Embedded mode: `embedded_*` (T = in-camera JPEG) and `neutral_*` (N =
    neutral render) required, each in global + sharp zone; `analysis` unused.
    """

    photo_id: str
    path: str
    current_develop: dict
    exif_camera: str | None
    analysis: RenderAnalysis | None = None   # current render (sharp zone) — seeds mode
    is_seed: bool = False                    # explicit marking (cache.is_seed)
    raw_tone: ToneStats | None = None        # source RAW, sharp zone — k-NN matching key
    raw_bands: list[BandStats] | None = None  # source RAW, sharp zone — raw_oversat HSL guard
    embedded_sharp: RenderAnalysis | None = None   # T: in-camera JPEG (sharp zone)
    embedded_global: RenderAnalysis | None = None  # T: in-camera JPEG (global)
    neutral_sharp: RenderAnalysis | None = None    # N: neutral render (sharp zone)
    neutral_global: RenderAnalysis | None = None   # N: neutral render (global)
    neutral_asshot_temp: float | None = None       # As Shot's numeric Temperature
    neutral_asshot_tint: float | None = None
    hash_style: str | None = None            # bias grouping key (with profile_capture)
    asshot_rg: float | None = None
    asshot_bg: float | None = None
    profile_capture: str | None = None       # in-camera creative profile (k-NN filter + bias)
    ev100: float | None = None               # scene context (diagnostic)


@dataclass
class ProfileBias:
    """Systematic T−N bias for a (in-camera creative profile, Lr style) pair.

    Robust medians of the per-photo deltas over the calibration pool:
    `l` (median ΔL*), `cast_a`/`cast_b` (Δcast a*/b* on neutrals),
    `bands[name] = (dchroma, dl, dhue)`. `n` = pool size (photos with tone).
    """

    n: int
    l: float = 0.0
    cast_a: float = 0.0
    cast_b: float = 0.0
    bands: dict[str, tuple[float, float, float]] = field(default_factory=dict)


@dataclass
class PlanDiagnostics:
    mode: str                       # "seeds" | "embedded"
    n_seeds: int
    n_targets: int
    notes: list[str] = field(default_factory=list)
    n_low_confidence: int = 0       # seeds mode: matched photo(s) above the pool's
                                     # own distance percentile (cf. PLAN.md N3/N4)


def _f(dev: dict, key: str, default: float = 0.0) -> float:
    v = (dev or {}).get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _hue_diff(a: float, b: float) -> float:
    """Signed circular difference a−b in [−180, 180) (the antipode gives −180)."""
    return (a - b + 180.0) % 360.0 - 180.0


# --------------------------------------------------------------------------- #
# Embedded mode — variant selection, profile bias, targets
# --------------------------------------------------------------------------- #
def _crop_area(dev: dict) -> float:
    """Lr crop area (fraction of the frame). 1.0 if no crop keys."""
    left = _f(dev, "CropLeft", 0.0)
    right = _f(dev, "CropRight", 1.0)
    top = _f(dev, "CropTop", 0.0)
    bottom = _f(dev, "CropBottom", 1.0)
    return max(0.0, right - left) * max(0.0, bottom - top)


def _variant_for(m: PhotoMeasure) -> str:
    """Embedded measurement variant: global by default, sharp zone on strong crop."""
    return "sharp" if _crop_area(m.current_develop) < _CROP_AREA_MIN else "global"


def _pair_for(
    m: PhotoMeasure, variant: str
) -> tuple[RenderAnalysis | None, RenderAnalysis | None, str]:
    """(T, N, effective variant) for the photo — falls back to the other variant
    if the requested one is incomplete (the returned variant also drives the bias choice)."""
    other_variant = "global" if variant == "sharp" else "sharp"
    if variant == "sharp":
        first = (m.embedded_sharp, m.neutral_sharp)
        other = (m.embedded_global, m.neutral_global)
    else:
        first = (m.embedded_global, m.neutral_global)
        other = (m.embedded_sharp, m.neutral_sharp)
    if first[0] is not None and first[1] is not None:
        return first[0], first[1], variant
    if other[0] is not None and other[1] is not None:
        return other[0], other[1], other_variant
    return None, None, variant


def _raw_oversat_by_name(raw_bands: list[BandStats] | None) -> dict[str, bool | None]:
    """Band name → `raw_oversat` (HSL guard), from the target photo's sharp-zone
    RAW bands (`PhotoMeasure.raw_bands` — the RAW of the photo being corrected,
    not the seeds': it's its own capture that must confirm the oversaturation)."""
    return {b.name: _hsl.raw_confirms_oversat(b) for b in (raw_bands or [])}


def _embedded_band_targets(
    t: RenderAnalysis,
    bias: ProfileBias,
    *,
    ignore_bias: bool = False,
    raw_bands: list[BandStats] | None = None,
) -> dict[str, BandTarget]:
    """Embedded HSL targets = in-camera JPEG bands.

    `ignore_bias=True` (live path): target = the in-camera JPEG's **raw** band
    (`target = T.band`), every reliable band counted — the in-camera look is
    transplanted as-is. `ignore_bias=False` (historical): `target = T.band − B.band`,
    we only target the deviation from the profile × style pair's norm, and
    bands without a bias norm are skipped.
    """
    raw_by_name = _raw_oversat_by_name(raw_bands)
    out: dict[str, BandTarget] = {}
    for b in t.bands or []:
        if not band_is_reliable(b):
            continue
        if ignore_bias:
            out[b.name] = BandTarget(
                name=b.name,
                chroma=b.median_chroma,
                lstar=b.median_l,
                hue=b.median_hue,
                raw_oversat=raw_by_name.get(b.name),
                embedded_raw=True,
            )
            continue
        b_bias = bias.bands.get(b.name)
        if b_bias is None:
            continue  # no norm for this band → no target (caution)
        dchroma, dl, dhue = b_bias
        out[b.name] = BandTarget(
            name=b.name,
            chroma=b.median_chroma - dchroma,
            lstar=b.median_l - dl,
            hue=b.median_hue - dhue,
            raw_oversat=raw_by_name.get(b.name),
        )
    return out


def _band_targets_from_seed_match(
    t: SeedTarget | None, raw_bands: list[BandStats] | None = None
) -> dict[str, BandTarget]:
    """HSL targets = aggregated bands from the closest seeds (already weighted).

    `raw_bands`: sharp-zone RAW of the **target** photo (the one being corrected,
    not the seeds) — used only as the `raw_oversat` guard (cf. `_raw_oversat_by_name`)."""
    out: dict[str, BandTarget] = {}
    if t is None or not t.bands:
        return out
    raw_by_name = _raw_oversat_by_name(raw_bands)
    for b in t.bands:
        out[b.name] = BandTarget(
            name=b.name,
            chroma=b.median_chroma,
            lstar=b.median_l,
            hue=b.median_hue,
            raw_oversat=raw_by_name.get(b.name),
        )
    return out


def _calib_develop_dict(t: SeedTarget) -> dict:
    """Calibration settings to write from a k-NN target — direct transplant
    (like Temperature/Tint), keys absent on the target are omitted (no forced 0
    on an axis with no seed). `EnableCalibration` is set as soon as any field is written."""
    keys = {
        "shadow_tint": "ShadowTint",
        "red_hue": "RedHue", "red_saturation": "RedSaturation",
        "green_hue": "GreenHue", "green_saturation": "GreenSaturation",
        "blue_hue": "BlueHue", "blue_saturation": "BlueSaturation",
    }
    out: dict = {}
    for field, sdk_key in keys.items():
        v = getattr(t, field)
        if v is not None:
            out[sdk_key] = _quantize.snap(max(-100, min(100, v)), _CALIB_STEP)
    if out:
        out["EnableCalibration"] = True
    return out


def plan(
    measures: list[PhotoMeasure],
    *,
    axes: frozenset[str] = DEFAULT_AXES,
    forced_embedded: bool = False,
    model: ResponseModel | None = None,
    seed_pool: list[SeedVector] | None = None,
) -> tuple[list[PhotoAdjustment], PlanDiagnostics]:
    """Plans the per-photo correction. See the module docstring for the modes."""
    seed_pool = seed_pool or []
    targets = [m for m in measures if not m.is_seed]
    mode_embedded = forced_embedded or not seed_pool

    dev_by_id: dict[str, dict] = {m.photo_id: {} for m in targets}
    diag = PlanDiagnostics(
        mode="embedded" if mode_embedded else "seeds",
        n_seeds=len(seed_pool),
        n_targets=len(targets),
    )
    if mode_embedded:
        reason = "checkbox checked" if forced_embedded else "no usable seed"
        diag.notes.insert(0, f"neutral-anchored embedded-JPEG mode ({reason})")
        adjustments, diag = _plan_embedded(targets, axes, model, dev_by_id, diag, seed_pool)
    else:
        diag.notes.insert(0, f"seeds mode — pool of {len(seed_pool)} seed(s)")
        adjustments, diag = _plan_seeds(targets, axes, model, seed_pool, dev_by_id, diag)

    if not targets and measures:
        # PLAN.md U4a: n_targets==0 with a non-empty selection is a distinct,
        # actionable case — every selected photo is a reference — not the
        # generic "no correction needed" the caller falls back to otherwise.
        # Inserted last (position 0) so it's the FIRST note the GUI shows,
        # ahead of the mode-selection note above.
        diag.notes.insert(
            0,
            f"{len(measures)} of {len(measures)} selected photo(s) are "
            "references — select non-reference photos",
        )
    return adjustments, diag


# --------------------------------------------------------------------------- #
# Embedded mode — neutral-anchored, per-photo deviation, absolute values
# --------------------------------------------------------------------------- #
def _plan_embedded(
    targets: list[PhotoMeasure],
    axes: frozenset[str],
    model: ResponseModel | None,
    dev_by_id: dict[str, dict],
    diag: PlanDiagnostics,
    seed_pool: list[SeedVector] | None = None,
) -> tuple[list[PhotoAdjustment], PlanDiagnostics]:
    # Target = raw in-camera JPEG measurement → zero profile bias (shared,
    # read-only): `bias.l`/`.cast_*`/`.bands` are 0 in the loops below.
    bias = ProfileBias(n=_BIAS_FULL_N)

    # Per-photo resolution: variant + (T, N) pair.
    resolved: list[tuple[PhotoMeasure, str, RenderAnalysis, RenderAnalysis, ProfileBias]] = []
    n_no_anchor = 0
    n_divergent = 0
    for m in targets:
        t, n, variant = _pair_for(m, _variant_for(m))
        if t is None or n is None or t.tone is None or n.tone is None:
            n_no_anchor += 1
            continue
        # Global ↔ sharp-zone divergence (subject/background diagnostic).
        if (
            m.embedded_global is not None and m.neutral_global is not None
            and m.embedded_sharp is not None and m.neutral_sharp is not None
            and m.embedded_global.tone and m.neutral_global.tone
            and m.embedded_sharp.tone and m.neutral_sharp.tone
        ):
            d_glob = m.embedded_global.tone.median_l - m.neutral_global.tone.median_l
            d_sharp = m.embedded_sharp.tone.median_l - m.neutral_sharp.tone.median_l
            if abs(d_glob - d_sharp) > _DIVERGENCE_L:
                n_divergent += 1
        if variant == "sharp":
            diag.notes.append(
                f"{m.photo_id[:8]}: strong crop (area {_crop_area(m.current_develop):.2f}) → measuring sharp zone"
            )
        resolved.append((m, variant, t, n, bias))

    diag.notes.append("profile bias ignored — target = raw in-camera JPEG measurements")
    if n_no_anchor:
        diag.notes.append(
            f"{n_no_anchor} photo(s) with no neutral anchor or in-camera target → skipped"
        )
    if n_divergent:
        diag.notes.append(
            f"{n_divergent} photo(s): global ↔ sharp-zone ΔL* diverge (> {_DIVERGENCE_L:g} L*) "
            f"— subject/background lit differently, correction worth checking"
        )

    # ---- Exposure: absolute value anchored at Exposure2012 = 0 ------------
    if "expo" in axes:
        samples = []
        for m, _variant, t, n, bias in resolved:
            desired_l = t.tone.median_l - bias.l
            samples.append(
                _exp.ExposureSample(
                    m.photo_id,
                    current_l=n.tone.median_l,
                    current_exposure=0.0,        # anchor: the delta IS the absolute value
                    desired_l=desired_l,
                    clipped_hi=n.tone.clipped_hi,
                    clipped_lo=n.tone.clipped_lo,
                )
            )
        n_written = 0
        n_conform = 0
        for adj in _exp.plan_from_render(samples, model.exposure if model else None):
            new_ev = adj.develop.get("Exposure2012", 0.0)
            if abs(new_ev) < _EXPO_DEADBAND_EV:
                n_conform += 1        # matches the profile → nothing written
                continue
            dev_by_id[adj.photo_id]["Exposure2012"] = new_ev
            n_written += 1
        diag.notes.append(
            f"expo: {n_written} deviant photo(s) corrected, {n_conform} matching the profile "
            f"(nothing written), out of {len(resolved)} resolved"
        )

    # ---- White balance: cast deviation, numeric As Shot base ---------------
    if "wb" in axes:
        wbresp = model.wb if model else None
        n_written = 0
        n_conform = 0
        n_uncalibrated = 0
        for m, _variant, t, n, bias in resolved:
            tn, nn = t.neutral, n.neutral
            if (
                tn is None or nn is None
                or tn.neutral_frac < _MIN_NEUTRAL_FRAC
                or nn.neutral_frac < _MIN_NEUTRAL_FRAC
            ):
                n_conform += 1  # cast not measurable → leave it alone
                continue
            # Excess cast of the Lr render (As Shot) vs in-camera, bias-corrected:
            # e = (N − T) + B; below the dead zone → photo matches, nothing to write.
            e_a = (nn.a_bias - tn.a_bias) + bias.cast_a
            e_b = (nn.b_bias - tn.b_bias) + bias.cast_b
            if (e_a * e_a + e_b * e_b) ** 0.5 < _WB_CAST_DEADBAND:
                n_conform += 1
                continue
            if (
                wbresp is None or not wbresp.is_calibrated()
                or m.neutral_asshot_temp is None
            ):
                n_uncalibrated += 1
                continue
            dtemp, dtint = wbresp.solve(e_a, e_b)
            temp = max(2000.0, min(12000.0, m.neutral_asshot_temp + max(-600.0, min(600.0, dtemp))))
            # Tint clamped to Lr's ±150 limits (Fable 5 review A-06), like Temperature.
            tint = max(-150.0, min(150.0, (m.neutral_asshot_tint or 0.0) + max(-10.0, min(10.0, dtint))))
            dev_by_id[m.photo_id].update(
                WhiteBalance="Custom",
                Temperature=_quantize.snap(temp, _TEMP_STEP),
                Tint=_quantize.snap(tint, _TINT_STEP),
            )
            n_written += 1
        note = f"wb: {n_written} corrected, {n_conform} matching (nothing written)"
        if n_uncalibrated:
            note += (
                f", {n_uncalibrated} deviant photo(s) NOT corrected — WB response not "
                f"calibrated (a render_probe sounding is needed)"
            )
        diag.notes.append(note)

    # ---- HSL: targets = T − bias, absolute values (HSL anchor = 0) --------
    if "hsl" in axes:
        n_written = 0
        for m, _variant, t, n, bias in resolved:
            tgs = _embedded_band_targets(t, bias, ignore_bias=True, raw_bands=m.raw_bands)
            deltas, _corrs = _hsl.plan_hsl(n.bands or [], tgs, model)
            wrote = False
            for key, d in deltas.items():
                # HSL anchor = 0 ⇒ the absolute value is the delta itself. The
                # dead zones in plan_band have already omitted matching bands.
                if d == 0:
                    continue
                dev_by_id[m.photo_id][key] = _quantize.snap(max(-100, min(100, d)), _HSL_STEP)
                wrote = True
            if wrote:
                n_written += 1
        diag.notes.append(
            f"hsl: {n_written}/{len(resolved)} deviant photo(s) adjusted "
            f"(matching keys omitted)"
        )

    # ---- Calibration: k-NN transplant (no measurable JPEG-side target) ----
    if "calib" in axes:
        if not seed_pool:
            diag.notes.append("calib: no usable seed — axis skipped")
        else:
            n_written = 0
            for m in targets:
                query = SeedVector(
                    photo_id=m.photo_id, asshot_rg=m.asshot_rg, asshot_bg=m.asshot_bg,
                    raw_median_l=m.raw_tone.median_l if m.raw_tone else None,
                    temperature=None, tint=None, preview_tone=None, preview_bands=None,
                    raw_bands=m.raw_bands, profile_capture=m.profile_capture, ev100=m.ev100,
                )
                t = seed_match.match_target(query, seed_pool)
                if t is None or not t.has_calibration():
                    continue
                dev_by_id[m.photo_id].update(_calib_develop_dict(t))
                n_written += 1
            diag.notes.append(
                f"calib: {n_written}/{len(targets)} photo(s) transplanted (k-NN seeds)"
            )

    adjustments = [
        PhotoAdjustment(photo_id=pid, develop=dev) for pid, dev in dev_by_id.items() if dev
    ]
    return adjustments, diag


# --------------------------------------------------------------------------- #
# Seeds mode — historical k-NN path (unchanged)
# --------------------------------------------------------------------------- #
def _plan_seeds(
    targets: list[PhotoMeasure],
    axes: frozenset[str],
    model: ResponseModel | None,
    seed_pool: list[SeedVector],
    dev_by_id: dict[str, dict],
    diag: PlanDiagnostics,
) -> tuple[list[PhotoAdjustment], PlanDiagnostics]:
    # Seeds mode measures the current state on the fresh render: `analysis` is required.
    usable = [m for m in targets if m.analysis is not None and m.analysis.tone is not None]
    if len(usable) < len(targets):
        diag.notes.append(f"{len(targets) - len(usable)} photo(s) with no current render → skipped")

    # Per-photo k-NN target, computed once and reused by the 3 axes.
    match_cache: dict[str, SeedTarget | None] = {}

    def _match(m: PhotoMeasure) -> SeedTarget | None:
        if m.photo_id not in match_cache:
            query = SeedVector(
                photo_id=m.photo_id,
                asshot_rg=m.asshot_rg,
                asshot_bg=m.asshot_bg,
                raw_median_l=m.raw_tone.median_l if m.raw_tone else None,
                temperature=None, tint=None, preview_tone=None, preview_bands=None,
                raw_bands=m.raw_bands, profile_capture=m.profile_capture, ev100=m.ev100,
            )
            match_cache[m.photo_id] = seed_match.match_target(query, seed_pool)
        return match_cache[m.photo_id]

    # ---- Exposure -----------------------------------------------------------
    if "expo" in axes:
        samples = []
        n_resolved = 0
        for m in usable:
            t = _match(m)
            desired = t.tone.median_l if (t and t.tone) else None
            if desired is not None:
                n_resolved += 1
            samples.append(
                _exp.ExposureSample(
                    m.photo_id, m.analysis.tone.median_l, _f(m.current_develop, "Exposure2012"),
                    desired_l=desired,
                    clipped_hi=m.analysis.tone.clipped_hi, clipped_lo=m.analysis.tone.clipped_lo,
                )
            )
        for adj in _exp.plan_from_render(samples, model.exposure if model else None):
            dev_by_id[adj.photo_id].update(adj.develop)
        diag.notes.append(f"expo: {n_resolved}/{len(usable)} target(s) resolved")

    # ---- White balance -------------------------------------------------------
    if "wb" in axes:
        n_wb = 0
        wbresp = model.wb if model else None
        for m in usable:
            t = _match(m)
            if t is None or t.temperature is None:
                continue
            temp = t.temperature
            tint = t.tint if t.tint is not None else 0.0
            # `neutral is not None` guard (Fable 5 review A-04): a RenderAnalysis
            # served from the cache may carry no NeutralStats — without the guard,
            # a single photo would fail the whole run (AttributeError).
            if wbresp is not None and m.analysis.neutral is not None:
                temp, tint, _ = _wb.refine_temp_tint(temp, tint, m.analysis.neutral, wbresp)
            temp = max(2000.0, min(12000.0, temp))  # Lr Temperature bounds (snap needs a clamped input)
            tint = max(-150.0, min(150.0, tint))  # Lr ±150 clamp (A-06)
            dev_by_id[m.photo_id].update(
                WhiteBalance="Custom",
                Temperature=_quantize.snap(temp, _TEMP_STEP),
                Tint=_quantize.snap(tint, _TINT_STEP),
            )
            n_wb += 1
        diag.notes.append(f"wb: {n_wb}/{len(usable)} photo(s) matched (k-NN seeds)")

    # ---- HSL -------------------------------------------------------------------
    if "hsl" in axes:
        n_hsl = 0
        for m in usable:
            tgs = _band_targets_from_seed_match(_match(m), raw_bands=m.raw_bands)
            deltas, _corrs = _hsl.plan_hsl(m.analysis.bands, tgs, model)
            for key, d in deltas.items():
                cur = _f(m.current_develop, key, 0.0)
                dev_by_id[m.photo_id][key] = _quantize.snap(max(-100, min(100, cur + d)), _HSL_STEP)
            if deltas:
                n_hsl += 1
        diag.notes.append(f"hsl: {n_hsl}/{len(usable)} photo(s) adjusted")

    # ---- Calibration: direct transplant from the k-NN target (like Temp/Tint) --
    if "calib" in axes:
        n_calib = 0
        for m in usable:
            t = _match(m)
            if t is None or not t.has_calibration():
                continue
            dev_by_id[m.photo_id].update(_calib_develop_dict(t))
            n_calib += 1
        diag.notes.append(f"calib: {n_calib}/{len(usable)} photo(s) transplanted (k-NN seeds)")

    # ---- Confidence gate (PLAN.md N3/N4) --------------------------------------
    # match_cache is populated by whichever axis loop(s) ran above, keyed by
    # photo_id — reused rather than re-matched.
    threshold = seed_match.pool_confidence_threshold(seed_pool)
    matched = [t for t in match_cache.values() if t is not None]
    n_low = sum(1 for t in matched if seed_match.is_low_confidence(t, threshold))
    diag.n_low_confidence = n_low
    if n_low:
        diag.notes.append(
            f"{n_low}/{len(matched)} photo(s): low-confidence match "
            f"(nearest seed further than the pool's usual spread)"
        )

    adjustments = [
        PhotoAdjustment(photo_id=pid, develop=dev) for pid, dev in dev_by_id.items() if dev
    ]
    return adjustments, diag
