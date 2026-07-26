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
import threading
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QThread, Signal

from ..core import (
    cache as cachemod,
    catalog as catalogmod,
    gpu,
    gpu_jpeg,
    render_metrics,
    render_metrics_gpu,
)
from ..server import budget
from ..server.job_queue import job_queue
from ..server.models import JobType, PhotoResult, ThumbnailResult

_log = logging.getLogger("abelr.neutral_preview")

# Circuit breaker (N2c): this many CONSECUTIVE chunks with zero successes
# aborts the run instead of grinding through every remaining chunk — a dead
# bridge or a systematic plugin failure shows up in the first 2 chunks.
_CIRCUIT_BREAKER_CHUNKS = 2

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

# Job timeout is computed by app.server.budget (PLAN.md N3) — shared with the
# payload's "timeout_s" so Lua derives its own wait from the same number
# instead of a second, independently-tuned constant (that's how the original
# bug happened: THUMB_SECONDS_PER_PHOTO/PROBE_SECONDS_PER_PHOTO in Lua vs a
# local Python constant, silently drifting apart).
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


def _top_reasons(failures: dict[str, str], top_n: int = 3) -> str:
    """Formats the `top_n` most common distinct failure reasons with counts."""
    if not failures:
        return "no reason recorded"
    counts: dict[str, int] = {}
    for reason in failures.values():
        counts[reason] = counts.get(reason, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:top_n]
    return ", ".join(f"{reason} (x{n})" for reason, n in ranked)


