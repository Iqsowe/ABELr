"""PLAN step H2 — `render_probe` sweep to calibrate `core.response.BandResponse`.

`core.hsl.plan_band` falls back to heuristic nominal gains
(`_NOM_DCHROMA_DSAT` etc.) as long as no measured response exists
(`response.save` never called — see PLAN.md step H2). This script probes the
actual slope ∂render/∂slider: for a reference photo **selected in
Lightroom**, it applies known deltas of `SaturationAdjustment<Band>` /
`LuminanceAdjustment<Band>` / `HueAdjustment<Band>` (one axis at a time,
around the slider's current value) via `render_probe` jobs, measures the
render (`render_metrics_gpu.analyze_rendered_gpu`), fits the local slope
(`core.response.fit_linear_response`) and saves the `ResponseModel`
(`response.save`) for (camera, profile).

⚠️ **Lr required**: Lightroom must be open, the `ABELr` plugin active
(polling `/jobs/pending`), a reference photo selected in the catalog.
GPU-strict (`gpu.require_cuda`): no CPU fallback.

This script starts its own FastAPI server (like `app.main`, without the Qt
GUI) so the plugin can connect to it — do not run it at the same time as
`python -m app.main` (port 5000 conflict).

Usage:
    python -m app.tools.calibrate_hsl_response
    python -m app.tools.calibrate_hsl_response --bands Red,Orange --deltas -15,-8,0,8,15
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core import exif_profile, gpu, gpu_jpeg, render_metrics, render_metrics_gpu, response  # noqa: E402
from app.core.response import BandResponse, fit_linear_response  # noqa: E402
from app.server.job_queue import job_queue  # noqa: E402
from app.server.models import JobType  # noqa: E402

_AXES = {
    "Saturation": "median_chroma",
    "Luminance": "median_l",
    "Hue": "median_hue",
}
_DEFAULT_DELTAS = (-15.0, -8.0, 0.0, 8.0, 15.0)
_BRIDGE_TIMEOUT_S = 60.0
_JOB_TIMEOUT_S = 30.0


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


def _probe_once(photo_id: str, develop: dict, settle: float) -> render_metrics.BandStats | None:
    """Probes a single temporary setting, returns the measured band stats (all bands)."""
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
    chw = gpu_jpeg.decode_file(thumb.thumbnail_path)
    if chw is None:
        print("  [!] unreadable thumbnail")
        return None
    undersized = render_metrics_gpu.reject_if_undersized(width=chw.shape[-1], height=chw.shape[-2])
    if undersized is not None:
        print(f"  [!] {undersized}")
        return None
    analysis = render_metrics_gpu.analyze_rendered_gpu(chw)
    return analysis.bands


def _hue_unwrap(hue: float, ref: float) -> float:
    """Hue wrapped into (ref−180, ref+180] — same conventions as `hsl._hue_diff`."""
    return ref + ((hue - ref + 180.0) % 360.0 - 180.0)


def calibrate_band_axis(
    photo_id: str, band_name: str, axis: str, current_val: float, deltas: list[float], settle: float
) -> float:
    """Probes one axis (Saturation/Luminance/Hue) of a band, returns the fitted slope."""
    key = f"{axis}Adjustment{band_name}"
    field = _AXES[axis]
    xs: list[float] = []
    ys: list[float] = []
    ref_hue: float | None = None
    for d in deltas:
        bands = _probe_once(photo_id, {key: current_val + d}, settle)
        if bands is None:
            continue
        band = next((b for b in bands if b.name == band_name), None)
        if band is None or not render_metrics.band_is_reliable(band):
            print(f"  [!] band {band_name} unreliable/missing for delta {d:+g} — skipped")
            continue
        value = getattr(band, field)
        if axis == "Hue":
            if ref_hue is None:
                ref_hue = value
            value = _hue_unwrap(value, ref_hue)
        xs.append(d)
        ys.append(value)
    slope = fit_linear_response(xs, ys)
    print(f"  {key:<28} n={len(xs)}  slope={slope:+.4f}")
    return slope


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--photo-id", default=None, help="specific photo_id (default: 1st selected photo)")
    ap.add_argument("--bands", default=",".join(render_metrics.BAND_NAMES),
                     help="bands to probe, comma-separated")
    ap.add_argument("--deltas", default=",".join(str(d) for d in _DEFAULT_DELTAS),
                     help="slider deltas to probe, comma-separated")
    ap.add_argument("--settle", type=float, default=0.6, help="Lr render delay (s) between apply and measurement")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    gpu.require_cuda()  # GPU-strict — no CPU fallback (CLAUDE.md).

    bands = [b.strip() for b in a.bands.split(",") if b.strip()]
    deltas = [float(d) for d in a.deltas.split(",")]

    _start_server()
    _wait_for_bridge(_BRIDGE_TIMEOUT_S)

    photo = _pick_reference_photo(a.photo_id)
    photo_id = photo["photo_id"]
    current_develop = photo.get("current_develop") or {}
    camera = (photo.get("exif") or {}).get("camera")
    profile = exif_profile.read_capture_profiles([photo["path"]]).get(photo["path"])
    print(f"Reference photo: {photo['path']}  (camera={camera!r}, profile={profile!r})\n")

    model = response.load(camera, profile)
    for band_name in bands:
        print(f"Band {band_name}:")
        for axis in ("Saturation", "Luminance", "Hue"):
            key = f"{axis}Adjustment{band_name}"
            current_val = float(current_develop.get(key) or 0.0)
            slope = calibrate_band_axis(photo_id, band_name, axis, current_val, deltas, a.settle)
            prev = model.bands.get(band_name, BandResponse())
            model.bands[band_name] = BandResponse(
                dchroma_dsat=slope if axis == "Saturation" else prev.dchroma_dsat,
                dl_dlum=slope if axis == "Luminance" else prev.dl_dlum,
                dhue_dhue=slope if axis == "Hue" else prev.dhue_dhue,
            )

    path = response.save(model)
    print(f"\nResponse model saved: {path}")


if __name__ == "__main__":
    main()
