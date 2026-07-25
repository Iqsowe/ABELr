"""PLAN step W1 — `render_probe` sweep to calibrate `core.response.WBResponse`.

`core.autocorrect._plan_embedded`'s WB axis (and `wb_model.refine_temp_tint` in
seeds mode) only ever writes a correction when `WBResponse.is_calibrated()` —
as long as no calibration exists (`response_cache/` empty, cf. PLAN.md
evidence), WB in embedded mode is a **guaranteed no-op**. This script probes
the actual local Jacobian ∂(a*, b*)/∂(Temperature, Tint): for a reference
photo **selected in Lightroom**, it anchors on the As Shot WB (numeric
Temperature/Tint read back after `WhiteBalance='As Shot'`, same mechanism as
`gui.neutral_preview_worker`), then applies known deltas one axis at a time
(`Temperature` around the As Shot value, `Tint` around 0) via `render_probe`
jobs, measures the residual cast on near-neutral pixels
(`render_metrics_gpu.analyze_rendered_gpu` → `NeutralStats.a_bias/b_bias`),
fits the local slope per axis (`core.response.fit_linear_response`) and saves
the `WBResponse` (`response.save`) for (camera, profile).

⚠️ **Lr required**: Lightroom must be open, the `ABELr` plugin active
(polling `/jobs/pending`), a reference photo selected in the catalog. The
reference photo should carry enough near-neutral content (skin, gray/white
surfaces, a color-neutral backdrop) for the a*/b* reading to be trustworthy —
probes with too few neutral pixels (`NeutralStats.neutral_frac` below
`_MIN_NEUTRAL_FRAC`) are skipped and logged, not silently included.
GPU-strict (`gpu.require_cuda`): no CPU fallback.

This script starts its own FastAPI server (like `app.main`, without the Qt
GUI) so the plugin can connect to it — do not run it at the same time as
`python -m app.main` (port 5000 conflict).

Usage:
    python -m app.tools.calibrate_wb_response
    python -m app.tools.calibrate_wb_response --temp-deltas -400,-200,0,200,400 --tint-deltas -15,-8,0,8,15
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core import exif_profile, gpu, gpu_jpeg, render_metrics_gpu, response  # noqa: E402
from app.core.response import WBResponse, fit_linear_response  # noqa: E402
from app.server.job_queue import job_queue  # noqa: E402
from app.server.models import JobType  # noqa: E402

_DEFAULT_TEMP_DELTAS = (-400.0, -200.0, 0.0, 200.0, 400.0)  # Kelvin, around the As Shot value
_DEFAULT_TINT_DELTAS = (-15.0, -8.0, 0.0, 8.0, 15.0)        # Lr Tint units, around 0
_BRIDGE_TIMEOUT_S = 60.0
_JOB_TIMEOUT_S = 30.0
# Below this fraction of near-neutral pixels, a probe's a*/b* reading is not
# trusted (cf. render_metrics._MIN_NEUTRAL_FRAC-style guard elsewhere).
_MIN_NEUTRAL_FRAC = 0.01


def _start_server() -> None:
    from app import main as app_main

    threading.Thread(target=app_main._run_server, daemon=True, name="fastapi").start()


def _wait_for_bridge(timeout_s: float) -> None:
    print("Waiting for the plugin bridge (Lightroom open, plugin active) …")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if job_queue.bridge_connected():
            print("Bridge connected.")
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"Bridge not connected after {timeout_s:.0f}s — Lightroom + active plugin required."
    )


def _pick_reference_photo(photo_id: str | None) -> dict:
    """Fetches the reference photo (current Lr selection) via `get_selected_photos`."""
    job_id = job_queue.submit(JobType.GET_SELECTED_PHOTOS)
    result = job_queue.wait_result(job_id, _JOB_TIMEOUT_S)
    if result is None or not result.photos:
        raise RuntimeError("No photo selected in Lightroom.")
    photos = result.photos
    if photo_id:
        match = next((p for p in photos if p.photo_id == photo_id), None)
        if match is None:
            raise RuntimeError(f"photo_id {photo_id!r} not in the current selection.")
        return match.model_dump()
    if len(photos) > 1:
        print(f"({len(photos)} photos selected — using the first: {photos[0].path})")
    return photos[0].model_dump()


def _probe_develop(photo_id: str, develop: dict, settle: float):
    """Submits one render_probe, returns the raw ThumbnailResult (or None on failure)."""
    job_id = job_queue.submit(
        JobType.RENDER_PROBE, {"adjustments": [{"photo_id": photo_id, "develop": develop}], "settle": settle}
    )
    result = job_queue.wait_result(job_id, _JOB_TIMEOUT_S)
    if result is None or not result.thumbnails:
        print(f"  [!] no render_probe response for {develop}")
        return None
    thumb = result.thumbnails[0]
    if thumb.restore_error:
        print(f"  [!] WARNING restore failed: {thumb.restore_error} — photo left in probed state.")
    if not thumb.thumbnail_path:
        print(f"  [!] thumbnail missing (error={thumb.error})")
        return None
    return thumb


def _read_asshot_temp_tint(photo_id: str, settle: float) -> tuple[float, float]:
    """Probes `WhiteBalance='As Shot'`, returns the numeric (Temperature, Tint)
    read back after the apply — same mechanism as `gui.neutral_preview_worker`.
    Falls back to (5500.0, 0.0) if the readback is unavailable (still lets the
    calibration proceed with a reasonable anchor rather than hard-failing)."""
    thumb = _probe_develop(photo_id, {"WhiteBalance": "As Shot"}, settle)
    if thumb is None or thumb.asshot_temp is None:
        print("  [!] could not read back As Shot Temperature — falling back to 5500K/0")
        return 5500.0, 0.0
    return float(thumb.asshot_temp), float(thumb.asshot_tint or 0.0)


def _measure_neutral(thumb) -> render_metrics_gpu.NeutralStats | None:
    chw = gpu_jpeg.decode_file(thumb.thumbnail_path)
    if chw is None:
        print("  [!] unreadable thumbnail")
        return None
    analysis = render_metrics_gpu.analyze_rendered_gpu(chw)
    neutral = analysis.neutral
    if neutral is None or neutral.neutral_frac < _MIN_NEUTRAL_FRAC:
        frac = neutral.neutral_frac if neutral else 0.0
        print(f"  [!] too few neutral pixels (frac={frac:.3f} < {_MIN_NEUTRAL_FRAC}) — skipped")
        return None
    return neutral


def calibrate_axis(
    photo_id: str, base_temp: float, base_tint: float, axis: str, deltas: list[float], settle: float
) -> tuple[float, float, int]:
    """Probes one axis ('Temperature' or 'Tint') around its base value.

    Returns (slope_a, slope_b, n) — slopes are **per raw delta unit** (Kelvin
    for Temperature, Lr units for Tint); the caller rescales to `WBResponse`'s
    documented convention (per +100K, per +1 Tint).
    """
    xs: list[float] = []
    ys_a: list[float] = []
    ys_b: list[float] = []
    for d in deltas:
        develop = {"WhiteBalance": "Custom", "Temperature": base_temp, "Tint": base_tint}
        develop[axis] = (base_temp if axis == "Temperature" else base_tint) + d
        thumb = _probe_develop(photo_id, develop, settle)
        if thumb is None:
            continue
        neutral = _measure_neutral(thumb)
        if neutral is None:
            continue
        xs.append(d)
        ys_a.append(neutral.a_bias)
        ys_b.append(neutral.b_bias)
    slope_a = fit_linear_response(xs, ys_a)
    slope_b = fit_linear_response(xs, ys_b)
    print(f"  {axis:<12} n={len(xs)}/{len(deltas)}  da/d{axis}={slope_a:+.5f}  db/d{axis}={slope_b:+.5f}")
    return slope_a, slope_b, len(xs)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--photo-id", default=None, help="specific photo_id (default: 1st selected photo)")
    ap.add_argument("--temp-deltas", default=",".join(str(d) for d in _DEFAULT_TEMP_DELTAS),
                     help="Kelvin deltas around the As Shot Temperature, comma-separated")
    ap.add_argument("--tint-deltas", default=",".join(str(d) for d in _DEFAULT_TINT_DELTAS),
                     help="Tint deltas around 0, comma-separated")
    ap.add_argument("--settle", type=float, default=0.6, help="Lr render delay (s) between apply and measurement")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    gpu.require_cuda()  # GPU-strict — no CPU fallback (CLAUDE.md).

    temp_deltas = [float(d) for d in a.temp_deltas.split(",")]
    tint_deltas = [float(d) for d in a.tint_deltas.split(",")]

    _start_server()
    _wait_for_bridge(_BRIDGE_TIMEOUT_S)

    photo = _pick_reference_photo(a.photo_id)
    photo_id = photo["photo_id"]
    camera = (photo.get("exif") or {}).get("camera")
    profile = exif_profile.read_capture_profiles([photo["path"]]).get(photo["path"])
    print(f"Reference photo: {photo['path']}  (camera={camera!r}, profile={profile!r})\n")

    print("Reading As Shot Temperature/Tint (calibration anchor) …")
    base_temp, base_tint = _read_asshot_temp_tint(photo_id, a.settle)
    print(f"  As Shot: Temperature={base_temp:.0f}K  Tint={base_tint:.1f}\n")

    print("Probing Temperature axis:")
    da_dtemp_raw, db_dtemp_raw, n_temp = calibrate_axis(
        photo_id, base_temp, base_tint, "Temperature", temp_deltas, a.settle
    )
    print("Probing Tint axis:")
    da_dtint, db_dtint, n_tint = calibrate_axis(
        photo_id, base_temp, base_tint, "Tint", tint_deltas, a.settle
    )

    if n_temp < 2 or n_tint < 2:
        raise RuntimeError(
            f"Not enough usable probes to fit a Jacobian (Temperature n={n_temp}, Tint n={n_tint}, "
            "need >=2 each) — check neutral_frac warnings above, pick a photo with more neutral content."
        )

    model = response.load(camera, profile)
    model.wb = WBResponse(
        da_dtemp=da_dtemp_raw * 100.0,  # raw slope is per-Kelvin -> convention is per +100K
        db_dtemp=db_dtemp_raw * 100.0,
        da_dtint=da_dtint,               # already per +1 Tint
        db_dtint=db_dtint,
    )
    path = response.save(model)
    print(f"\nWB response saved: {path}")
    print(f"  is_calibrated={model.wb.is_calibrated()}")


if __name__ == "__main__":
    main()