def _probe_chunk(
    chunk: list[PhotoResult], settle: float, timeout: float
) -> tuple[dict[str, tuple[ThumbnailResult, object]], dict[str, str]]:
    """Submits a render_probe job for `chunk`, decodes+measures the thumbnails (GPU).

    Returns `(out, failures)`: `out[photo_id] = (ThumbnailResult, RenderAnalysisDual)`
    for photos with a usable render; `failures[photo_id] = reason` for every other
    photo in `chunk` (job-level error, missing thumbnail, or a failed JPEG decode).
    Raises RuntimeError if the plugin never responds at all (no result submitted).
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
            # Shipped so Lua derives its own wait from the same number (N3c)
            # instead of a second, independently-tuned constant.
            "timeout_s": timeout,
        },
    )
    result = job_queue.wait_result(job_id, timeout)
    if result is None:
        raise RuntimeError(
            "Timeout — the Lr plugin did not return the neutral renders "
            "(is Lightroom open and the bridge connected?)."
        )

    out: dict[str, tuple[ThumbnailResult, object]] = {}
    failures: dict[str, str] = {}

    if result.status != "ok":
        # Job-level failure (N2a: the plugin now tells the truth here) — every
        # photo in the chunk failed, there is nothing per-photo to decode.
        reason = result.error or result.errors_summary or "render_probe reported an error"
        for p in chunk:
            failures[p.photo_id] = reason
        return out, failures

    by_photo = {t.photo_id: t for t in result.thumbnails}
    for p in chunk:
        t = by_photo.get(p.photo_id)
        if t is None:
            failures[p.photo_id] = "no result for this photo"
            continue
        if not t.thumbnail_path:
            failures[p.photo_id] = t.error or "no thumbnail returned"
            continue
        chw = gpu_jpeg.decode_file(t.thumbnail_path)
        gpu_jpeg.cleanup_if_export(t.thumbnail_path, t.is_export)
        if chw is None:
            failures[p.photo_id] = "jpeg decode failed"
            continue
        undersized = render_metrics_gpu.reject_if_undersized(
            width=chw.shape[-1], height=chw.shape[-2]
        )
        if undersized is not None:
            failures[p.photo_id] = undersized
            continue
        out[p.photo_id] = (t, render_metrics_gpu.analyze_rendered_gpu_dual(chw))
    return out, failures


def _annotate_undersized(failures: dict[str, str], photos: list[PhotoResult]) -> None:
    """Appends the Catalog-Settings fix to every "undersized render" reason.

    `requestJpegThumbnail` never renders above the catalog's Standard Preview
    Size — it serves the largest tier it holds. On "Automatic" that cap sits
    below the measurement grid on any sub-4K display, so every probe comes back
    sub-grid and no amount of settle/retry changes it. No SDK call sets that
    setting (LrCatalog exposes nothing for it), so the only fix is the user's:
    the error has to name it, in place, rather than read as a plugin failure.
    """
    targets = [pid for pid, reason in failures.items() if "undersized render" in reason]
    if not targets:
        return
    catalog_path = next((p.catalog_path for p in photos if p.catalog_path), None)
    advice = catalogmod.preview_size_advice(catalog_path, render_metrics.MEASURE_LONG_EDGE)
    if not advice:
        return
    _log.warning("%s", advice)
    for pid in targets:
        if advice not in failures[pid]:
            failures[pid] = f"{failures[pid]} — {advice}"


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


@dataclass
class NeutralPreviewOutcome:
    """Result of `ensure_neutral_previews` (PLAN.md N2c — replaces a bare 2-tuple
    so failures are no longer discarded on the way back to the caller)."""

    by_id: dict[str, dict]
    n_refreshed: int
    n_requested: int
    failures: dict[str, str]
    cancelled: bool = False


def _summarize(outcome: NeutralPreviewOutcome, n_photos: int) -> tuple[bool, str]:
    """Decides failed vs. success and builds the summary message.

    Extracted out of `NeutralPreviewWorker.run` (N1b) so it's testable
    without Qt. `failed=True` when every requested anchor failed to refresh
    — previously this case still emitted a "calibrated" success message
    (fake success, N2c's reason for existing).
    """
    n_missing = n_photos - len(outcome.by_id)
    if outcome.cancelled:
        # Not a failure (PLAN.md U3) — the user asked to stop; whatever
        # refreshed before the cancel point is still cached and usable.
        return False, (
            f"Neutral render cancelled — {outcome.n_refreshed} recomputed, "
            f"{len(outcome.by_id)}/{n_photos} available before stopping."
        )
    if outcome.n_refreshed == 0 and outcome.n_requested > 0:
        return True, (
            f"Neutral render failed for all {outcome.n_requested} requested "
            f"photo(s): {_top_reasons(outcome.failures, 3)}"
        )
    if outcome.n_refreshed == 0 and n_missing == 0:
        return False, f"Neutral renders already up to date ({n_photos} photo(s), cache)."
    msg = (
        f"Neutral renders calibrated: {outcome.n_refreshed} recomputed, "
        f"{len(outcome.by_id)}/{n_photos} available"
    )
    if n_missing:
        msg += f" ({n_missing} failed: {_top_reasons(outcome.failures, 3)})."
    else:
        msg += "."
    return False, msg


def ensure_neutral_previews(
    photos: list[PhotoResult],
    conn,
    *,
    progress: Callable[[str], None] | None = None,
    progress_count: Callable[[int, int], None] | None = None,
    chunk_size: int = _CHUNK_SIZE,
    settle: float = DEFAULT_SETTLE,
    should_cancel: Callable[[], bool] | None = None,
) -> NeutralPreviewOutcome:
    """Ensures an up-to-date neutral render (`NeutralPreviewJPEG` cache) for each photo.

    Cache hits (`hash_style` up to date) are served without I/O; misses trigger
    plugin `render_probe` jobs in batches, decoded and measured on GPU.
    `outcome.by_id[uuid]` = `cache.get_neutral_preview` dict
    (sharp/glob/asshot_temp/asshot_tint/mask_sharp_frac), photos without a
    thumbnail are absent from the dict; `outcome.n_refreshed` = number of
    anchors recomputed via the plugin; `outcome.failures[uuid]` = reason for
    every requested photo that did NOT end up in `by_id`.

    `should_cancel` (PLAN.md U3): checked between chunks — a chunk already
    submitted always finishes (never abandons a photo mid render/restore),
    remaining chunks are skipped and `outcome.cancelled=True`. Never raises;
    a user-requested stop is not a failure.

    Raises RuntimeError if the plugin doesn't respond, if an anchor stays
    suspect (stale probe) after retry, or if the circuit breaker trips
    (`_CIRCUIT_BREAKER_CHUNKS` consecutive chunks with zero successes) — in
    all three cases, remaining chunks are never submitted.
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
    n_requested = len(todo)
    failures: dict[str, str] = {}
    consecutive_zero_success = 0
    cancelled = False
    step = max(1, chunk_size)
    for start in range(0, len(todo), step):
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        chunk = todo[start:start + step]
        say(
            f"Neutral render {min(start + len(chunk), len(todo))}/{len(todo)} "
            f"photo(s) in Lightroom…"
        )
        tick(start, len(todo))
        timeout = budget.job_timeout(
            len(chunk), render_metrics.MEASURE_LONG_EDGE, render_metrics.MEASURE_LONG_EDGE,
            settle, "probe",
        )
        got, chunk_failures = _probe_chunk(chunk, settle, timeout)
        _annotate_undersized(chunk_failures, chunk)
        failures.update(chunk_failures)
        by_id = {p.photo_id: p for p in chunk}

        # NO retry on a per-photo hard failure ("error loading thumb"): one was
        # added on 2026-07-26 and recovered 0/1 on every live attempt
        # (abelr_app.log 13:36-13:42) while costing a second full probe — a
        # photo Lr refuses to render is not a timing problem. It fails once,
        # with its reason, and the run continues.

        # Restore failed on the plugin side (Fable 5 review L-03): the photo stayed
        # in a NEUTRAL state in Lightroom — strong signal, must be shown, never silent.
        # Deliberately does NOT block caching the anchor (current behavior).
        restore_failed = [t.photo_id[:8] for (t, _d) in got.values() if t.restore_error]
        if restore_failed:
            msg = (
                f"WARNING: restore failed for {len(restore_failed)} photo(s) — "
                f"left in a neutral state in Lr: {', '.join(restore_failed)}"
            )
            _log.error(msg)
            say(msg)

        # Stale-probe guard: single retry with a long settle, then hard failure.
        suspects = [
            by_id[pid] for pid, (_t, dual) in got.items()
            if _anchor_suspect(by_id[pid], dual, conn)
        ]
        if suspects:
            say(
                f"Suspect anchor(s) ({len(suspects)}) — re-rendering with a long "
                f"delay ({_RETRY_SETTLE:g}s)…"
            )
            retry_timeout = budget.job_timeout(
                len(suspects), render_metrics.MEASURE_LONG_EDGE, render_metrics.MEASURE_LONG_EDGE,
                _RETRY_SETTLE, "probe",
            )
            retry_got, retry_failures = _probe_chunk(suspects, _RETRY_SETTLE, retry_timeout)
            got.update(retry_got)
            failures.update(retry_failures)
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

        chunk_written: list[str] = []
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
                    failures[p.photo_id] = "cache write failed"
                    continue
            out[p.photo_id] = {
                "sharp": dual.sharp, "glob": dual.glob,
                "asshot_temp": t.asshot_temp, "asshot_tint": t.asshot_tint,
                "mask_sharp_frac": dual.mask_sharp_frac,
            }
            n_refreshed += 1
            chunk_written.append(p.photo_id)
        if conn is not None and chunk_written:
            try:
                conn.commit()  # P-07: one commit per batch, not per photo
            except Exception:
                _log.exception("neutral batch commit failed")
                # The batch was never durably written — every photo just
                # counted as refreshed in this chunk is actually lost.
                for pid in chunk_written:
                    out.pop(pid, None)
                    failures[pid] = "cache commit failed"
                n_refreshed -= len(chunk_written)

        chunk_ok = sum(1 for p in chunk if p.photo_id in out)
        consecutive_zero_success = 0 if chunk_ok else consecutive_zero_success + 1
        tick(min(start + len(chunk), len(todo)), len(todo))
        if consecutive_zero_success >= _CIRCUIT_BREAKER_CHUNKS:
            done = min(start + len(chunk), len(todo))
            raise RuntimeError(
                f"Circuit breaker: {consecutive_zero_success} consecutive chunks "
                f"produced zero successes — aborting with {len(todo) - done} photo(s) "
                f"not attempted. Reasons: {_top_reasons(failures, 3)}"
            )
    return NeutralPreviewOutcome(
        by_id=out, n_refreshed=n_refreshed, n_requested=n_requested, failures=failures,
        cancelled=cancelled,
    )


class NeutralPreviewWorker(QThread):
    """Generates/refreshes the neutral anchor renders for the selection (warm-up)."""

    finished_result = Signal(str)   # summary message
    progress = Signal(str)
    progress_count = Signal(int, int)  # (done, total) -> determinate progress bar
    failed = Signal(str)

    def __init__(self, photos: list[PhotoResult]) -> None:
        super().__init__()
        self._photos = photos
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """PLAN.md U3 — cooperative: checked between chunks in
        ensure_neutral_previews, never kills the thread mid-chunk."""
        self._cancel_event.set()

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

            outcome = ensure_neutral_previews(
                photos, conn, progress=self.progress.emit,
                progress_count=self.progress_count.emit,
                should_cancel=self._cancel_event.is_set,
            )
            failed, msg = _summarize(outcome, len(photos))
            if failed:
                self.failed.emit(msg)
            else:
                self.finished_result.emit(msg)
        except Exception as exc:  # safety net
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
