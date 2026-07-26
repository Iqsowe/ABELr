"""CAL1/CAL2 — HSL/WB response calibration driven over MCP instead of
`job_queue` in-process (PLAN.md CAL section, same pattern as OLD_PLAN.md W2).

`calibrate_hsl_response.py`/`calibrate_wb_response.py` start their own FastAPI
server, which conflicts with an already-running `python -m app.main` on port
5000. This script drives the *same* math (`response.fit_linear_response`,
`response.save`) through the MCP client SDK against the live App's `/mcp`
endpoint instead — the App stays untouched, only the job-submission transport
differs. Reference photo is passed explicitly (`--photo-id`), not read from
Lr's current GUI selection: `render_probe` resolves photos by UUID
independent of Lr selection (`Thumbnails.fetchProbe` -> `findPhotoByUuid`).

⚠️ Lr required: Lightroom open, ABELr plugin connected, `python -m app.main`
already running (this script is the MCP *client*, not a second server).
GPU-strict (`gpu.require_cuda`): no CPU fallback, same as the two scripts
this supersedes for the port-5000-conflict case.

Usage:
    python -m app.tools.mcp_calibrate hsl --photo-id <uuid> --camera ILCE-7M4 --profile Neutral --bands Green
    python -m app.tools.mcp_calibrate wb  --photo-id <uuid> --camera ILCE-7M4 --profile Neutral
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from app.core import gpu, gpu_jpeg, render_metrics, render_metrics_gpu, response  # noqa: E402
from app.core.response import BandResponse, ExposureResponse, WBResponse, fit_linear_response  # noqa: E402

_MCP_URL = "http://127.0.0.1:5000/mcp"
_AXES = {"Saturation": "median_chroma", "Luminance": "median_l", "Hue": "median_hue"}
_DEFAULT_HSL_DELTAS = (-15.0, -8.0, 0.0, 8.0, 15.0)
_DEFAULT_TEMP_DELTAS = (-400.0, -200.0, 0.0, 200.0, 400.0)
_DEFAULT_TINT_DELTAS = (-15.0, -8.0, 0.0, 8.0, 15.0)
_DEFAULT_EXPOSURE_DELTAS = (-2.0, -1.0, -0.5, 0.5, 1.0, 2.0)
_MIN_NEUTRAL_FRAC = 0.01


_REFERENCE_HEADROOM_FRAC = 0.01  # stricter than response.clip_ok's per-sample 0.05:
# the reference must have HEADROOM for a +-2EV probe, not just pass at EV0.


def _pick_exposure_reference(db_path: Path, headroom: float = _REFERENCE_HEADROOM_FRAC) -> str:
    """Scans cached `InCameraJPEG.tone_sharp` for the uuid whose sharp-zone
    median L* sits closest to 50 (mid-tone) with near-zero clipping —
    evidence-based pick, same pattern CAL1/CAL2 used for HSL/WB (PLAN.md X1)."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        best_uuid, best_score = None, float("inf")
        rows = conn.execute("SELECT uuid, tone_sharp FROM InCameraJPEG WHERE tone_sharp IS NOT NULL")
        for uuid, tone_json in rows:
            tone = json.loads(tone_json)
            if not response.clip_ok(tone["clipped_hi"], tone["clipped_lo"], max_frac=headroom):
                continue
            # Headroom for a +-2EV probe matters more than an exact mid-tone
            # hit: weight clip margin over |median_l-50|.
            score = 20.0 * (tone["clipped_hi"] + tone["clipped_lo"]) + abs(tone["median_l"] - 50.0)
            if score < best_score:
                best_score, best_uuid = score, uuid
        if best_uuid is None:
            raise RuntimeError(
                f"no InCameraJPEG.tone_sharp row with clip <= {headroom} (all clipped, or empty cache)"
            )
        return best_uuid
    finally:
        conn.close()


