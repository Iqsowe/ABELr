"""Calibrated ∂render/∂slider response — inverted model, cached by (camera, profile).

The Lr render chain is non-linear and depends on the DCP profile. We **don't
model it**: we **measure** how the render moves when a slider moves (`render_probe`
probing: apply a delta → re-render → measure in L*a*b* space), then we
**invert** to translate a measured gap into a slider delta.

Why per (camera, profile): the response `∂L*/∂EV`, `∂(a*,b*)/∂(Temp,Tint)` and
the HSL Jacobian depend on the profile. Calibrated **once** per profile on a
handful of probes (as the WB slope 2450 was), **cached to disk** → the
per-photo correction stays a single `apply`. Per-photo closed-loop correction
(re-measure) remains possible when high precision demands it.

This module is pure (model + fit + inversion + cache). Probe orchestration
(submitting `render_probe` jobs, reading thumbnails) lives in a worker/tool.

Warning: the **nominal** values below are *transparent physical priors*
(derived, not invented), used as long as no calibration exists. They must be
replaced by measurement (probing) — see validation scripts.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Disk cache of response models, key "camera|profile".
_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "response_cache"


# --------------------------------------------------------------------------- #
# Nominal exposure prior — transparent derivation (to refine via probing)
# --------------------------------------------------------------------------- #
# Exposure2012 ≈ scene-linear gain in stops before the curve. On an 18% mid-tone
# (lin 0.184 → L* 50), +1 EV doubles the linear value (0.368) → L* = 116·(0.368^(1/3)) − 16
# ≈ 67. Local slope ≈ 17 L*/EV near mid-tones. The slope DECREASES toward highlights
# (roll-off) and INCREASES in shadows; this scalar is only a median prior.
NOMINAL_DL_DEV = 17.0  # L* per stop, near mid-tones — a prior, not a profile truth.

# A probe sample near either clipping wall poisons slope_at (the curve
# saturates there) — reject before it enters ev/lstar.
MAX_CLIP_FRAC = 0.05


def clip_ok(clipped_hi: float, clipped_lo: float, max_frac: float = MAX_CLIP_FRAC) -> bool:
    """True if a probed sample's clipped fractions are low enough to trust."""
    return clipped_hi <= max_frac and clipped_lo <= max_frac


@dataclass
class ExposureResponse:
    """L* lightness ↔ Exposure2012 response, measured on a reference photo.

    `ev`/`lstar`: probed samples (applied EV deltas → measured rendered median
    L*), sorted by increasing EV. If empty → falls back to `NOMINAL_DL_DEV`.
    """

    ev: list[float] = field(default_factory=list)
    lstar: list[float] = field(default_factory=list)

    def _sorted(self) -> tuple[list[float], list[float]]:
        if not self.ev:
            return [], []
        pairs = sorted(zip(self.ev, self.lstar))
        return [p[0] for p in pairs], [p[1] for p in pairs]

    def slope_at(self, l_value: float) -> float:
        """Local slope ∂L*/∂EV at lightness `l_value` (finite difference on the probed curve).

        Falls back to `NOMINAL_DL_DEV` if fewer than 2 samples. Always ≥ a small
        positive value (lightness increases with EV) to avoid an unstable division.
        """
        ev, ls = self._sorted()
        if len(ev) < 2:
            return NOMINAL_DL_DEV
        # Curve segment whose L* interval contains l_value (else the nearest segment).
        best_slope = NOMINAL_DL_DEV
        best_dist = float("inf")
        for i in range(len(ev) - 1):
            dl = ls[i + 1] - ls[i]
            de = ev[i + 1] - ev[i]
            if de == 0:
                continue
            slope = dl / de
            lo, hi = min(ls[i], ls[i + 1]), max(ls[i], ls[i + 1])
            if lo <= l_value <= hi:
                return max(slope, 1.0)
            dist = min(abs(l_value - lo), abs(l_value - hi))
            if dist < best_dist:
                best_dist, best_slope = dist, slope
        return max(best_slope, 1.0)

    def solve_dev(self, current_l: float, target_l: float) -> float:
        """Exposure2012 delta to bring `current_l` toward `target_l` (local linearization).

        Slope taken at the midpoint of the [current_l, target_l] interval:
        robust to the curve's non-linearity (a single step; closed-loop
        refines further if needed).
        """
        slope = self.slope_at(0.5 * (current_l + target_l))
        return (target_l - current_l) / slope


