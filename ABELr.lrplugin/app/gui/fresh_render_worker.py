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

from ..server.job_queue import job_queue
from ..server.models import JobType, PhotoResult, ThumbnailResult

_log = logging.getLogger("abelr.fresh_render")

# Chunk size: larger than render_probe's _CHUNK_SIZE=16 since there's no
# write/restore risk window to bound here, just the request's own timeout.
_CHUNK_SIZE = 40
_SECONDS_PER_PHOTO = 0.6
_MIN_TIMEOUT = 30.0


def fetch_thumbnails_chunked(
    photo_ids: list[str],
    width: int,
    height: int,
    *,
    progress: Callable[[str], None] | None = None,
    progress_count: Callable[[int, int], None] | None = None,
    chunk_size: int = _CHUNK_SIZE,
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
        timeout = max(_MIN_TIMEOUT, _SECONDS_PER_PHOTO * len(chunk))
        job_id = job_queue.submit(
            JobType.GET_THUMBNAILS,
            {"photo_ids": chunk, "width": width, "height": height},
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
        tick(min(start + len(chunk), total), total)
    return out


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
            )
            self.finished_result.emit(out)
        except Exception as exc:  # safety net, mirrors NeutralPreviewWorker
            self.failed.emit(str(exc))