class McpProbe:
    """Thin wrapper: one persistent MCP session, `render_probe` calls."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def probe(self, photo_id: str, develop: dict, settle: float) -> dict | None:
        result = await self._session.call_tool(
            "render_probe",
            {
                "adjustments": [{"photo_id": photo_id, "develop": develop}], "settle": settle,
                # PLAN.md X3: explicit, though the server tool now defaults to
                # the same value — avoids silently drifting back to Lua's 512
                # legacy fallback if the server's own default ever changes.
                "width": render_metrics.MEASURE_LONG_EDGE,
                "height": render_metrics.MEASURE_LONG_EDGE,
            },
        )
        if result.isError:
            print(f"  [!] render_probe error for {develop}: {result.content}")
            return None
        data = result.structuredContent
        if not data and result.content:
            # fastmcp doesn't structure plain-dict returns -> parse the text block.
            data = json.loads(result.content[0].text)
        if not data or not data.get("thumbnails"):
            print(f"  [!] no thumbnails for {develop}")
            return None
        thumb = data["thumbnails"][0]
        if thumb.get("restore_error"):
            print(f"  [!] WARNING restore failed: {thumb['restore_error']} — photo left probed.")
        if not thumb.get("thumbnail_path"):
            print(f"  [!] thumbnail missing (error={thumb.get('error')})")
            return None
        return thumb


def _hue_unwrap(hue: float, ref: float) -> float:
    return ref + ((hue - ref + 180.0) % 360.0 - 180.0)


async def _calibrate_band_axis(
    probe: McpProbe, photo_id: str, band_name: str, axis: str,
    current_val: float, deltas: list[float], settle: float,
) -> float:
    key = f"{axis}Adjustment{band_name}"
    field = _AXES[axis]
    xs: list[float] = []
    ys: list[float] = []
    ref_hue: float | None = None
    for d in deltas:
        thumb = await probe.probe(photo_id, {key: current_val + d}, settle)
        if thumb is None:
            continue
        chw = gpu_jpeg.decode_file(thumb["thumbnail_path"])
        gpu_jpeg.cleanup_if_export(thumb["thumbnail_path"], thumb.get("is_export", False))
        if chw is None:
            print("  [!] unreadable thumbnail")
            continue
        undersized = render_metrics_gpu.reject_if_undersized(width=chw.shape[-1], height=chw.shape[-2])
        if undersized is not None:
            print(f"  [!] {undersized}")
            continue
        analysis = render_metrics_gpu.analyze_rendered_gpu(chw)
        band = next((b for b in analysis.bands if b.name == band_name), None) if analysis.bands else None
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
    print(f"  {key:<28} n={len(xs)}/{len(deltas)}  slope={slope:+.4f}")
    return slope


async def _measure_neutral(probe: McpProbe, photo_id: str, develop: dict, settle: float):
    thumb = await probe.probe(photo_id, develop, settle)
    if thumb is None:
        return None
    chw = gpu_jpeg.decode_file(thumb["thumbnail_path"])
    gpu_jpeg.cleanup_if_export(thumb["thumbnail_path"], thumb.get("is_export", False))
    if chw is None:
        print("  [!] unreadable thumbnail")
        return None
    undersized = render_metrics_gpu.reject_if_undersized(width=chw.shape[-1], height=chw.shape[-2])
    if undersized is not None:
        print(f"  [!] {undersized}")
        return None
    analysis = render_metrics_gpu.analyze_rendered_gpu(chw)
    neutral = analysis.neutral
    if neutral is None or neutral.neutral_frac < _MIN_NEUTRAL_FRAC:
        frac = neutral.neutral_frac if neutral else 0.0
        print(f"  [!] too few neutral pixels (frac={frac:.3f} < {_MIN_NEUTRAL_FRAC}) — skipped")
        return None
    return neutral, thumb


async def _calibrate_wb_axis(
    probe: McpProbe, photo_id: str, base_temp: float, base_tint: float,
    axis: str, deltas: list[float], settle: float,
) -> tuple[float, float, int]:
    xs: list[float] = []
    ys_a: list[float] = []
    ys_b: list[float] = []
    for d in deltas:
        develop = {"WhiteBalance": "Custom", "Temperature": base_temp, "Tint": base_tint}
        develop[axis] = (base_temp if axis == "Temperature" else base_tint) + d
        out = await _measure_neutral(probe, photo_id, develop, settle)
        if out is None:
            continue
        neutral, _ = out
        xs.append(d)
        ys_a.append(neutral.a_bias)
        ys_b.append(neutral.b_bias)
    slope_a = fit_linear_response(xs, ys_a)
    slope_b = fit_linear_response(xs, ys_b)
    print(f"  {axis:<12} n={len(xs)}/{len(deltas)}  da/d{axis}={slope_a:+.5f}  db/d{axis}={slope_b:+.5f}")
    return slope_a, slope_b, len(xs)


async def _calibrate_exposure_axis(
    probe: McpProbe, photo_id: str, deltas: list[float], settle: float,
) -> tuple[list[float], list[float]]:
    # Sharp-zone L*, matching the consumer (`autocorrect` solves on
    # `sharp.tone.median_l`, PLAN.md X1) — not the global scope.
    xs: list[float] = []
    ys: list[float] = []
    for d in deltas:
        thumb = await probe.probe(photo_id, {"Exposure2012": d}, settle)
        if thumb is None:
            continue
        chw = gpu_jpeg.decode_file(thumb["thumbnail_path"])
        gpu_jpeg.cleanup_if_export(thumb["thumbnail_path"], thumb.get("is_export", False))
        if chw is None:
            print("  [!] unreadable thumbnail")
            continue
        undersized = render_metrics_gpu.reject_if_undersized(width=chw.shape[-1], height=chw.shape[-2])
        if undersized is not None:
            print(f"  [!] {undersized}")
            continue
        tone = render_metrics_gpu.analyze_rendered_gpu_dual(chw).sharp.tone
        if not response.clip_ok(tone.clipped_hi, tone.clipped_lo):
            print(f"  [!] EV{d:+g}: clipped (hi={tone.clipped_hi:.3f} lo={tone.clipped_lo:.3f}) — rejected")
            continue
        xs.append(d)
        ys.append(tone.median_l)
    print(f"  Exposure2012  n={len(xs)}/{len(deltas)}")
    return xs, ys


async def _run_exposure(args) -> None:
    async with streamablehttp_client(_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            probe = McpProbe(session)
            xs, ys = await _calibrate_exposure_axis(probe, args.photo_id, args.deltas, args.settle)
            if len(xs) < 2:
                raise RuntimeError(f"Not enough usable probes (n={len(xs)}, need >=2).")
            model = response.load(args.camera, args.profile)
            model.exposure = ExposureResponse(ev=xs, lstar=ys)
            path = response.save(model)
            print(f"\nExposure response saved: {path}")
            for d, l_val in zip(xs, ys):
                print(f"  EV{d:+g} -> L*{l_val:.2f}")


async def _run_hsl(args) -> None:
    async with streamablehttp_client(_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            probe = McpProbe(session)
            model = response.load(args.camera, args.profile)
            for band_name in args.bands:
                print(f"Band {band_name}:")
                for axis in ("Saturation", "Luminance", "Hue"):
                    slope = await _calibrate_band_axis(
                        probe, args.photo_id, band_name, axis, 0.0, args.deltas, args.settle
                    )
                    prev = model.bands.get(band_name, BandResponse())
                    model.bands[band_name] = BandResponse(
                        dchroma_dsat=slope if axis == "Saturation" else prev.dchroma_dsat,
                        dl_dlum=slope if axis == "Luminance" else prev.dl_dlum,
                        dhue_dhue=slope if axis == "Hue" else prev.dhue_dhue,
                    )
            path = response.save(model)
            print(f"\nResponse model saved: {path}")


async def _run_wb(args) -> None:
    async with streamablehttp_client(_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            probe = McpProbe(session)
            print("Reading As Shot Temperature/Tint (calibration anchor) …")
            thumb = await probe.probe(args.photo_id, {"WhiteBalance": "As Shot"}, args.settle)
            if thumb is None or thumb.get("asshot_temp") is None:
                print("  [!] could not read back As Shot Temperature — falling back to 5500K/0")
                base_temp, base_tint = 5500.0, 0.0
            else:
                base_temp = float(thumb["asshot_temp"])
                base_tint = float(thumb.get("asshot_tint") or 0.0)
            print(f"  As Shot: Temperature={base_temp:.0f}K  Tint={base_tint:.1f}\n")

            print("Probing Temperature axis:")
            da_dtemp_raw, db_dtemp_raw, n_temp = await _calibrate_wb_axis(
                probe, args.photo_id, base_temp, base_tint, "Temperature", args.temp_deltas, args.settle
            )
            print("Probing Tint axis:")
            da_dtint, db_dtint, n_tint = await _calibrate_wb_axis(
                probe, args.photo_id, base_temp, base_tint, "Tint", args.tint_deltas, args.settle
            )
            if n_temp < 2 or n_tint < 2:
                raise RuntimeError(
                    f"Not enough usable probes (Temperature n={n_temp}, Tint n={n_tint}, need >=2 each)."
                )
            model = response.load(args.camera, args.profile)
            model.wb = WBResponse(
                da_dtemp=da_dtemp_raw * 100.0,
                db_dtemp=db_dtemp_raw * 100.0,
                da_dtint=da_dtint,
                db_dtint=db_dtint,
            )
            path = response.save(model)
            print(f"\nWB response saved: {path}")
            print(f"  is_calibrated={model.wb.is_calibrated()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)

    hsl = sub.add_parser("hsl")
    hsl.add_argument("--photo-id", required=True)
    hsl.add_argument("--camera", required=True)
    hsl.add_argument("--profile", required=True)
    hsl.add_argument("--bands", required=True, help="comma-separated band names")
    hsl.add_argument("--deltas", default=",".join(str(d) for d in _DEFAULT_HSL_DELTAS))
    hsl.add_argument("--settle", type=float, default=0.6)

    wb = sub.add_parser("wb")
    wb.add_argument("--photo-id", required=True)
    wb.add_argument("--camera", required=True)
    wb.add_argument("--profile", required=True)
    wb.add_argument("--temp-deltas", default=",".join(str(d) for d in _DEFAULT_TEMP_DELTAS))
    wb.add_argument("--tint-deltas", default=",".join(str(d) for d in _DEFAULT_TINT_DELTAS))
    wb.add_argument("--settle", type=float, default=0.6)

    exp = sub.add_parser("exposure")
    exp.add_argument("--photo-id", default=None, help="explicit uuid (default: auto-pick, needs --catalog)")
    exp.add_argument("--catalog", default=None, help="catalog folder or ABELr_cache.db path (for auto-pick)")
    exp.add_argument("--camera", required=True)
    exp.add_argument("--profile", required=True)
    exp.add_argument("--deltas", default=",".join(str(d) for d in _DEFAULT_EXPOSURE_DELTAS))
    exp.add_argument("--settle", type=float, default=0.6)

    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    gpu.require_cuda()  # GPU-strict — no CPU fallback (CLAUDE.md).

    if a.mode == "hsl":
        a.bands = [b.strip() for b in a.bands.split(",") if b.strip()]
        a.deltas = [float(d) for d in a.deltas.split(",")]
        asyncio.run(_run_hsl(a))
    elif a.mode == "wb":
        a.temp_deltas = [float(d) for d in a.temp_deltas.split(",")]
        a.tint_deltas = [float(d) for d in a.tint_deltas.split(",")]
        asyncio.run(_run_wb(a))
    else:
        if not a.photo_id:
            if not a.catalog:
                raise SystemExit("exposure mode needs --photo-id or --catalog (for auto-pick)")
            db_path = Path(a.catalog)
            from app.core import cache as _cache
            if db_path.is_dir():
                db_path = db_path / _cache.CACHE_FILENAME
            a.photo_id = _pick_exposure_reference(db_path)
            print(f"Auto-picked reference photo: {a.photo_id}")
        a.deltas = [float(d) for d in a.deltas.split(",")]
        asyncio.run(_run_exposure(a))


if __name__ == "__main__":
    main()
