"""Selection of the **render** (output-referred) measurement channel.

Three channels, chosen for reliability/speed (user decision):

1. **requestJpegThumbnail** (plugin job `get_thumbnails` / `render_probe`) — fresh Lr
   render, written as JPEG to disk by the plugin. Reflects the current (or probed)
   settings. **Priority** channel; also the only one that lets us PROBE the response.
2. **Previews.lrdata** (`previews.PreviewIndex`) — already-cached rendered preview. Free,
   fast, but may be stale/absent. Passive fallback.
3. **LrExportSession** — full render export, slow/costly. Last resort (not wired
   here; to be enabled on the plugin side if channel 1 turns out to return a stale cache).

This module is **App-side**: it doesn't submit a job itself (that lives in the GUI
workers via the queue). `resolve_render_path` locates the render **file** (channel
priority only, no decoding) for the GPU pipeline (`gpu_jpeg`/`render_metrics_gpu`
decode and measure it).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from .previews import PreviewIndex


class RenderChannel(str, Enum):
    THUMBNAIL = "thumbnail"   # requestJpegThumbnail (fresh, priority)
    PREVIEW = "preview"       # Previews.lrdata (passive fallback)
    EXPORT = "export"         # LrExportSession (last resort, not wired)
    NONE = "none"             # no render available


def resolve_render_path(
    *,
    thumbnail_path: str | Path | None = None,
    preview_index: PreviewIndex | None = None,
    id_global: str | None = None,
) -> tuple[Path | None, RenderChannel]:
    """Locates the render **file** (without decoding) following channel priority.

    The path is handed to the GPU pipeline (read its bytes, decode on GPU via
    nvJPEG) rather than decoded here on CPU.
    Priority: fresh thumbnail (plugin) → Previews.lrdata preview → None.
    """
    if thumbnail_path is not None and Path(thumbnail_path).is_file():
        return Path(thumbnail_path), RenderChannel.THUMBNAIL
    if preview_index is not None and id_global:
        p = preview_index.rendered_path(id_global)
        if p is not None:
            return p, RenderChannel.PREVIEW
    return None, RenderChannel.NONE
