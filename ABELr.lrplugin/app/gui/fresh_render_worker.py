"""Fresh render fetch (`get_thumbnails`), chunked — same pattern as
`neutral_preview_worker`'s `render_probe` batching, applied to the plain
fresh-render request used before every measurement/plan/apply pass.

Unlike `render_probe` (apply → render → restore, a write/restore window that
must stay short), `get_thumbnails` is read-only (parallel
`requestJpegThumbnail` callbacks on the plugin side) — so chunking here is
only about bounding a single job's blocking time inside the plugin's
`pollOnce` dispatch and giving per-chunk progress/failure isolation, not
about limiting how long photos sit in a mutated state.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QThread, Signal

from ..core import catalog
from ..server import budget
from ..server.job_queue import job_queue
from ..server.models import JobType, PhotoResult, ThumbnailResult

_log = logging.getLogger("abelr.fresh_render")

# Chunk size: larger than render_probe's _CHUNK_SIZE=16 since there's no
# write/restore risk window to bound here, just the request's own timeout.
_CHUNK_SIZE = 40


def fetch_thumbnails_chunked(
    photo_ids: list[str],
    width: int,
    height: int,
    *,
    progress: Callable[[str], None] | None = None,
    progress_count: Callable[[int, int], None] | None = None,
    chunk_size: int = _CHUNK_SIZE,
    catalog_path: str | None = None,
) -> dict[str, ThumbnailResult]:
    """Fetches fresh-render thumbnails for `photo_ids` via small `get_thumbnails` jobs.

    Returns `{photo_id: ThumbnailResult}`. A chunk that times out is logged and
    skipped — its photos are simply absent from the result (same degraded,
    non-blocking fallback contract the caller already applies to a single
    failed `get_thumbnails` job), rather than losing the whole selection.
    """
    say = progress or (lambda _msg: None)
    tick = progress_count or (lambda _done, _total: None)
    out: dict[str, ThumbnailResult] = {}
    total = len(photo_ids)
    step = max(1, chunk_size)
    for start in range(0, total, step):
        chunk = photo_ids[start:start + step]
        say(f"Fresh render {min(start + len(chunk), total)}/{total} photo(s) in Lightroom…")
        timeout = budget.job_timeout(len(chunk), width, height, 0.0, "fetch")
        job_id = job_queue.submit(
            JobType.GET_THUMBNAILS,
            {
                "photo_ids": chunk, "width": width, "height": height,
                # Shipped so Lua derives its own wait from the same number
                # (N3c) instead of a second, independently-tuned constant.
                "timeout_s": timeout,
            },
        )
        result = job_queue.wait_result(job_id, timeout)
        if result is None:
            _log.warning(
                "fetch_thumbnails_chunked: timeout (%.1fs) on chunk %d-%d/%d — "
                "those photos fall back to the passive preview.",
                timeout, start, start + len(chunk), total,
            )
        else:
            for t in result.thumbnails:
                out[t.photo_id] = t
            _log_undersized(result.thumbnails, width, height, catalog_path)
        tick(min(start + len(chunk), total), total)
    return out


def _log_undersized(thumbnails, width: int, height: int, catalog_path: str | None) -> None:
    """Reports renders served below the requested grid, with the tier actually
    served (`ThumbnailResult.width/height`, read from the JPEG by the plugin).

    requestJpegThumbnail does not honour the requested size — it serves whichever
    preview tier Lr has cached. Those renders are rejected downstream
    (`reject_if_undersized`), and until now the only trace was one warning per
    photo at decode time, with no way to tell "Lr has no 2048 tier for this
    catalog" from "this one photo failed". A whole chunk coming back at the same
    sub-grid size is the signature of the catalog's Standard Preview Size being
    below the measurement grid.
    """
    want = max(width, height)
    small = [t for t in thumbnails if t.width and max(t.width, t.height or 0) < want]
    if not small:
        return
    sizes: dict[str, int] = {}
    for t in small:
        key = f"{t.width}x{t.height}"
        sizes[key] = sizes.get(key, 0) + 1
    advice = catalog.preview_size_advice(catalog_path, want) or (
        "Lr serves a cached preview tier, not the requested size — check Catalog "
        "Settings > File Handling > Standard Preview Size."
    )
    _log.warning(
        "get_thumbnails: %d/%d render(s) below the %d grid — tiers served: %s. %s",
        len(small), len(thumbnails), want,
        ", ".join(f"{k} (x{n})" for k, n in sorted(sizes.items(), key=lambda kv: -kv[1])),
        advice,
    )


class FreshRenderWorker(QThread):
    """Fetches fresh-render thumbnails for a selection, submitted in chunks."""

    finished_result = Signal(dict)   # {photo_id: ThumbnailResult}
    progress = Signal(str)
    progress_count = Signal(int, int)
    failed = Signal(str)

    def __init__(self, photos: list[PhotoResult], width: int, height: int) -> None:
        super().__init__()
        self._photos = photos
        self._width = width
        self._height = height

    def run(self) -> None:
        try:
            ids = [p.photo_id for p in self._photos]
            out = fetch_thumbnails_chunked(
                ids, self._width, self._height,
                progress=self.progress.emit, progress_count=self.progress_count.emit,
                catalog_path=next((p.catalog_path for p in self._photos if p.catalog_path), None),
            )
            self.finished_result.emit(out)
        except Exception as exc:  # safety net, mirrors NeutralPreviewWorker
            self.failed.emit(str(exc))
