"""Neutral anchor renders ("Neutral Preview") — Qt worker + reusable function.

Renders each photo in its **current style** (DCP profile + tone + Color Grading)
but with **As Shot WB**, **Exposure2012=0** and **all 24 HSL sliders at zero**,
then measures this neutral render (GPU, dual global+sharp) and caches it in
`NeutralPreviewJPEG`. HSL is neutralized so the anchor stays independent of
HSL corrections applied afterward (otherwise every HSL Apply would invalidate
`hash_style` → a full re-probe on every cycle).

Purpose: deterministic anchor for embedded mode — the delta between in-camera
JPEG and neutral render yields **absolute** (idempotent) settings, independent
of the current render.

Mechanism: plugin job `render_probe` (`Thumbnails.fetchProbe`: apply → render →
**restore**), submitted **in batches** (`chunk_size`) to keep the bridge
heartbeat alive and bound the window during which photos sit in a neutral
state. Freshness key: `hash_style` (see `cache.style_hash`) — recomputed only
when the style changes, not when Temp/Exposure/HSL move.

Stale-probe guard: if a photo has a current `Exposure2012` marked as non-zero
(>= 0.3) but its "neutral" anchor measures the same lightness as its last
known rendered preview, the probe likely served a cached preview (not a fresh
render) → single retry with a long settle, then **explicit failure** (a
suspect anchor is NEVER cached — it would poison every calculation until the
style changes).

The probe re-reads Temperature/Tint AFTER applying `WhiteBalance='As Shot'`:
that is the numeric As Shot value (cached as `wb_asshot_temp/tint`), the basis
for an absolute WB correction. This read effectively verifies the assumption
"applyDevelopSettings{WhiteBalance='As Shot'} resets Temp/Tint".
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QThread, Signal

from ..core import cache as cachemod, gpu, gpu_jpeg, render_metrics, render_metrics_gpu
from ..server.job_queue import job_queue
from ..server.models import JobType, PhotoResult, ThumbnailResult

_log = logging.getLogger("abelr.neutral_preview")

# Neutral render settings: as-shot WB + flat exposure + neutralized HSL,
# the rest of the style (DCP profile, tone, Color Grading, crop) untouched.
_NEUTRAL_DEVELOP: dict[str, object] = {
    "WhiteBalance": "As Shot",
    "Exposure2012": 0.0,
    **{
        f"{prefix}Adjustment{band}": 0
        for prefix in ("Hue", "Saturation", "Luminance")
        for band in render_metrics.BAND_NAMES
    },
}

# Time budget per photo (apply + settle + render + restore on the plugin side).
_SECONDS_PER_PHOTO = 4.0
_MIN_TIMEOUT = 30.0
# render_probe batch size: the plugin dispatch is synchronous inside pollOnce,
# a large batch would block the heartbeat for several minutes and widen the
# window during which photos stay in a neutral state if Lr crashes.
_CHUNK_SIZE = 16
# Delay given to Lr to regenerate the preview after the probe's apply (seconds);
# long settle used on retry if the anchor looks stale.
DEFAULT_SETTLE = 0.6
_RETRY_SETTLE = 2.0
# Stale-probe guard: current |Exposure2012| threshold above which the anchor
# MUST differ from the last known preview, and the L* gap below which it is
# considered suspect.
_SUSPECT_MIN_EXPO = 0.3
_SUSPECT_MAX_DELTA_L = 2.0


def _probe_chunk(
    chunk: list[PhotoResult], settle: float, timeout: float
) -> dict[str, tuple[ThumbnailResult, object]]:
    """Submits a render_probe job for `chunk`, decodes+measures the thumbnails (GPU).

    Returns {photo_id: (ThumbnailResult, RenderAnalysisDual)} — photos without a
    usable thumbnail are absent. Raises RuntimeError if the plugin doesn't respond.
    """
    adjustments = [
        {"photo_id": p.photo_id, "develop": dict(_NEUTRAL_DEVELOP)} for p in chunk
    ]
    job_id = job_queue.submit(
        JobType.RENDER_PROBE,
        {
            "adjustments": adjustments,
            "settle": settle,
            "width": render_metrics.MEASURE_LONG_EDGE,
            "height": render_metrics.MEASURE_LONG_EDGE,
        },
    )
    result = job_queue.wait_result(job_id, timeout)
    if result is None:
        raise RuntimeError(
            "Timeout — the Lr plugin did not return the neutral renders "
            "(is Lightroom open and the bridge connected?)."
        )
    out: dict[str, tuple[ThumbnailResult, object]] = {}
    for t in result.thumbnails:
        if not t.thumbnail_path:
            continue
        chw = gpu_jpeg.decode_file(t.thumbnail_path)
        if chw is None:
            continue
        out[t.photo_id] = (t, render_metrics_gpu.analyze_rendered_gpu_dual(chw))
    return out


def _anchor_suspect(p: PhotoResult, dual, conn) -> bool:
    """True if the "neutral" anchor looks like the last known rendered preview while
    the current Exposure2012 is far from 0 → the probe likely rendered something stale."""
    try:
        expo = abs(float((p.current_develop or {}).get("Exposure2012") or 0.0))
    except (TypeError, ValueError):
        return False
    if expo < _SUSPECT_MIN_EXPO or conn is None:
        return False
    if dual.sharp is None or dual.sharp.tone is None:
        return False
    try:
        prev = cachemod.get_preview_jpeg_latest(conn, p.photo_id)
    except Exception:
        # Do NOT swallow: without a cache read we can't clear the anchor, and a
        # cached suspect anchor poisons embedded mode until the style changes
        # (Fable 5 review B-03) → treat as suspect.
        _log.exception("cache read failed during _anchor_suspect (%s)", p.photo_id)
        return True
    if prev is None or prev.tone is None:
        return False
    return abs(dual.sharp.tone.median_l - prev.tone.median_l) < _SUSPECT_MAX_DELTA_L


def ensure_neutral_previews(
    photos: list[PhotoResult],
    conn,
    *,
    progress: Callable[[str], None] | None = None,
    progress_count: Callable[[int, int], None] | None = None,
    chunk_size: int = _CHUNK_SIZE,
    settle: float = DEFAULT_SETTLE,
) -> tuple[dict[str, dict], int]:
    """Ensures an up-to-date neutral render (`NeutralPreviewJPEG` cache) for each photo.

    Cache hits (`hash_style` up to date) are served without I/O; misses trigger
    plugin `render_probe` jobs in batches, decoded and measured on GPU. Returns
    `(by_id, n_refreshed)`: `by_id[uuid]` = `cache.get_neutral_preview` dict
    (sharp/glob/asshot_temp/asshot_tint/mask_sharp_frac), photos without a
    thumbnail are absent from the dict; `n_refreshed` = number of anchors
    recomputed via the plugin.

    Raises RuntimeError if the plugin doesn't respond or if an anchor stays
    suspect (stale probe) after retry — in that case nothing is cached for
    those photos.
    """
    say = progress or (lambda _msg: None)
    tick = progress_count or (lambda _done, _total: None)
    out: dict[str, dict] = {}
    todo: list[PhotoResult] = []
    style_by_id: dict[str, str] = {}
    for p in photos:
        hs = cachemod.style_hash(p.current_develop or {})
        style_by_id[p.photo_id] = hs
        cached = cachemod.get_neutral_preview(conn, p.photo_id, hs) if conn is not None else None
        if cached is not None:
            out[p.photo_id] = cached
        else:
            todo.append(p)

    n_refreshed = 0
    step = max(1, chunk_size)
    for start in range(0, len(todo), step):
        chunk = todo[start:start + step]
        say(
            f"Neutral render {min(start + len(chunk), len(todo))}/{len(todo)} "
            f"photo(s) in Lightroom…"
        )
        tick(start, len(todo))
        timeout = max(_MIN_TIMEOUT, _SECONDS_PER_PHOTO * len(chunk))
        got = _probe_chunk(chunk, settle, timeout)

        # Restore failed on the plugin side (Fable 5 review L-03): the photo stayed
        # in a NEUTRAL state in Lightroom — strong signal, must be shown, never silent.
        restore_failed = [t.photo_id[:8] for (t, _d) in got.values() if t.restore_error]
        if restore_failed:
            msg = (
                f"WARNING: restore failed for {len(restore_failed)} photo(s) — "
                f"left in a neutral state in Lr: {', '.join(restore_failed)}"
            )
            _log.error(msg)
            say(msg)

        # Stale-probe guard: single retry with a long settle, then hard failure.
        by_id = {p.photo_id: p for p in chunk}
        suspects = [
            by_id[pid] for pid, (_t, dual) in got.items()
            if _anchor_suspect(by_id[pid], dual, conn)
        ]
        if suspects:
            say(
                f"Suspect anchor(s) ({len(suspects)}) — re-rendering with a long "
                f"delay ({_RETRY_SETTLE:g}s)…"
            )
            retry_timeout = max(_MIN_TIMEOUT, (_SECONDS_PER_PHOTO + _RETRY_SETTLE) * len(suspects))
            got.update(_probe_chunk(suspects, _RETRY_SETTLE, retry_timeout))
            still = [
                p.photo_id[:8] for p in suspects
                if p.photo_id in got and _anchor_suspect(p, got[p.photo_id][1], conn)
            ]
            if still:
                raise RuntimeError(
                    "Neutral render still stale after retry (requestJpegThumbnail is "
                    f"serving a cache) for: {', '.join(still)}. Nothing was cached — "
                    "an LrExportSession fallback needs wiring on the plugin side if "
                    "this persists."
                )

        for p in chunk:
            hit = got.get(p.photo_id)
            if hit is None:
                continue
            t, dual = hit
            hs = style_by_id[p.photo_id]
            if conn is not None:
                try:
                    cachemod.put_neutral_preview(
                        conn, p.photo_id, hs,
                        sharp=dual.sharp, glob=dual.glob,
                        mask_sharp_frac=dual.mask_sharp_frac,
                        asshot_temp=t.asshot_temp, asshot_tint=t.asshot_tint,
                        commit=False,
                    )
                except Exception:
                    _log.exception("put_neutral_preview failed (%s)", p.photo_id)
            out[p.photo_id] = {
                "sharp": dual.sharp, "glob": dual.glob,
                "asshot_temp": t.asshot_temp, "asshot_tint": t.asshot_tint,
                "mask_sharp_frac": dual.mask_sharp_frac,
            }
            n_refreshed += 1
        if conn is not None:
            try:
                conn.commit()  # P-07: one commit per batch, not per photo
            except Exception:
                _log.exception("neutral batch commit failed")
        tick(min(start + len(chunk), len(todo)), len(todo))
    return out, n_refreshed


class NeutralPreviewWorker(QThread):
    """Generates/refreshes the neutral anchor renders for the selection (warm-up)."""

    finished_result = Signal(str)   # summary message
    progress = Signal(str)
    progress_count = Signal(int, int)  # (done, total) -> determinate progress bar
    failed = Signal(str)

    def __init__(self, photos: list[PhotoResult]) -> None:
        super().__init__()
        self._photos = photos

    def run(self) -> None:
        conn = None
        try:
            photos = self._photos
            if not photos:
                self.failed.emit("No photo selected.")
                return
            # GPU first, fallback to CPU (never a blocking failure — see core/gpu.py).
            if not gpu.is_available():
                self.progress.emit(
                    f"No GPU — analyzing on {gpu.device_name()} (slower)."
                )

            catalog_path = next((p.catalog_path for p in photos if p.catalog_path), None)
            conn = cachemod.open_cache(catalog_path) if catalog_path else None

            by_id, n_refreshed = ensure_neutral_previews(
                photos, conn, progress=self.progress.emit,
                progress_count=self.progress_count.emit,
            )
            n_missing = len(photos) - len(by_id)
            if n_refreshed == 0 and n_missing == 0:
                self.finished_result.emit(
                    f"Neutral renders already up to date ({len(photos)} photo(s), cache)."
                )
            else:
                self.finished_result.emit(
                    f"Neutral renders calibrated: {n_refreshed} recomputed, "
                    f"{len(by_id)}/{len(photos)} available"
                    + (f" ({n_missing} without a thumbnail)." if n_missing else ".")
                )
        except Exception as exc:  # safety net
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