@dataclass
class WBResponse:
    """Local 2×2 Jacobian ∂(a*, b*)/∂(Temp, Tint), measured by probing.

    da_dtemp, db_dtemp: a*/b* change per +100 K. da_dtint, db_dtint: per +1 Tint.
    All zero → not calibrated (the neutral WB refinement doesn't apply, the
    seed value is kept). No nominal prior: the magnitude depends too much on
    the profile to be guessed honestly.
    """

    da_dtemp: float = 0.0
    db_dtemp: float = 0.0
    da_dtint: float = 0.0
    db_dtint: float = 0.0

    def is_calibrated(self) -> bool:
        return any(abs(v) > 1e-9 for v in (self.da_dtemp, self.db_dtemp, self.da_dtint, self.db_dtint))

    def solve(self, a_bias: float, b_bias: float) -> tuple[float, float]:
        """(ΔTemp, ΔTint) to cancel a bias (a_bias, b_bias) measured on neutrals.

        Solves the 2×2 system J·[dTemp(/100), dTint] = -[a_bias, b_bias]. Returns
        (0, 0) if not calibrated or singular. ΔTemp in Kelvin, ΔTint in Lr units.
        """
        if not self.is_calibrated():
            return 0.0, 0.0
        det = self.da_dtemp * self.db_dtint - self.da_dtint * self.db_dtemp
        if abs(det) < 1e-12:
            return 0.0, 0.0
        # We want Δ(a,b) = -bias.
        ta, tb = -a_bias, -b_bias
        dtemp100 = (ta * self.db_dtint - self.da_dtint * tb) / det
        dtint = (self.da_dtemp * tb - ta * self.db_dtemp) / det
        return dtemp100 * 100.0, dtint


def fit_linear_response(deltas: Sequence[float], measured: Sequence[float]) -> float:
    """Slope ∂measured/∂slider_delta — linear regression (least squares, free
    intercept) over probe samples (known slider delta, measured render).

    Free intercept (not forced through the origin): absorbs a constant
    measurement offset between samples (render noise), since only the SLOPE
    matters to us (`BandResponse.dchroma_dsat` etc. — cf. `core.hsl`).
    Returns 0.0 if <2 samples or deltas without spread (slope not
    identifiable, e.g. all probed deltas identical) — the caller falls back
    to the nominal prior.
    """
    n = len(deltas)
    if n != len(measured) or n < 2:
        return 0.0
    mean_x = sum(deltas) / n
    mean_y = sum(measured) / n
    var_x = sum((x - mean_x) ** 2 for x in deltas)
    if var_x < 1e-9:
        return 0.0
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(deltas, measured))
    return cov_xy / var_x


@dataclass
class BandResponse:
    """Local response of an HSL band (≈ diagonal: Sat→chroma, Lum→L*, Hue→hue).

    dchroma_dsat: ΔC* (CIELAB) per +1 of SaturationAdjustment<band>.
    dl_dlum      : ΔL* per +1 of LuminanceAdjustment<band>.
    dhue_dhue    : Δhue (deg) per +1 of HueAdjustment<band>.
    0 → not calibrated for this axis (the corresponding slider isn't emitted).
    """

    dchroma_dsat: float = 0.0
    dl_dlum: float = 0.0
    dhue_dhue: float = 0.0

    def is_calibrated(self) -> bool:
        return any(abs(v) > 1e-9 for v in (self.dchroma_dsat, self.dl_dlum, self.dhue_dhue))


@dataclass
class ResponseModel:
    """Complete response model for a (camera, profile) pair."""

    camera: str
    profile: str
    exposure: ExposureResponse = field(default_factory=ExposureResponse)
    wb: WBResponse = field(default_factory=WBResponse)
    bands: dict[str, BandResponse] = field(default_factory=dict)

    def band(self, name: str) -> BandResponse:
        return self.bands.get(name, BandResponse())


# --------------------------------------------------------------------------- #
# Disk cache (JSON) — key "camera|profile"
# --------------------------------------------------------------------------- #
def _key(camera: str | None, profile: str | None) -> str:
    cam = (camera or "unknown").replace("|", "_")
    prof = (profile or "unknown").replace("|", "_")
    return f"{cam}|{prof}"


def _cache_file(camera: str | None, profile: str | None) -> Path:
    safe = _key(camera, profile).replace("|", "__").replace("/", "_").replace(" ", "_")
    return _CACHE_DIR / f"{safe}.json"


def load(camera: str | None, profile: str | None) -> ResponseModel:
    """Loads the cached model for (camera, profile), or an empty model (nominal priors).

    A corrupted/truncated cache JSON falls back to the empty model (Fable 5
    review A-05): disposable data should never make the whole analysis fail.
    """
    f = _cache_file(camera, profile)
    if not f.is_file():
        return ResponseModel(camera=camera or "unknown", profile=profile or "unknown")
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return ResponseModel(
            camera=data.get("camera", camera or "unknown"),
            profile=data.get("profile", profile or "unknown"),
            exposure=ExposureResponse(**data.get("exposure", {})),
            wb=WBResponse(**data.get("wb", {})),
            bands={k: BandResponse(**v) for k, v in data.get("bands", {}).items()},
        )
    except Exception:
        logging.getLogger("abelr.response").exception(
            "unreadable response model (%s) — falling back to priors", f
        )
        return ResponseModel(camera=camera or "unknown", profile=profile or "unknown")


def save(model: ResponseModel) -> Path:
    """Persists a response model to disk (JSON)."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = _cache_file(model.camera, model.profile)
    payload = {
        "camera": model.camera,
        "profile": model.profile,
        "exposure": asdict(model.exposure),
        "wb": asdict(model.wb),
        "bands": {k: asdict(v) for k, v in model.bands.items()},
    }
    f.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return f
